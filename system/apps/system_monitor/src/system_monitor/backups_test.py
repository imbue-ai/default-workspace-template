"""Tests for the backups summary: each state the disk view can show, from a written events log."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system_monitor import backups

_NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _event(event_type: str, at: datetime, tick: str) -> str:
    return json.dumps(
        {
            "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
            "type": event_type,
            "event_id": f"evt-{tick}-{event_type}",
            "source": "backup",
            "tick_id": tick,
        }
    )


def _write(
    tmp_path: Path, lines: list[str], is_configured: bool = True
) -> tuple[Path, Path]:
    events_dir = tmp_path / "events" / "backup"
    events_dir.mkdir(parents=True)
    (events_dir / "events.jsonl").write_text("".join(line + "\n" for line in lines))
    restic_env = tmp_path / "restic.env"
    if is_configured:
        restic_env.write_text("RESTIC_REPOSITORY=x\n")
    return events_dir, restic_env


def _summary(
    tmp_path: Path, lines: list[str], is_configured: bool = True
) -> dict[str, object]:
    events_dir, restic_env = _write(tmp_path, lines, is_configured)
    return backups.summarize(events_dir, restic_env, tmp_path / "no-backup.toml", _NOW)


def test_without_a_repository_backups_are_not_configured(tmp_path: Path) -> None:
    summary = _summary(
        tmp_path, [_event("RESTIC_BACKUP_SUCCEEDED", _NOW, "t1")], is_configured=False
    )

    assert summary["state"] == "not_configured"
    assert summary["last_success_at"] is None
    assert str(summary["readme_path"]).endswith("host_backup/README.md")
    assert (
        backups.summarize(None, tmp_path / "restic.env", tmp_path / "no.toml", _NOW)[
            "state"
        ]
        == "not_configured"
    )


def test_a_recent_success_is_up_to_date_and_an_old_one_is_stale(tmp_path: Path) -> None:
    recent = _summary(
        tmp_path,
        [
            _event("BACKUP_STARTED", _NOW - timedelta(minutes=20), "t1"),
            _event("RESTIC_BACKUP_SUCCEEDED", _NOW - timedelta(minutes=14), "t1"),
        ],
    )
    assert recent["state"] == "up_to_date"
    assert recent["last_success_at"] == (_NOW - timedelta(minutes=14)).isoformat()
    assert recent["interval_seconds"] == 3600.0

    old = _summary(
        tmp_path / "old",
        [_event("RESTIC_BACKUP_SUCCEEDED", _NOW - timedelta(hours=5), "t1")],
    )
    assert old["state"] == "stale"


def test_a_failed_backup_is_failing_and_counts_toward_the_day_s_failures(
    tmp_path: Path,
) -> None:
    summary = _summary(
        tmp_path,
        [
            _event("RESTIC_BACKUP_SUCCEEDED", _NOW - timedelta(hours=3), "t1"),
            _event("RESTIC_BACKUP_FAILED", _NOW - timedelta(hours=2), "t2"),
            _event("RESTIC_BACKUP_FAILED", _NOW - timedelta(hours=30), "t0"),
            _event("BACKUP_STARTED", _NOW - timedelta(hours=1), "t3"),
            _event("RESTIC_BACKUP_FAILED", _NOW - timedelta(minutes=50), "t3"),
        ],
    )

    assert summary["state"] == "failing"
    assert summary["last_failure_at"] == (_NOW - timedelta(minutes=50)).isoformat()
    assert summary["last_success_at"] == (_NOW - timedelta(hours=3)).isoformat()
    assert summary["failures_last_day"] == 2


def test_a_backup_in_flight_is_running_and_missing_secrets_mean_not_configured(
    tmp_path: Path,
) -> None:
    running = _summary(
        tmp_path,
        [
            _event("RESTIC_BACKUP_SUCCEEDED", _NOW - timedelta(hours=1), "t1"),
            _event("BACKUP_STARTED", _NOW - timedelta(minutes=1), "t2"),
        ],
    )
    assert running["state"] == "running"
    assert running["last_success_at"] == (_NOW - timedelta(hours=1)).isoformat()

    skipped = _summary(
        tmp_path / "skipped",
        [
            _event(
                "TICK_SKIPPED_DUE_TO_MISSING_SECRETS", _NOW - timedelta(minutes=1), "t1"
            )
        ],
    )
    assert skipped["state"] == "not_configured"
