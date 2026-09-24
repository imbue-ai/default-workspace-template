"""Unit tests for how events are stored: the per-field cap `write_event` applies and
the rotation that bounds the log."""

from __future__ import annotations

import json
from pathlib import Path

from imbue.imbue_common.logging import ROTATED_JSONL_PATTERN

from host_backup.events import (
    EVENTS_LOG_ROTATION_BYTES,
    MAX_ROTATED_EVENTS_LOGS,
    BackupEventType,
    make_event,
    rotate_events_log_if_over,
    write_event,
)


def test_a_huge_restic_stdout_is_capped_at_both_ends_when_it_is_written(
    tmp_path: Path,
) -> None:
    """An uncapped progress stream grew the events log by megabytes a day. What an
    operator reads -- the opening lines and the final summary -- has to survive the
    cap, so both ends are kept."""
    events_dir = tmp_path / "events"
    stdout = "HEAD-MARKER\n" + ("progress tick\n" * 200_000) + "TAIL-SUMMARY"
    write_event(
        events_dir,
        make_event(
            BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="t1", stdout=stdout
        ),
    )

    written = (events_dir / "events.jsonl").read_text()
    stored = json.loads(written)["stdout"]
    assert len(written) < len(stdout) / 100
    assert stored.startswith("HEAD-MARKER")
    assert stored.endswith("TAIL-SUMMARY")
    assert "characters dropped" in stored


def test_an_ordinary_event_is_stored_verbatim(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    write_event(
        events_dir,
        make_event(
            BackupEventType.RESTIC_BACKUP_FAILED, tick_id="t1", stdout="repo locked"
        ),
    )

    assert (
        json.loads((events_dir / "events.jsonl").read_text())["stdout"] == "repo locked"
    )


def _rotated_logs(events_dir: Path) -> list[Path]:
    return sorted(
        child
        for child in events_dir.iterdir()
        if ROTATED_JSONL_PATTERN.match(child.name)
    )


def _write_sparse_log(events_path: Path, size: int) -> None:
    with events_path.open("wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\n")


def test_a_log_over_the_threshold_is_moved_aside_and_the_oldest_rotations_dropped(
    tmp_path: Path,
) -> None:
    oldest = tmp_path / "events.jsonl.20250101000000000000"
    older = tmp_path / "events.jsonl.20250102000000000000"
    for rotated in (oldest, older):
        rotated.write_text("{}\n")
    _write_sparse_log(tmp_path / "events.jsonl", EVENTS_LOG_ROTATION_BYTES)

    rotate_events_log_if_over(tmp_path)

    assert not (tmp_path / "events.jsonl").exists()
    rotated_logs = _rotated_logs(tmp_path)
    assert len(rotated_logs) == MAX_ROTATED_EVENTS_LOGS
    assert oldest not in rotated_logs
    assert rotated_logs[-1].stat().st_size == EVENTS_LOG_ROTATION_BYTES


def test_a_log_under_the_threshold_is_left_alone(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_sparse_log(events_path, EVENTS_LOG_ROTATION_BYTES - 1)

    rotate_events_log_if_over(tmp_path)

    assert events_path.stat().st_size == EVENTS_LOG_ROTATION_BYTES - 1
    assert _rotated_logs(tmp_path) == []
