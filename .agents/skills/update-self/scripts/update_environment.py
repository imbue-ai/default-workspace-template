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
from pathlib import Path
from typing import Mapping, NamedTuple, Sequence

import tool_env
from update_apply_contract import SnapshotRecord, snapshots_root
from update_banding import ExpendWrapper
from update_classification import ApplyPlan, AppTool
from update_layout import (
    FRONTEND_BUNDLES,
    MNGR_DIR,
    MNGR_EXECUTABLE,
    MNGR_PLUGIN_KEY,
    MNGR_TOOL_NAME,
    NPM_ROOT_DIR,
    PLUGIN_MANIFEST_PATH,
    PROVISIONER_HOME,
    PROVISIONER_PATH,
    PROVISIONER_SCRIPT,
    RECEIPT,
)
from update_runtime import Runner, run_checked, tail

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


def snapshot_targets(
    plan: ApplyPlan, repo_root: Path, destinations: Mapping[str, ToolDestination]
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

    A tool environment is named by ``destinations``
    (:func:`resolve_tool_destinations`), the same value the reinstall is given,
    so what is copied aside is what the reinstall overwrites.
    """
    targets: list[tuple[str, Path]] = []
    if plan.frontend:
        for bundle in FRONTEND_BUNDLES:
            targets.append((bundle.snapshot_name, repo_root / bundle.static_dir))
    if plan.frontend_manifest:
        targets.append(("node_modules", repo_root / NPM_ROOT_DIR / "node_modules"))
    tool_names: list[str] = []
    if plan.backend_manifest:
        targets.append(("venv", repo_root / ".venv"))
        tool_names.append(MNGR_TOOL_NAME)
    tool_names.extend(app.tool_name for app in plan.app_tools if app.is_critical)
    for tool_name in tool_names:
        targets.append(
            (
                tool_snapshot_name(tool_name),
                destinations[tool_name].tool_dir / tool_name,
            )
        )
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
    destinations: Mapping[str, ToolDestination],
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
    for name, source in snapshot_targets(plan, repo_root, destinations):
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
    """The homes a live apply sweeps for stale tool installs: the caller's
    ``$HOME`` (where a pre-minds-v0.4.3 apply left its copy) and the one the
    build installs tools under.

    The build's is ``tool_env``'s literal default, not :func:`tool_env.tool_home`:
    this list feeds a deletion, and ``TOOL_ENV_HOME`` is a test override that
    must not be able to redirect it. Which installation survives the sweep is
    a separate question, answered from ``PATH`` by
    :func:`remove_shadowing_mngr_installs` and
    :func:`remove_shadowing_app_tool_installs`.
    """
    homes = [Path(tool_env.DEFAULT_TOOL_HOME)]
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
    return tool_env.remove_shadowing_installs(
        canonical[0], homes, MNGR_TOOL_NAME, MNGR_EXECUTABLE
    )


def remove_shadowing_app_tool_installs(
    runner: Runner, homes: Sequence[Path], app_tools: Sequence[AppTool]
) -> list[Path]:
    """Delete stale copies of each app's tool under ``homes`` that shadow the pinned one.

    The same pre-pin installs that left a second mngr under ``$HOME/.local`` left one
    of every app tool beside it, and a login shell reaches those first too -- a month
    behind the copy each apply refreshes. Stricter than the mngr sweep, because an app's
    program line resolves its entry point through the pinned bin directory: a tool is
    swept only when this apply's PATH already reaches its pinned installation, so
    neither the copy PATH runs nor the pinned one is ever the one removed. A tool whose
    only copy is under ``$HOME`` is left where it is.
    """
    pinned = _pinned_tool_location()[0].resolve()
    removed: list[Path] = []
    for app in app_tools:
        canonical = _installed_tool_location(app.executable, app.tool_name, runner)
        if canonical is None or canonical[0].resolve() != pinned:
            continue
        removed.extend(
            tool_env.remove_shadowing_installs(
                canonical[0], homes, app.tool_name, app.executable
            )
        )
    return removed


def _pinned_tool_location() -> tuple[Path, Path]:
    """``(tool_dir, bin_dir)`` of the installation the build pins, resolved from
    nothing on this machine.

    This is the same pair ``install_mngr.py`` sets ``UV_TOOL_DIR`` /
    ``UV_TOOL_BIN_DIR`` to, taken from the one module both trees share, so the
    apply's floor cannot drift from the build's target.
    """
    home = tool_env.tool_home()
    return tool_env.tools_dir(home), tool_env.bin_dir(home)


class ToolDestination(NamedTuple):
    """Where a refresh of one tool installs, and what it says about landing there.

    ``note`` is the line the reinstall writes when the destination is not the
    tool's own installation, and ``None`` when it is -- the ordinary case, which
    has nothing to explain.
    """

    tool_dir: Path
    bin_dir: Path
    note: str | None


def _refresh_destination(
    executable: str, tool_name: str, runner: Runner
) -> ToolDestination:
    """Where a refresh of ``tool_name`` installs: ``executable``'s own
    installation when we can confirm which that is, else the mngr tool's, else
    the one the build pins.

    A tool the merge adds (an app this workspace has never run) is on no PATH
    yet, and uv's own default tool directory follows ``$HOME`` -- which at
    runtime is not the one build_workspace.sh installed under, and whose bin
    directory is on nobody's PATH. Left to that default the new tool installs
    fine and is never found. So a tool with no installation of its own goes
    beside the mngr tool, whose bin directory every program line resolves its
    entry point through.

    When neither resolves there is still a right answer, and it is never uv's
    default: the build installs under a pinned home regardless of the ``$HOME``
    it runs beneath, so that is where a reachable copy goes. Falling through to
    uv instead put the tool under ``/home/user/.local`` and exited 0, which the
    apply read as success -- a workspace whose ``mngr`` had been deleted, or an
    old lease that never had a uv-tool ``mngr`` to resolve, updated itself into
    having no ``mngr`` on any PATH at all.
    """
    own = _installed_tool_location(executable, tool_name, runner)
    if own is not None:
        return ToolDestination(own[0], own[1], None)
    beside_mngr = _installed_tool_location(MNGR_EXECUTABLE, MNGR_TOOL_NAME, runner)
    if beside_mngr is not None:
        return ToolDestination(
            beside_mngr[0],
            beside_mngr[1],
            f"refresh: '{executable}' is not an installed uv tool on PATH; "
            f"installing '{tool_name}' beside the mngr tool ({beside_mngr[1]}).\n",
        )
    pinned = _pinned_tool_location()
    return ToolDestination(
        pinned[0],
        pinned[1],
        f"refresh: could not identify the uv tool behind '{executable}' "
        f"(not an installed uv tool on PATH) nor the one behind "
        f"'{MNGR_EXECUTABLE}'; installing '{tool_name}' into the build's "
        f"pinned tool home ({pinned[1]}).\n",
    )


def resolve_tool_destinations(
    plan: ApplyPlan, runner: Runner
) -> dict[str, ToolDestination]:
    """Where each tool ``plan`` reinstalls will land, keyed by tool name.

    Resolved once and handed to both the snapshot and the reinstall, so the
    environment copied aside is the one the reinstall rebuilds.
    """
    tools: list[tuple[str, str]] = []
    if plan.backend_manifest:
        tools.append((MNGR_TOOL_NAME, MNGR_EXECUTABLE))
    tools.extend((app.tool_name, app.executable) for app in plan.app_tools)
    return {
        tool_name: _refresh_destination(executable, tool_name, runner)
        for tool_name, executable in tools
    }


def _tool_extras(tool_name: str, tool_dir: Path) -> list[str]:
    """Return the ``--with``/``--with-editable`` args a tool was installed with.

    A ``uv tool install --reinstall`` rebuilds the environment from the base
    package alone, dropping every extra -- for the mngr tool those extras *are*
    its plugins. uv records them in the tool's receipt; read them back rather
    than keeping a second copy of the plugin list to drift.

    ``tool_dir`` is the directory this install is about to write to, so the
    receipt read is the one already there. Asking ``uv tool dir`` instead would
    answer for ``$HOME``, which is the directory being overridden.
    """
    receipt = tool_dir / tool_name / RECEIPT
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
            continue  # the base package, which we re-pin to its in-tree source
        editable = requirement.get("editable") or requirement.get("directory")
        if editable:
            extras.extend(["--with-editable", editable])
        elif requirement.get("git"):
            extras.extend(["--with", f"{name} @ git+{requirement['git']}"])
        elif requirement.get("specifier"):
            extras.extend(["--with", f"{name}{requirement['specifier']}"])
        else:
            extras.extend(["--with", name])
    return extras


def _manifest_extras(plugin_key: str, repo_root: Path) -> list[str]:
    """The ``--with-editable`` args ``PLUGIN_MANIFEST_PATH`` assigns to ``plugin_key``.

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
    extras: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            _warn_manifest_unread(manifest_path, "it lists a plugin with no path")
            continue
        if plugin_key in entry.get("tools", []):
            extras.extend(["--with-editable", str(repo_root / str(entry["path"]))])
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


def _merge_extras(*extra_lists: list[str]) -> list[str]:
    """Concatenate ``--with``/``--with-editable`` pairs, dropping repeats of a target."""
    merged: list[str] = []
    seen: set[str] = set()
    for extras in extra_lists:
        for flag, target in zip(extras[::2], extras[1::2]):
            if target in seen:
                continue
            seen.add(target)
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
    source_dir: str,
    plugin_key: str,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    destination: ToolDestination,
    timeout: float | None = None,
) -> None:
    """Re-resolve ``tool_name`` into ``destination`` from its in-tree source,
    keeping the extras it was installed with and adding the merged tree's own
    plugin manifest (the plugins it assigns to ``plugin_key``).

    ``expend`` gates the expendable tag: a forward install may be shed (the
    rollback restores the tool-environment snapshot), a recovery install must
    keep the orchestrator's protection (``keep_protected``).
    """
    if destination.note is not None:
        sys.stderr.write(destination.note)
    env = dict(os.environ)
    env["UV_TOOL_DIR"] = str(destination.tool_dir)
    env["UV_TOOL_BIN_DIR"] = str(destination.bin_dir)
    argv = [
        "uv",
        "tool",
        "install",
        "-e",
        source_dir,
        *_merge_extras(
            _tool_extras(tool_name, destination.tool_dir),
            _manifest_extras(plugin_key, repo_root),
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
    destinations: Mapping[str, ToolDestination],
    timeout: float | None = None,
) -> None:
    """Re-resolve the two shared backend environments from the current tree,
    mirroring ``build_workspace.sh``: the vendored ``mngr`` tool and the
    workspace venv (``uv sync``). ``timeout`` bounds each (the forward apply's
    budget; recovery passes none)."""
    _reinstall_tool(
        MNGR_TOOL_NAME,
        MNGR_DIR,
        MNGR_PLUGIN_KEY,
        repo_root,
        runner,
        expend,
        destinations[MNGR_TOOL_NAME],
        timeout,
    )
    run_checked(
        runner,
        expend(["uv", "sync", "--all-packages", "--frozen"]),
        repo_root,
        "uv sync --all-packages --frozen",
        timeout=timeout,
    )


def refresh_app_tools(
    app_tools: Sequence[AppTool],
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    destinations: Mapping[str, ToolDestination],
    timeout: float | None = None,
) -> None:
    """Reinstall each app's own tool environment from its directory in the
    current tree, with the mngr plugins the plugin table assigns to it, as
    ``build_workspace.sh`` installs them. ``timeout`` bounds each install."""
    for app in app_tools:
        _reinstall_tool(
            app.tool_name,
            app.directory,
            app.plugin_key,
            repo_root,
            runner,
            expend,
            destinations[app.tool_name],
            timeout,
        )
