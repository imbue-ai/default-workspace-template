"""Boot-time convergence: overlay symlinks, env.d units, and record replay.

Two phases with different contracts:

- **Fast phase** (run synchronously by bootstrap BEFORE supervisord starts):
  applies the declarative overlay list (`scripts/env.d/overlay-paths.json`) so
  services never write to a rootfs path that should persist. Instant, no
  network.
- **Slow phase** (the `env-converge` supervisord one-shot; never blocks boot):
  re-runs every `scripts/env.d/<NNNN>-<name>.sh` unit in lexical order (units
  are idempotent with fast satisfied-checks -- there are NO marker files;
  version stability comes from the units' pins and the snapshot-pinned apt
  sources, so re-running is deterministic), then installs anything present in
  the record but missing from this rootfs.

Ordering rule (removal stickiness): on a rootfs that carries the identity
stamp, capture runs FIRST so deliberate removals stick; on a fresh rootfs
(image rebuild, restore onto a new container) converge runs first from the
record, then captures and stamps.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger

from env_converge.capture import capture_apt_state
from env_converge.capture import capture_base_identity
from env_converge.capture import capture_npm_state
from env_converge.capture import capture_uv_tool_state
from env_converge.data_types import ConvergeResult
from env_converge.data_types import OverlayApplyResult
from env_converge.data_types import UnitRunResult
from env_converge.events import EnvConvergeEventType
from env_converge.events import emit_event
from env_converge.record import EnvConvergeError
from env_converge.record import is_rootfs_stamped
from env_converge.record import read_apt_state
from env_converge.record import read_npm_state
from env_converge.record import read_uv_tool_state
from env_converge.record import stamp_rootfs
from env_converge.record import write_apt_state
from env_converge.record import write_base_identity
from env_converge.record import write_npm_state
from env_converge.record import write_uv_tool_state

_UNIT_TIMEOUT_SECONDS = 3600.0
_INSTALL_TIMEOUT_SECONDS = 900.0

DEFAULT_HOME_DIR = Path("/home/user")
DEFAULT_WORKSPACE_DIR = DEFAULT_HOME_DIR / "workspace"
DEFAULT_OVERLAY_DIR = DEFAULT_HOME_DIR / "overlay"


class OverlayEntryError(EnvConvergeError, ValueError):
    """Raised when an overlay-paths entry is not an absolute path."""

    def __init__(self, entry: object) -> None:
        super().__init__(f"Overlay entries must be absolute paths, got: {entry!r}")


def read_overlay_paths(workspace_dir: Path) -> list[Path]:
    """The declared overlay entries (absolute rootfs paths that must persist)."""
    overlay_file = workspace_dir / "scripts" / "env.d" / "overlay-paths.json"
    if not overlay_file.exists():
        return []
    entries = json.loads(overlay_file.read_text())
    paths: list[Path] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith("/"):
            raise OverlayEntryError(entry)
        paths.append(Path(entry))
    return paths


def apply_overlay_entry(absolute_path: Path, overlay_dir: Path) -> OverlayApplyResult:
    """Symlink one rootfs path into the persistent overlay tree.

    Adopt-and-move semantics: pre-existing rootfs content moves into the
    overlay on first application (preserved into user data); when both exist,
    the overlay copy wins and the rootfs version is discarded.
    """
    overlay_target = overlay_dir / str(absolute_path).lstrip("/")
    is_adopted = False
    if absolute_path.is_symlink():
        # Already applied (or user-managed); repointed idempotently below.
        pass
    elif absolute_path.exists():
        if overlay_target.exists():
            # Both exist: the overlay copy (user data) wins; the rootfs
            # version is regenerable image content and is discarded.
            if absolute_path.is_dir():
                shutil.rmtree(absolute_path)
            else:
                absolute_path.unlink()
        else:
            overlay_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(absolute_path), str(overlay_target))
            is_adopted = True
    if not overlay_target.exists():
        # Nothing to adopt anywhere yet: entries default to directories.
        overlay_target.mkdir(parents=True, exist_ok=True)
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    if absolute_path.is_symlink():
        absolute_path.unlink()
    absolute_path.symlink_to(overlay_target)
    return OverlayApplyResult(absolute_path=str(absolute_path), is_adopted=is_adopted)


def run_fast_phase(workspace_dir: Path, overlay_dir: Path) -> list[OverlayApplyResult]:
    """Apply every overlay entry. Instant and network-free; runs pre-services."""
    results: list[OverlayApplyResult] = []
    for absolute_path in read_overlay_paths(workspace_dir):
        result = apply_overlay_entry(absolute_path, overlay_dir)
        results.append(result)
        emit_event(
            EnvConvergeEventType.OVERLAY_APPLIED,
            {"path": result.absolute_path, "is_adopted": result.is_adopted},
        )
    return results


def _list_unit_scripts(workspace_dir: Path) -> list[Path]:
    unit_dir = workspace_dir / "scripts" / "env.d"
    if not unit_dir.is_dir():
        return []
    return sorted(path for path in unit_dir.iterdir() if path.name.endswith(".sh"))


def run_unit_scripts(workspace_dir: Path, overlay_dir: Path) -> list[UnitRunResult]:
    """Run every env.d unit in lexical order with failure isolation."""
    results: list[UnitRunResult] = []
    for unit_path in _list_unit_scripts(workspace_dir):
        start = time.monotonic()
        env = dict(os.environ)
        env["ENV_CONVERGE_OVERLAY_DIR"] = str(overlay_dir)
        env["ENV_CONVERGE_WORKSPACE_DIR"] = str(workspace_dir)
        try:
            completed = subprocess.run(
                ["bash", str(unit_path)],
                cwd=str(workspace_dir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=_UNIT_TIMEOUT_SECONDS,
            )
            exit_code = completed.returncode
            stderr_tail = completed.stderr[-2000:]
        except (OSError, subprocess.TimeoutExpired) as e:
            exit_code = -1
            stderr_tail = str(e)
        duration = time.monotonic() - start
        result = UnitRunResult(unit_name=unit_path.name, exit_code=exit_code, duration_seconds=duration)
        results.append(result)
        if exit_code == 0:
            emit_event(EnvConvergeEventType.UNIT_RUN, {"unit": unit_path.name, "duration_seconds": duration})
        else:
            # Failure isolation: one broken unit never blocks the others (or boot).
            logger.warning("env.d unit {} failed (rc={}): {}", unit_path.name, exit_code, stderr_tail)
            emit_event(
                EnvConvergeEventType.UNIT_FAILED,
                {"unit": unit_path.name, "exit_code": exit_code, "stderr_tail": stderr_tail},
            )
    return results


def _install_missing(
    kind: str,
    missing: list[str],
    build_command: "list[str]",
) -> tuple[list[str], list[str]]:
    """Install one batch; returns (installed, unavailable). Never raises."""
    if not missing:
        return [], []
    try:
        completed = subprocess.run(
            build_command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        emit_event(EnvConvergeEventType.PACKAGE_UNAVAILABLE, {"kind": kind, "packages": missing, "error": str(e)})
        return [], missing
    if completed.returncode != 0:
        logger.warning("Installing recorded {} packages failed: {}", kind, completed.stderr[-1000:])
        emit_event(
            EnvConvergeEventType.PACKAGE_UNAVAILABLE,
            {"kind": kind, "packages": missing, "stderr_tail": completed.stderr[-2000:]},
        )
        return [], missing
    emit_event(EnvConvergeEventType.PACKAGE_INSTALLED, {"kind": kind, "packages": missing})
    return missing, []


def install_missing_from_record(record_dir: Path) -> tuple[list[str], list[str], list[str], list[str]]:
    """Install record entries absent from this rootfs.

    Returns (installed_apt, installed_npm, installed_uv, unavailable). The apt
    install-set is the recorded manual set (dependencies follow via apt at the
    pinned snapshot timestamp, so versions are deterministic without pinning
    each name).
    """
    unavailable: list[str] = []

    recorded_apt = read_apt_state(record_dir)
    installed_apt: list[str] = []
    if recorded_apt is not None:
        current = capture_apt_state()
        missing_apt = sorted(set(recorded_apt.manual_packages) - set(current.version_by_package))
        if missing_apt:
            subprocess.run(["apt-get", "update", "-qq"], check=False, timeout=_INSTALL_TIMEOUT_SECONDS)
        installed_apt, unavailable_apt = _install_missing(
            "apt", missing_apt, ["apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing_apt]
        )
        unavailable.extend(unavailable_apt)

    recorded_npm = read_npm_state(record_dir)
    installed_npm: list[str] = []
    if recorded_npm is not None:
        current_npm = capture_npm_state()
        missing_npm = sorted(set(recorded_npm.version_by_package) - set(current_npm.version_by_package))
        npm_specs = [f"{name}@{recorded_npm.version_by_package[name]}" for name in missing_npm]
        installed_npm, unavailable_npm = _install_missing("npm", npm_specs, ["npm", "install", "-g", *npm_specs])
        unavailable.extend(unavailable_npm)

    recorded_uv = read_uv_tool_state(record_dir)
    installed_uv: list[str] = []
    if recorded_uv is not None:
        current_uv = capture_uv_tool_state()
        missing_uv = sorted(set(recorded_uv.version_by_tool) - set(current_uv.version_by_tool))
        for tool in missing_uv:
            spec = f"{tool}=={recorded_uv.version_by_tool[tool]}"
            installed, not_installed = _install_missing("uv_tool", [spec], ["uv", "tool", "install", spec])
            installed_uv.extend(installed)
            unavailable.extend(not_installed)

    return installed_apt, installed_npm, installed_uv, unavailable


def capture_all(record_dir: Path, snapshot_timestamp: str, workspace_dir: Path) -> None:
    """Capture every source's actual state into the record ("dpkg is truth")."""
    write_base_identity(record_dir, capture_base_identity(snapshot_timestamp, workspace_dir))
    write_apt_state(record_dir, capture_apt_state())
    write_npm_state(record_dir, capture_npm_state())
    write_uv_tool_state(record_dir, capture_uv_tool_state())
    emit_event(EnvConvergeEventType.STATE_CAPTURED, {"snapshot_timestamp": snapshot_timestamp})


def read_pinned_snapshot_timestamp(workspace_dir: Path) -> str:
    """The committed apt snapshot timestamp the workspace's sources pin to."""
    return (workspace_dir / ".mngr" / "apt-snapshot-timestamp").read_text().strip()


def run_slow_phase(
    record_dir: Path,
    workspace_dir: Path,
    overlay_dir: Path,
) -> ConvergeResult:
    """The supervisord one-shot: units, record replay, and re-capture."""
    is_fresh = not is_rootfs_stamped()
    snapshot_timestamp = read_pinned_snapshot_timestamp(workspace_dir)

    # Known rootfs: capture FIRST so deliberate removals made since the last
    # capture stick instead of being resurrected by the replay below.
    if not is_fresh:
        capture_all(record_dir, snapshot_timestamp, workspace_dir)

    unit_results = run_unit_scripts(workspace_dir, overlay_dir)
    installed_apt, installed_npm, installed_uv, unavailable = install_missing_from_record(record_dir)

    # Reality changed (units ran, packages installed) -- or this is a fresh
    # rootfs whose record predates it; either way, re-capture and stamp.
    capture_all(record_dir, snapshot_timestamp, workspace_dir)
    stamp_rootfs()

    result = ConvergeResult(
        overlay_results=(),
        unit_results=tuple(unit_results),
        installed_apt_packages=tuple(installed_apt),
        installed_npm_packages=tuple(installed_npm),
        installed_uv_tools=tuple(installed_uv),
        unavailable_packages=tuple(unavailable),
        is_fresh_rootfs=is_fresh,
    )
    emit_event(
        EnvConvergeEventType.CONVERGE_COMPLETED,
        {
            "is_fresh_rootfs": is_fresh,
            "unit_count": len(unit_results),
            "installed_apt": installed_apt,
            "installed_npm": installed_npm,
            "installed_uv": installed_uv,
            "unavailable": unavailable,
        },
    )
    return result
