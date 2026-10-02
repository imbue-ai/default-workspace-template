"""Installing the recorder's once-a-minute cron entry.

The entry is code-owned, so like the bootstrap's update-apply-recover guard it goes straight into /etc/cron.d/ and
never into the user-editable data/.state/cron.d/. /etc/cron.d/ is on the container rootfs, so the app writes it each
time it starts, which it does at every boot (its program autostarts before the shell stops it); that also keeps the
entry pointing at the app's current install path. The line skips with_agent_env.sh: the recorder needs no agent
credentials, and the history should keep recording while the agent environment is broken.
"""

from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure

CRON_ENTRY_NAME: Final[str] = "activity-memory-history"
SYSTEM_CRON_DIR: Final[Path] = Path("/etc/cron.d")
WORKSPACE_ROOT: Final[Path] = Path("/home/user/workspace")
RECORDER_LOG_PATH: Final[Path] = Path("/var/log/supervisor/activity-record-memory.log")


@pure
def cron_entry_text(recorder_path: Path, workspace_root: Path, log_path: Path) -> str:
    return (
        "# System Monitor's memory-over-time recorder: one reading a minute (system/apps/activity, cron_entry.py).\n"
        f"* * * * * root cd {workspace_root} && {recorder_path} >> {log_path} 2>&1\n"
    )


def _write_if_changed(path: Path, text: str) -> None:
    if path.is_file() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o644)


def install_cron_entry(text: str, system_cron_dir: Path) -> None:
    """Write the entry where cron reads it. A failure is logged, not raised: the app still serves, and the chart says
    recording has paused once its readings go stale."""
    try:
        _write_if_changed(system_cron_dir / CRON_ENTRY_NAME, text)
    except OSError as e:
        logger.warning("Could not install the memory recorder's cron entry: {}", e)
