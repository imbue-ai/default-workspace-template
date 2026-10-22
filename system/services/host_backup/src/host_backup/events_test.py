"""Unit tests for how events are stored and read back: the per-field cap `write_event`
applies, the rotation that bounds the log, and the scan for the tick in flight."""

from __future__ import annotations

import json
from pathlib import Path

from imbue.imbue_common.logging import ROTATED_JSONL_PATTERN

from host_backup.events import (
    _TAIL_READ_MAX_BYTES,
    EVENTS_LOG_ROTATION_BYTES,
    MAX_ROTATED_EVENTS_LOGS,
    BackupEventType,
    _read_tail_lines,
    find_inflight_tick_id,
    make_event,
    record_abandoned_tick,
    rotate_events_log_if_over,
    write_event,
)
from host_backup.testing import write_tick


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


def test_inflight_scan_treats_every_tick_ending_as_finished(tmp_path: Path) -> None:
    """A tick that ended without a restic event is not in flight, so nothing waits on it."""
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.SNAPSHOT_FAILED,
        tick_id="tick-snapshot",
    )
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
        tick_id="tick-skip",
    )
    write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-running")
    pending = find_inflight_tick_id(tmp_path / "events.jsonl")
    assert pending == "tick-running"


def test_a_tick_that_never_finished_is_not_in_flight_once_a_later_tick_started(
    tmp_path: Path,
) -> None:
    """A tick killed mid-restic (an OOM shed, a services restart) never emits a terminal
    event. The runner runs one tick at a time, so a later tick starting means it is dead."""
    write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-killed")
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )
    assert find_inflight_tick_id(tmp_path / "events.jsonl") is None


def test_inflight_scan_ignores_foreign_event_sources(tmp_path: Path) -> None:
    """Only `backup`-sourced events are considered, so a shared log cannot wedge the wait."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "type": BackupEventType.BACKUP_STARTED.value,
                "source": "something-else",
                "tick_id": "tick-foreign",
            }
        )
        + "\n"
    )
    assert find_inflight_tick_id(events_path) is None


def test_the_inflight_scan_never_reads_the_whole_events_log(tmp_path: Path) -> None:
    """The reported OOM kill: every event embeds the full stdout of the restic command
    it reports, so the log reaches gigabytes on an old workspace and reading it whole
    got `host-backup-now` killed by the watchdog before it did anything at all."""
    events_path = tmp_path / "events.jsonl"
    padding = "x" * 200_000
    with events_path.open("w") as fh:
        fh.write(
            json.dumps(
                {
                    "source": "backup",
                    "type": BackupEventType.BACKUP_STARTED.value,
                    "tick_id": "older-than-the-window",
                }
            )
            + "\n"
        )
        for index in range(50):
            fh.write(
                json.dumps(
                    {
                        "source": "backup",
                        "type": BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
                        "tick_id": f"old-{index}",
                        "stdout": padding,
                    }
                )
                + "\n"
            )
    assert events_path.stat().st_size > _TAIL_READ_MAX_BYTES

    # The log is 51 lines, so `max_lines` excludes nothing: the only thing that can
    # keep the tick at the top of the file out of this answer is a window that never
    # reached it. A scan that read the file whole reports it as in flight.
    assert find_inflight_tick_id(events_path) is None

    with events_path.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "source": "backup",
                    "type": BackupEventType.BACKUP_STARTED.value,
                    "tick_id": "in-flight",
                }
            )
            + "\n"
        )
    assert find_inflight_tick_id(events_path) == "in-flight"


def test_the_tail_read_drops_the_line_its_window_cut_in_half(tmp_path: Path) -> None:
    # A window that starts mid-file lands mid-line; that fragment is not an event and
    # must not be handed to the caller as one.
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("first-line-is-long\nsecond\nthird\n")

    assert _read_tail_lines(events_path, max_lines=10, max_bytes=14) == [
        "second",
        "third",
    ]
    assert _read_tail_lines(events_path, max_lines=10, max_bytes=10_000) == [
        "first-line-is-long",
        "second",
        "third",
    ]


def test_a_restarted_service_records_the_tick_it_was_killed_out_of(
    tmp_path: Path,
) -> None:
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-done",
    )
    write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-killed")

    record_abandoned_tick(tmp_path)

    last = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert (last["type"], last["tick_id"]) == (
        BackupEventType.TICK_ABANDONED.value,
        "tick-killed",
    )
    assert find_inflight_tick_id(tmp_path / "events.jsonl") is None


def test_a_service_that_stopped_between_ticks_records_nothing(tmp_path: Path) -> None:
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-done",
    )
    before = (tmp_path / "events.jsonl").read_text()

    record_abandoned_tick(tmp_path)

    assert (tmp_path / "events.jsonl").read_text() == before
