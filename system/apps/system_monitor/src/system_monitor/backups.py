"""The backup service's state in a few words, read from its events and settings; it never runs restic
(``docs/system/specs/system-monitor.md`` section 4.5)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from host_backup import config as backup_config
from host_backup import events as backup_events
from imbue.imbue_common.event_envelope import parse_iso_timestamp

from system_monitor.tail import read_tail

README_PATH: Final[str] = "/home/user/workspace/system/services/host_backup/README.md"
FAILURES_WINDOW: Final[timedelta] = timedelta(hours=24)
_FAILURE_TYPES: Final[frozenset[str]] = frozenset(
    {
        backup_events.BackupEventType.RESTIC_BACKUP_FAILED.value,
        backup_events.BackupEventType.SNAPSHOT_FAILED.value,
        backup_events.BackupEventType.TICK_ERROR.value,
    }
)
_TAIL_BYTES: Final[int] = 1024 * 1024
# A backup older than this many intervals is stale.
_STALE_INTERVALS: Final[float] = 2.0


def summarize(
    events_dir: Path | None,
    restic_env_path: Path = backup_config.RESTIC_ENV_PATH,
    backup_toml_path: Path = backup_config.BACKUP_TOML_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The summary the disk view shows: the state, the last success and failure, the cadence, and recent failures."""
    moment = now or datetime.now(timezone.utc)
    interval = backup_config.load_backup_config(
        backup_toml_path
    ).backup_interval_seconds
    summary: dict[str, Any] = {
        "state": "not_configured",
        "last_success_at": None,
        "last_failure_at": None,
        "interval_seconds": interval,
        "failures_last_day": 0,
        "readme_path": README_PATH,
    }
    events_path = (
        events_dir / backup_events.EVENTS_FILENAME if events_dir is not None else None
    )
    if events_path is None or not restic_env_path.exists():
        return summary
    ticks = backup_events.scan_recent_ticks_across_rotation(events_path)
    success_at = _event_time(ticks.newest_success_event)
    terminal = ticks.newest_terminal_event
    terminal_type = terminal.get("type") if terminal else None
    summary["last_success_at"] = success_at.isoformat() if success_at else None
    summary["failures_last_day"] = _failures_since(
        events_path, moment - FAILURES_WINDOW
    )
    if terminal_type in _FAILURE_TYPES:
        failed_at = _event_time(terminal)
        summary["last_failure_at"] = failed_at.isoformat() if failed_at else None
    if (
        terminal_type
        == backup_events.BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value
    ):
        return summary
    if ticks.inflight_tick_id is not None:
        summary["state"] = "running"
    elif terminal_type in _FAILURE_TYPES:
        summary["state"] = "failing"
    elif success_at is not None and moment - success_at <= timedelta(
        seconds=interval * _STALE_INTERVALS
    ):
        summary["state"] = "up_to_date"
    else:
        summary["state"] = "stale"
    return summary


def _event_time(event: dict[str, object] | None) -> datetime | None:
    if event is None:
        return None
    return parse_iso_timestamp(str(event.get("timestamp", "")))


def _failures_since(events_path: Path, since: datetime) -> int:
    try:
        lines = read_tail(events_path, _TAIL_BYTES)
    except OSError:
        return 0
    count = 0
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") not in _FAILURE_TYPES:
            continue
        at = parse_iso_timestamp(str(event.get("timestamp", "")))
        if at is not None and at >= since:
            count += 1
    return count
