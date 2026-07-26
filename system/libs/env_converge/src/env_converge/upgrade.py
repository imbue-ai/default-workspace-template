"""The explicit upgrade operation: advance the pinned snapshot timestamp.

Versions change ONLY here (never on restore, restart, or re-converge): the
workspace's apt sources re-render at the repo's committed timestamp, apt
full-upgrades against the new frozen universe, the env.d units re-run (their
pins may have advanced with the template), and the record re-captures. Run as
part of the update-self flow after the template merge lands the new
`.mngr/apt-snapshot-timestamp`.
"""

import subprocess
from pathlib import Path

from loguru import logger

from env_converge.capture import parse_dpkg_versions
from env_converge.converge import (
    capture_all,
    read_pinned_snapshot_timestamp,
    run_unit_scripts,
)
from env_converge.events import EnvConvergeEventType, emit_event
from env_converge.record import EnvConvergeError, read_apt_state

_APT_TIMEOUT_SECONDS = 1800.0


class UpgradeCommandError(EnvConvergeError, RuntimeError):
    """Raised when a step of the upgrade fails."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"Upgrade step failed ({step}): {detail}")


def _run_upgrade_step(step: str, command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_APT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpgradeCommandError(step, str(e)) from e
    if completed.returncode != 0:
        raise UpgradeCommandError(step, completed.stderr.strip()[-1000:])


def run_upgrade(
    record_dir: Path, workspace_dir: Path, overlay_dir: Path
) -> dict[str, str]:
    """Advance to the committed timestamp; returns the package-version deltas."""
    target_timestamp = read_pinned_snapshot_timestamp(workspace_dir)
    recorded = read_apt_state(record_dir)
    versions_before = dict(recorded.version_by_package) if recorded is not None else {}
    emit_event(
        EnvConvergeEventType.UPGRADE_STARTED, {"target_timestamp": target_timestamp}
    )

    # Re-render the pinned sources at the (possibly new) committed timestamp,
    # then move every package to its version in that frozen universe.
    _run_upgrade_step(
        "write_apt_sources",
        [
            "bash",
            str(workspace_dir / "system" / "scripts" / "write_apt_sources.sh"),
            target_timestamp,
        ],
    )
    _run_upgrade_step("apt_update", ["apt-get", "update", "-qq"])
    _run_upgrade_step("apt_full_upgrade", ["apt-get", "full-upgrade", "-y", "-qq"])

    # Units re-run at the new pins (the template merge may have bumped them),
    # then the record re-captures against the new base.
    run_unit_scripts(workspace_dir, overlay_dir)
    capture_all(record_dir, target_timestamp, workspace_dir)

    upgraded = read_apt_state(record_dir)
    versions_after = dict(upgraded.version_by_package) if upgraded is not None else {}
    deltas = {
        package: f"{versions_before.get(package, '(absent)')} -> {version}"
        for package, version in sorted(versions_after.items())
        if versions_before.get(package) != version
    }
    logger.info(
        "Upgrade to {} changed {} package versions", target_timestamp, len(deltas)
    )
    emit_event(
        EnvConvergeEventType.UPGRADE_COMPLETED,
        {
            "target_timestamp": target_timestamp,
            "changed_count": len(deltas),
            "deltas": deltas,
        },
    )
    return deltas


def compute_version_deltas(before_output: str, after_output: str) -> dict[str, str]:
    """Pure delta computation over two dpkg-query outputs (exposed for tests)."""
    before = parse_dpkg_versions(before_output)
    after = parse_dpkg_versions(after_output)
    return {
        package: f"{before.get(package, '(absent)')} -> {version}"
        for package, version in sorted(after.items())
        if before.get(package) != version
    }
