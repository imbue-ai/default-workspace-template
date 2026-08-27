"""The served environment the apply rebuilds and protects: the two uv tool
environments, the root venv, ``node_modules``, the provisioner run, and the
pre-apply snapshots a rollback restores from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

from update_apply_contract import SnapshotRecord, snapshots_root
from update_banding import ExpendWrapper
from update_classification import ApplyPlan
from update_layout import (
    FRONTEND_DIR,
    MANIFEST_TOOL_NAMES,
    MNGR_DIR,
    MNGR_EXECUTABLE,
    MNGR_TOOL_NAME,
    PLUGIN_MANIFEST_PATH,
    PROVISIONER_HOME,
    PROVISIONER_PATH,
    PROVISIONER_SCRIPT,
    RECEIPT,
    STATIC_DIR,
    SYSTEM_INTERFACE_DIR,
    TOOL_NAME,
)
from update_runtime import Runner, run_checked, tail

ENVIRONMENT_REFRESH_TIMEOUT_SECONDS = 1200.0

_PROVISIONER_TIMEOUT_SECONDS = 1800.0


def provisioner_env(*, is_forced: bool = False) -> dict:
    """The canonical environment for a live provisioner run (see
    :data:`PROVISIONER_HOME`).

    ``is_forced`` sets ``PROVISION_FORCE=1``, which runs the script past its
    content-addressed skip guard (``system/scripts/_provision_guard.sh``).
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
    if is_forced:
        env["PROVISION_FORCE"] = "1"
    return env


def run_provisioner(
    runner: Runner, repo_root: Path, *, is_forced: bool = False
) -> str | None:
    """Re-run the pinned-toolchain provisioner live; return why it failed, or
    ``None`` on success.

    ``is_forced`` is for the rollback re-run: it runs from the restored tree,
    which is exactly the tree the provision guard's marker was written for, so
    without forcing it the guard would skip the very run that is meant to put
    the global toolchain back.

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
            env=provisioner_env(is_forced=is_forced),
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

    Every entry is a directory restored by a plain copy: the built bundle and
    ``node_modules`` (the build and ``npm ci`` both delete before they
    produce), the root venv (``uv sync`` rewrites it), and the two uv tool
    environments (``uv tool install --reinstall`` rebuilds them from scratch).
    """
    targets: list[tuple[str, Path]] = []
    if plan.frontend:
        targets.append(("bundle", repo_root / STATIC_DIR))
    if plan.frontend_manifest:
        targets.append(("node_modules", repo_root / FRONTEND_DIR / "node_modules"))
    if plan.backend_manifest:
        targets.append(("venv", repo_root / ".venv"))
        for tool_name, executable in (
            (MNGR_TOOL_NAME, MNGR_EXECUTABLE),
            (TOOL_NAME, TOOL_NAME),
        ):
            tool_dir = _tool_environment_dir(executable, tool_name, runner)
            if tool_dir is None:
                sys.stderr.write(
                    f"note: could not locate the uv tool environment behind "
                    f"'{executable}' (not a uv tool on PATH), so it will not be "
                    "copied aside; a failed apply will have to rebuild it to recover.\n"
                )
                continue
            targets.append((f"tool-{tool_name}", tool_dir))
    return targets


# The snapshot names that together cover the backend environments a manifest
# change rebuilds. A rollback that could not put every one of them back has to
# re-resolve all three from the restored tree: a restored venv over a tool
# environment still built from the rolled-back-away tree is the
# ModuleNotFoundError-on-``mngr`` state that reads as recovered while nothing
# works.
ENVIRONMENT_SNAPSHOT_NAMES = frozenset(
    {"venv", f"tool-{MNGR_TOOL_NAME}", f"tool-{TOOL_NAME}"}
)


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


def _tool_location(script: Path, tool_name: str) -> tuple[Path, Path] | None:
    """Return ``(tool_dir, bin_dir)`` for the uv tool that owns console
    ``script``, resolved from the script's shebang; ``None`` when it cannot be
    confirmed. Resolved from the shebang rather than asked of uv, for two
    reasons: uv's default tool dir follows ``$HOME``, which is not the one the
    workspace was built under, and a venv console script must not masquerade
    as a tool."""
    try:
        shebang = script.read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    interpreter = shebang[2:].strip().split(" ", 1)[0]
    if not interpreter:
        return None
    parents = Path(interpreter).parents
    if len(parents) < 3:
        return None
    tool_dir = parents[2]
    if not (tool_dir / tool_name / RECEIPT).is_file():
        return None
    return tool_dir, script.parent


def _uv_tool_env(executable: str, tool_name: str, runner: Runner) -> dict:
    """The environment for a ``uv tool`` call, aimed at ``executable``'s own
    installation when we can confirm which that is."""
    env = dict(os.environ)
    found = runner.which(executable)
    location = _tool_location(Path(found), tool_name) if found is not None else None
    if location is None:
        sys.stderr.write(
            f"refresh: could not identify the uv tool behind '{executable}'"
            f" ({found or 'not on PATH'}); letting uv choose the tool directory,"
            " which may rebuild a copy that is not the one being run.\n"
        )
        return env
    env["UV_TOOL_DIR"] = str(location[0])
    env["UV_TOOL_BIN_DIR"] = str(location[1])
    return env


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


def _manifest_extras(tool_name: str, repo_root: Path) -> list[str]:
    """The ``--with-editable`` args ``PLUGIN_MANIFEST_PATH`` assigns to ``tool_name``.

    Empty for a tree that predates the manifest (a rollback re-refreshes the
    restored tree, and the receipt alone was that tree's whole answer).
    """
    manifest_path = repo_root / PLUGIN_MANIFEST_PATH
    if not manifest_path.is_file():
        return []
    manifest = tomllib.loads(manifest_path.read_text())
    tool = MANIFEST_TOOL_NAMES.get(tool_name, tool_name)
    extras: list[str] = []
    for entry in manifest.get("plugins", []):
        if tool in entry.get("tools", []):
            extras.extend(["--with-editable", str(repo_root / str(entry["path"]))])
    return extras


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
    executable: str,
    source_dir: str,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Re-resolve the installed ``executable``'s tool from its in-tree source,
    keeping the extras it was installed with and adding the merged tree's own
    plugin manifest.

    ``expend`` gates the expendable tag: a forward install may be shed (the
    rollback restores the tool-environment snapshot), a recovery install must
    keep the orchestrator's protection (``keep_protected``).
    """
    env = _uv_tool_env(executable, tool_name, runner)
    argv = [
        "uv",
        "tool",
        "install",
        "-e",
        source_dir,
        *_merge_extras(
            _tool_extras(tool_name, repo_root, runner, env),
            _manifest_extras(tool_name, repo_root),
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
) -> None:
    """Re-resolve the three backend environments from the current tree,
    mirroring ``build_workspace.sh``: the vendored ``mngr`` tool, the
    ``system-interface`` tool, and the workspace venv (``uv sync``).
    ``timeout`` bounds each of the three (the forward apply's budget; recovery
    passes none)."""
    _reinstall_tool(
        MNGR_TOOL_NAME, MNGR_EXECUTABLE, MNGR_DIR, repo_root, runner, expend, timeout
    )
    _reinstall_tool(
        TOOL_NAME, TOOL_NAME, SYSTEM_INTERFACE_DIR, repo_root, runner, expend, timeout
    )
    run_checked(
        runner,
        expend(["uv", "sync", "--all-packages", "--frozen"]),
        repo_root,
        "uv sync --all-packages --frozen",
        timeout=timeout,
    )
