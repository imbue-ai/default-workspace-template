"""The served environment the apply rebuilds and protects: the mngr tool
environment, one tool environment per Python app, the root venv,
``node_modules``, the provisioner run, and the pre-apply snapshots a rollback
restores from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tool_env
from update_apply_contract import SnapshotRecord, snapshots_root
from update_banding import ExpendWrapper
from update_classification import ApplyPlan, AppTool
from update_layout import (
    FRONTEND_BUNDLES,
    MNGR_ASSETS_SCRIPT,
    MNGR_EXECUTABLE,
    MNGR_PLUGIN_KEY,
    MNGR_TOOL_NAME,
    NPM_ROOT_DIR,
    PLUGIN_MANIFEST_PATH,
    PROVISIONER_HOME,
    PROVISIONER_PATH,
    PROVISIONER_SCRIPT,
    PYPROJECT_PATH,
    RECEIPT,
)
from update_runtime import ApplyFailed, Runner, run_checked, tail

ENVIRONMENT_REFRESH_TIMEOUT_SECONDS = 1200.0

_PROVISIONER_TIMEOUT_SECONDS = 1800.0


def provisioner_env() -> dict:
    """The canonical environment for a live provisioner run (see
    :data:`PROVISIONER_HOME`).

    ``PROVISION_FORCE=1`` runs the script past its content-addressed skip guard
    (``system/scripts/_provision_guard.sh``). The apply only runs the
    provisioner when a file it reads changed, so the guard has nothing to
    save it; the one tree the guard can match is one a rollback re-provisioned
    away from, and skipping there leaves the retry's toolchain at the
    rolled-back versions.
    """
    # The script's version pins are `:=` defaults, so an inherited *_VERSION
    # (an image built when the Dockerfile still exported its pins as ENV) would
    # win over the merged tree's and reinstall the old version while the
    # script's own pin check passed. The pins are the tree's; drop them all.
    env = {
        key: value for key, value in os.environ.items() if not key.endswith("_VERSION")
    }
    env["HOME"] = PROVISIONER_HOME
    env["PATH"] = PROVISIONER_PATH
    env["PROVISION_FORCE"] = "1"
    return env


def run_provisioner(runner: Runner, repo_root: Path) -> str | None:
    """Re-run the pinned-toolchain provisioner live; return why it failed, or
    ``None`` on success.

    Never raises -- a hang and a spawn failure (no ``bash``, an exec error)
    both come back as the reason: the forward apply carries on past a failed
    provisioner (a failed tool install leaves the tree and services
    consistent; whether the update is good is what the probes decide) and
    records the failure instead, so the caller needs the reason, not an
    exception.
    """
    try:
        result = runner.run_process_group(
            ["bash", PROVISIONER_SCRIPT],
            cwd=str(repo_root),
            env=provisioner_env(),
            timeout=_PROVISIONER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"bash {PROVISIONER_SCRIPT} did not finish within "
            f"{_PROVISIONER_TIMEOUT_SECONDS:g}s (hung or stalled)"
        )
    except OSError as exc:
        return f"bash {PROVISIONER_SCRIPT} could not be run ({exc})"
    returncode = getattr(result, "returncode", 0)
    if returncode == 0:
        return None
    stderr = tail((getattr(result, "stderr", "") or "").strip(), 20)
    return f"bash {PROVISIONER_SCRIPT} failed (exit {returncode}): {stderr}"


def _tool_environment_dir(
    executable: str, tool_name: str, runner: Runner
) -> Path | None:
    """The installed tool environment behind ``executable``, or ``None``.

    Resolved from the console script's shebang (see :func:`_tool_location`), so
    the snapshot copies the installation actually being run rather than
    whatever uv would default to under this process's ``$HOME``.
    """
    found = runner.which(executable)
    location = _tool_location(Path(found), tool_name) if found is not None else None
    if location is None:
        return None
    return location[0] / tool_name


def snapshot_targets(
    plan: ApplyPlan, repo_root: Path, runner: Runner
) -> list[tuple[str, Path]]:
    """The state the apply's destructive steps can destroy, by plan.

    Every entry is a directory restored by a plain copy: the built bundles
    (the shell's and the chat's) and the npm workspace's ``node_modules`` (the
    build and ``npm ci`` both delete before they produce), the root venv
    (``uv sync`` rewrites it), the mngr tool environment, and the tool
    environment of every *critical* app the plan
    reinstalls (``uv tool install --reinstall`` rebuilds them from scratch). A
    non-critical app's tool is not copied aside: a rollback reinstalls it from
    the restored tree instead.
    """
    targets: list[tuple[str, Path]] = []
    if plan.frontend:
        for bundle in FRONTEND_BUNDLES:
            targets.append((bundle.snapshot_name, repo_root / bundle.static_dir))
    if plan.frontend_manifest:
        targets.append(("node_modules", repo_root / NPM_ROOT_DIR / "node_modules"))
    tools: list[tuple[str, str]] = []
    if plan.backend_manifest:
        targets.append(("venv", repo_root / ".venv"))
        tools.append((MNGR_TOOL_NAME, MNGR_EXECUTABLE))
    tools.extend(
        (app.tool_name, app.executable) for app in plan.app_tools if app.is_critical
    )
    for tool_name, executable in tools:
        tool_dir = _tool_environment_dir(executable, tool_name, runner)
        if tool_dir is None:
            sys.stderr.write(
                f"note: could not locate the uv tool environment behind "
                f"'{executable}' (not a uv tool on PATH), so it will not be "
                "copied aside; a failed apply will have to rebuild it to recover.\n"
            )
            continue
        targets.append((tool_snapshot_name(tool_name), tool_dir))
    return targets


def tool_snapshot_name(tool_name: str) -> str:
    return f"tool-{tool_name}"


# The snapshot names that together cover the backend environments a manifest
# change rebuilds. A rollback that could not put both of them back has to
# re-resolve both from the restored tree: a restored venv over a tool
# environment still built from the rolled-back-away tree is the
# ModuleNotFoundError-on-``mngr`` state that reads as recovered while nothing
# works. An app's tool environment stands on its own (it resolves from the
# app's pyproject, not the venv), so each is judged by its own snapshot name.
BACKEND_SNAPSHOT_NAMES = frozenset({"venv", tool_snapshot_name(MNGR_TOOL_NAME)})


def take_snapshots(
    plan: ApplyPlan,
    repo_root: Path,
    runner: Runner,
    existing: Sequence[SnapshotRecord],
) -> list[SnapshotRecord]:
    """Copy aside everything the forward apply could destroy; return the manifest.

    ``existing`` is the marker's already-recorded manifest (a resumed apply):
    a copy that is still on disk is reused rather than re-taken, because by
    resume time the live state may already be part-destroyed -- re-copying it
    would overwrite the good copy with the wreckage.

    A target that does not exist contributes nothing (a workspace that never
    built a bundle has nothing to lose), and a copy that cannot be taken
    degrades to a warning rather than a refusal: this is a precaution, and
    recovery then falls back to rebuilding.
    """
    kept: list[SnapshotRecord] = [
        record for record in existing if Path(record.copy).exists()
    ]
    already = {record.name for record in kept}
    root = snapshots_root(repo_root)
    for name, source in snapshot_targets(plan, repo_root, runner):
        if name in already:
            continue
        if not source.exists():
            sys.stderr.write(
                f"note: nothing to copy aside for '{name}' ({source} does not exist); "
                "a failed apply will have to rebuild it to recover.\n"
            )
            continue
        copy = root / name
        try:
            if copy.exists():
                shutil.rmtree(copy)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, copy, symlinks=True)
        except OSError as exc:
            shutil.rmtree(copy, ignore_errors=True)
            sys.stderr.write(
                f"warning: could not copy '{name}' aside ({type(exc).__name__}: {exc}); "
                "a failed apply will have to rebuild it to recover.\n"
            )
            continue
        kept.append(SnapshotRecord(name=name, source=str(source), copy=str(copy)))
    return kept


def restore_snapshots(snapshots: Sequence[SnapshotRecord]) -> list[str]:
    """Put every pre-apply copy back over its original path.

    Returns the names that could NOT be restored. Never raises: this is the
    last line of defense, and one failed restore must not stop the others.
    """
    failed: list[str] = []
    for record in snapshots:
        source = Path(record.source)
        copy = Path(record.copy)
        if not copy.exists():
            failed.append(record.name)
            sys.stderr.write(
                f"recovery: the copy of '{record.name}' at {copy} is gone; cannot restore it.\n"
            )
            continue
        try:
            if source.exists():
                shutil.rmtree(source)
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(copy, source, symlinks=True)
        except OSError as exc:
            failed.append(record.name)
            sys.stderr.write(
                f"recovery: could not restore '{record.name}' to {source} "
                f"({type(exc).__name__}: {exc}).\n"
            )
    return failed


def discard_snapshots(repo_root: Path) -> None:
    shutil.rmtree(snapshots_root(repo_root), ignore_errors=True)


# Shared with the build (see tool_env.py's module docstring on why it is vendored here
# rather than imported across trees). Kept as a name in this module because the tests and
# the rest of the file already speak it.
_tool_location = tool_env.tool_location


def _installed_tool_location(
    executable: str, tool_name: str, runner: Runner
) -> tuple[Path, Path] | None:
    """``(tool_dir, bin_dir)`` of the uv tool installation behind ``executable``
    as found on PATH, or ``None`` when there is none to confirm."""
    found = runner.which(executable)
    return _tool_location(Path(found), tool_name) if found is not None else None


# CLEANUP: remove once no supported workspace can still carry a tool install
# left by a pre-minds-v0.4.3 apply (those followed uv's $HOME default instead
# of targeting the installation on PATH).
def default_sweep_homes() -> list[Path]:
    """The homes a live apply sweeps for a stale mngr install: the caller's
    ``$HOME`` (where a pre-minds-v0.4.3 apply left its copy) and the image
    build's."""
    homes = [Path(PROVISIONER_HOME)]
    if os.environ.get("HOME"):
        homes.insert(0, Path(os.environ["HOME"]))
    return homes


def remove_shadowing_mngr_installs(runner: Runner, homes: Sequence[Path]) -> list[Path]:
    """Delete stale copies of the mngr tool under ``homes`` that shadow the one
    this apply refreshes.

    A pre-minds-v0.4.3 apply reinstalled the tool wherever uv's default pointed, which at
    runtime is ``$HOME/.local`` rather than the ``/root/.local`` the image was built
    under. That copy sits first on every login shell's PATH (the terminal app, the desktop
    app's ``mngr exec``) and is never refreshed again, so the first release adding a
    dependency breaks every command those shells run while the refreshed copy reports
    success.

    This resolves which installation to keep -- the one behind ``mngr`` on this apply's
    PATH, which is what ``Runner`` is for -- and hands the rest to the shared sweep the
    build also runs. The homes are the caller's, never read from here: the
    apply's tests drive this with a fake ``mngr`` on PATH, and a sweep that
    reached for the real ``/root`` on its own deleted the workspace's live
    install from under the suite that was validating a release.
    """
    canonical = _installed_tool_location(MNGR_EXECUTABLE, MNGR_TOOL_NAME, runner)
    if canonical is None:
        return []
    return tool_env.remove_shadowing_mngr_installs(canonical[0], homes)


def _uv_tool_env(executable: str, tool_name: str, runner: Runner) -> dict:
    """The environment for a ``uv tool`` call, aimed at ``executable``'s own
    installation when we can confirm which that is, else at the mngr tool's.

    A tool the merge adds (an app this workspace has never run) is on no PATH
    yet, and uv's own default tool directory follows ``$HOME`` -- which at
    runtime is not the one build_workspace.sh installed under, and whose bin
    directory is on nobody's PATH. Left to that default the new tool installs
    fine and is never found. So a tool with no installation of its own goes
    beside the mngr tool, whose bin directory every program line resolves its
    entry point through.
    """
    env = dict(os.environ)
    location = _installed_tool_location(executable, tool_name, runner)
    if location is None:
        beside_mngr = _installed_tool_location(MNGR_EXECUTABLE, MNGR_TOOL_NAME, runner)
        if beside_mngr is None:
            sys.stderr.write(
                f"refresh: could not identify the uv tool behind '{executable}' "
                f"(not an installed uv tool on PATH) nor the one behind "
                f"'{MNGR_EXECUTABLE}'; letting uv choose the tool directory, which may "
                "install a copy that nothing on PATH runs.\n"
            )
            return env
        sys.stderr.write(
            f"refresh: '{executable}' is not an installed uv tool on PATH; installing "
            f"'{tool_name}' beside the mngr tool ({beside_mngr[1]}).\n"
        )
        location = beside_mngr
    env["UV_TOOL_DIR"] = str(location[0])
    env["UV_TOOL_BIN_DIR"] = str(location[1])
    return env


_GIT_REF_QUERY_KEYS = frozenset({"rev", "tag", "branch"})
_GIT_SUBDIRECTORY_QUERY_KEY = "subdirectory"


def _format_git_url(url: str) -> str:
    """Rewrite a git URL from uv's receipt form into the form ``uv tool install`` accepts.

    Input, as uv writes it into uv-receipt.toml:
    ``https://github.com/imbue-ai/mngr?subdirectory=libs%2Fmngr&rev=v0.2.17``
    Output, as ``uv tool install`` reads it back:
    ``https://github.com/imbue-ai/mngr@v0.2.17#subdirectory=libs/mngr``
    """
    parsed = urlsplit(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    ref = next((value for key, value in params if key in _GIT_REF_QUERY_KEYS), None)
    subdirectory = next(
        (value for key, value in params if key == _GIT_SUBDIRECTORY_QUERY_KEY), None
    )
    if ref is None and subdirectory is None:
        return url
    # uv only honors "@<ref>" directly after the path; anywhere else it is silently
    # ignored and the default branch is installed, e.g. "repo?foo=bar@v0.2.17" ignores @v0.2.17.
    path = parsed.path if ref is None else f"{parsed.path}@{ref}"
    rewritten = [
        (key, value)
        for key, value in params
        if key not in _GIT_REF_QUERY_KEYS and key != _GIT_SUBDIRECTORY_QUERY_KEY
    ]
    subdirectory_fragment = (
        "" if subdirectory is None else f"{_GIT_SUBDIRECTORY_QUERY_KEY}={subdirectory}"
    )
    fragment = "&".join(
        part for part in (parsed.fragment, subdirectory_fragment) if part
    )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(rewritten), fragment)
    )


_MNGR_SUBDIRECTORY = Path("libs/mngr")


@dataclass(frozen=True)
class _GitPin:
    """mngr comes from a git repo at a fixed commit."""

    git_url: str
    rev: str

    def requirement(self, package: str, subdirectory: str) -> str:
        return f"{package} @ git+{self.git_url}@{self.rev}#subdirectory={subdirectory}"


def _mngr_source(repo_root: Path) -> _GitPin | None:
    """Where the merged tree's pyproject.toml says ``imbue-mngr`` comes from.

    ``[tool.uv.sources]`` gives it as a git commit
    (``{ git = "...", rev = "<commit>", subdirectory = "libs/mngr" }``).

    ``None`` when the source cannot be read; callers degrade to a warning because this
    also runs on the rollback path, where a raised error would escape the apply's
    last line of defense.
    """
    pyproject = repo_root / PYPROJECT_PATH
    try:
        source = (
            tomllib.loads(pyproject.read_text())
            .get("tool", {})
            .get("uv", {})
            .get("sources", {})
            .get(MNGR_TOOL_NAME)
        )
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(source, dict):
        return None
    if "git" in source and "rev" in source:
        return _GitPin(git_url=str(source["git"]), rev=str(source["rev"]))
    return None


def _mngr_base_arguments(repo_root: Path) -> list[str]:
    """The positional arguments that reinstall the mngr tool from the merged tree's source."""
    source = _mngr_source(repo_root)
    if source is None:
        raise ApplyFailed(
            f"{PYPROJECT_PATH} does not give {MNGR_TOOL_NAME} a source in [tool.uv.sources] "
            '(expected { git = "...", rev = "<commit>", subdirectory = "libs/mngr" }); '
            "the mngr tool cannot be re-resolved without it"
        )
    return [source.requirement(MNGR_TOOL_NAME, _MNGR_SUBDIRECTORY.as_posix())]


def _plugin_extra(source: _GitPin, package: str, subdirectory: str) -> list[str]:
    """The ``--with`` pair that adds one manifest plugin from ``source``."""
    return ["--with", source.requirement(package, subdirectory)]


def _tool_extras(
    tool_name: str, repo_root: Path, runner: Runner, env: dict
) -> list[str]:
    """Return the ``--with``/``--with-editable`` args a tool was installed with.

    A ``uv tool install --reinstall`` rebuilds the environment from the base
    package alone, dropping every extra -- for the mngr tool those extras *are*
    its plugins. uv records them in the tool's receipt; read them back rather
    than keeping a second copy of the plugin list to drift.
    """
    tool_dir = env.get("UV_TOOL_DIR")
    if tool_dir is None:
        try:
            result = runner.run(
                ["uv", "tool", "dir"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except OSError as exc:
            _warn_extras_lost(tool_name, f"'uv tool dir' could not be run ({exc})")
            return []
        if getattr(result, "returncode", 0) != 0:
            _warn_extras_lost(tool_name, f"'uv tool dir' exited {result.returncode}")
            return []
        tool_dir = (getattr(result, "stdout", "") or "").strip()
    receipt = Path(tool_dir) / tool_name / RECEIPT
    try:
        parsed = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if receipt.is_file():
            _warn_extras_lost(tool_name, f"{receipt} is unreadable ({exc})")
        return []
    extras: list[str] = []
    for requirement in parsed.get("tool", {}).get("requirements", []):
        name = requirement.get("name", "")
        if _canonical(name) == _canonical(tool_name):
            continue  # the base package, which we re-pin to the commit pyproject.toml gives it
        editable = requirement.get("editable") or requirement.get("directory")
        if editable:
            # uv installs an editable from its ``pyproject.toml``; a receipt naming a path
            # the merged tree no longer has one at is dropped, and the manifest supplies
            # that package instead. A directory alone is not enough: a deleted project
            # leaves its gitignored ``__pycache__`` behind, so the path still resolves.
            if (Path(editable) / PYPROJECT_PATH).is_file():
                extras.extend(["--with-editable", editable])
        elif requirement.get("git"):
            extras.extend(
                ["--with", f"{name} @ git+{_format_git_url(requirement['git'])}"]
            )
        elif requirement.get("specifier"):
            extras.extend(["--with", f"{name}{requirement['specifier']}"])
        else:
            extras.extend(["--with", name])
    return extras


def _manifest_extras(plugin_key: str, repo_root: Path) -> list[str]:
    """The ``--with``/``--with-editable`` args ``PLUGIN_MANIFEST_PATH`` assigns to ``plugin_key``, from the merged tree's mngr source.

    ``plugin_key`` is how the plugin table names the tool: ``mngr`` for the mngr
    tool and an app's manifest name for the app's tool. Empty for a tree that
    predates the manifest (a rollback re-refreshes the restored tree, and the
    receipt alone was that tree's whole answer).

    A manifest that will not parse, or an entry that is not a table naming a
    ``path``, degrades to a warning rather than raising: this also runs on the
    rollback path, from ``_recover_running_state``, which catches only
    ``ApplyFailed`` and ``OSError`` -- so a ``TOMLDecodeError`` here would
    escape the apply's last line of defense and leave a live marker, an
    unrestored environment and no emergency record.
    """
    manifest_path = repo_root / PLUGIN_MANIFEST_PATH
    if not manifest_path.is_file():
        return []
    try:
        manifest = tomllib.loads(manifest_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _warn_manifest_unread(manifest_path, f"it could not be read ({exc})")
        return []
    entries = manifest.get("plugins", [])
    if not isinstance(entries, list):
        _warn_manifest_unread(
            manifest_path, "its 'plugins' key is not a list of tables"
        )
        return []
    source = _mngr_source(repo_root)
    if source is None:
        _warn_manifest_unread(
            manifest_path, f"{PYPROJECT_PATH} does not give {MNGR_TOOL_NAME} a source"
        )
        return []
    extras: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not entry.get("package")
            or not entry.get("subdirectory")
        ):
            _warn_manifest_unread(
                manifest_path, "it lists a plugin with no package or subdirectory"
            )
            continue
        if plugin_key in entry.get("tools", []):
            extras.extend(
                _plugin_extra(source, str(entry["package"]), str(entry["subdirectory"]))
            )
    return extras


def _warn_manifest_unread(manifest_path: Path, why: str) -> None:
    """Say that the merged tree's plugin manifest was skipped, and what that costs.

    Deliberately not :func:`_warn_extras_lost`: the receipt is still readable
    here, so the reinstall keeps every plugin the tool already had. What is lost
    is narrower -- a plugin this release *adds* -- and saying "base package
    alone" would send an operator looking for a breakage that has not happened.
    """
    sys.stderr.write(
        f"refresh: skipping the plugin manifest at {manifest_path} ({why}); the tools "
        "keep the plugins their receipts already name, but any plugin this release "
        "adds will not be registered.\n"
    )


def _extra_key(flag: str, target: str) -> str:
    """What makes two extras the same package: its name, from a ``--with`` requirement or
    an editable path's ``pyproject.toml``; the path itself when that has no name."""
    if flag == "--with":
        for separator in (" @ ", "==", ">=", "<=", "~=", "!=", ">", "<", ";", "["):
            if separator in target:
                return _canonical(target.split(separator, 1)[0].strip())
        return _canonical(target.strip())
    try:
        name = (
            tomllib.loads((Path(target) / PYPROJECT_PATH).read_text())
            .get("project", {})
            .get("name")
        )
    except (OSError, tomllib.TOMLDecodeError):
        name = None
    return _canonical(name) if isinstance(name, str) and name else target


def _merge_extras(*extra_lists: list[str]) -> list[str]:
    """Concatenate ``--with``/``--with-editable`` pairs, keeping the first of each package.

    Order the lists so the one that should win comes first: the merged tree's manifest
    carries the new pin, the receipt the old one, and both name the same plugins.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for extras in extra_lists:
        for flag, target in zip(extras[::2], extras[1::2]):
            key = _extra_key(flag, target)
            if key in seen:
                continue
            seen.add(key)
            merged.extend([flag, target])
    return merged


def _warn_extras_lost(tool_name: str, why: str) -> None:
    sys.stderr.write(
        f"refresh: cannot read what '{tool_name}' was installed with ({why}); "
        "reinstalling from the base package alone, which drops any plugins it "
        "had registered.\n"
    )


def _canonical(name: str) -> str:
    """Normalize a package name the way packaging does, for comparison."""
    return name.replace("_", "-").lower()


def _reinstall_tool(
    tool_name: str,
    executable: str,
    base: list[str],
    plugin_key: str,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Re-resolve the installed ``executable``'s tool from ``base`` -- ``["-e", <dir>]``
    for an in-tree app, or a single git requirement for the pinned mngr -- keeping the
    extras it was installed with and adding the merged tree's own
    plugin manifest (the plugins it assigns to ``plugin_key``).

    ``expend`` gates the expendable tag: a forward install may be shed (the
    rollback restores the tool-environment snapshot), a recovery install must
    keep the orchestrator's protection (``keep_protected``).
    """
    env = _uv_tool_env(executable, tool_name, runner)
    argv = [
        "uv",
        "tool",
        "install",
        *base,
        *_merge_extras(
            _manifest_extras(plugin_key, repo_root),
            _tool_extras(tool_name, repo_root, runner, env),
        ),
        "--reinstall",
    ]
    run_checked(
        runner,
        expend(argv),
        repo_root,
        f"uv tool install {tool_name} --reinstall",
        env=env,
        timeout=timeout,
    )


def refresh_backend_dependencies(
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
    is_mngr_source_required: bool = True,
) -> None:
    """Re-resolve the two shared backend environments from the current tree,
    mirroring ``build_workspace.sh``: the ``mngr`` tool from the source
    pyproject.toml gives it, the assets that pin carries, and the workspace venv
    (``uv sync``). ``timeout`` bounds each (the forward apply's budget; recovery
    passes none).

    ``is_mngr_source_required`` is the recovery path's opt-out: a tree restored from
    before the pin gives ``imbue-mngr`` a source this cannot re-resolve, and the
    snapshot that tree was running is still installed, so warn and keep it rather than
    abandoning the recovery."""
    if is_mngr_source_required or _mngr_source(repo_root) is not None:
        _reinstall_tool(
            MNGR_TOOL_NAME,
            MNGR_EXECUTABLE,
            _mngr_base_arguments(repo_root),
            MNGR_PLUGIN_KEY,
            repo_root,
            runner,
            expend,
            timeout,
        )
    else:
        sys.stderr.write(
            f"refresh: {PYPROJECT_PATH} does not give {MNGR_TOOL_NAME} a source this can "
            "re-resolve; keeping the mngr tool the restored tree was running.\n"
        )
    run_checked(
        runner,
        expend(["uv", "sync", "--all-packages", "--frozen"]),
        repo_root,
        "uv sync --all-packages --frozen",
        timeout=timeout,
    )
    _fetch_mngr_assets(repo_root, runner, expend, timeout)


def _fetch_mngr_assets(
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Fetch the files the pin carries that no package does, into ``system/vendor/mngr-assets``.

    The frontends' ``prebuild`` runs this too, but an apply that installs a worker's
    bundle builds no frontend, and ``docs/system/style_guide.md`` is a symlink into the
    fetched tree. A no-op when the marker already names this pin. Absent in a tree that
    predates the script, which a rollback can restore.
    """
    script = repo_root / MNGR_ASSETS_SCRIPT
    if not script.is_file():
        return
    run_checked(
        runner,
        expend(["bash", str(script)]),
        repo_root,
        f"bash {MNGR_ASSETS_SCRIPT}",
        timeout=timeout,
    )


def refresh_app_tools(
    app_tools: Sequence[AppTool],
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Reinstall each app's own tool environment from its directory in the
    current tree, with the mngr plugins the plugin table assigns to it, as
    ``build_workspace.sh`` installs them. ``timeout`` bounds each install."""
    for app in app_tools:
        _reinstall_tool(
            app.tool_name,
            app.executable,
            ["-e", app.directory],
            app.plugin_key,
            repo_root,
            runner,
            expend,
            timeout,
        )
