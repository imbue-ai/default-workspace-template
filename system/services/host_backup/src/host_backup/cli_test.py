"""Unit tests for the `host-backup-now` waiters and exit-code contract."""

from __future__ import annotations

import json
import time
from pathlib import Path

from host_backup.cli import (
    EXIT_BACKUP_FAILED,
    EXIT_BACKUP_SUCCEEDED,
    EXIT_BACKUPS_NOT_CONFIGURED,
    _exit_code_for_completion,
    _scan_for_inflight_tick_ids,
    _wait_for_next_completion,
)
from host_backup.events import BackupEventType, make_event, write_event

# Long enough that a waiter which fails to recognise a terminal event is
# unambiguously stuck rather than merely slow, short enough that the test still
# finishes if that regression reappears.
_GENEROUS_TIMEOUT_SECONDS = 3.0

# A prompt return happens on the waiter's first poll, before it ever sleeps.
_PROMPT_RETURN_SECONDS = 1.0


def _write_tick(events_dir: Path, *types: BackupEventType, tick_id: str) -> None:
    for event_type in types:
        write_event(events_dir, make_event(event_type, tick_id=tick_id))


def _wait_from_start(events_path: Path) -> tuple[dict[str, object] | None, float]:
    """Wait for a completion over the whole existing file, returning it and the elapsed time."""
    start = time.monotonic()
    completion = _wait_for_next_completion(
        events_path, 0, start + _GENEROUS_TIMEOUT_SECONDS
    )
    return completion, time.monotonic() - start


def test_wait_returns_promptly_when_the_tick_skips_for_missing_secrets(
    tmp_path: Path,
) -> None:
    """The reported hang: a tick that never reaches restic must still end the wait."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
        tick_id="tick-skip",
    )
    completion, elapsed = _wait_from_start(tmp_path / "events.jsonl")
    assert completion is not None
    assert (
        completion["type"] == BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value
    )
    assert elapsed < _PROMPT_RETURN_SECONDS
    assert _exit_code_for_completion(completion) == EXIT_BACKUPS_NOT_CONFIGURED


def test_wait_returns_promptly_when_the_snapshot_step_fails(tmp_path: Path) -> None:
    """A snapshot failure aborts the tick before restic runs, so it is terminal too."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.SNAPSHOT_FAILED,
        tick_id="tick-snapshot",
    )
    completion, elapsed = _wait_from_start(tmp_path / "events.jsonl")
    assert completion is not None
    assert completion["type"] == BackupEventType.SNAPSHOT_FAILED.value
    assert elapsed < _PROMPT_RETURN_SECONDS
    assert _exit_code_for_completion(completion) == EXIT_BACKUP_FAILED


def test_wait_returns_promptly_when_the_tick_raises(tmp_path: Path) -> None:
    """An unhandled error is recorded as TICK_ERROR and never followed by a restic event."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.TICK_ERROR,
        tick_id="tick-error",
    )
    completion, elapsed = _wait_from_start(tmp_path / "events.jsonl")
    assert completion is not None
    assert completion["type"] == BackupEventType.TICK_ERROR.value
    assert elapsed < _PROMPT_RETURN_SECONDS
    assert _exit_code_for_completion(completion) == EXIT_BACKUP_FAILED


def test_wait_returns_the_succeeded_event_and_ignores_mid_tick_events(
    tmp_path: Path,
) -> None:
    """The happy path still resolves on RESTIC_BACKUP_SUCCEEDED, not on a mid-tick event."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.SNAPSHOT_CREATED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-ok",
    )
    completion, elapsed = _wait_from_start(tmp_path / "events.jsonl")
    assert completion is not None
    assert completion["type"] == BackupEventType.RESTIC_BACKUP_SUCCEEDED.value
    assert elapsed < _PROMPT_RETURN_SECONDS
    assert _exit_code_for_completion(completion) == EXIT_BACKUP_SUCCEEDED


def test_wait_times_out_when_the_tick_never_resolves(tmp_path: Path) -> None:
    """A tick that emits nothing terminal still has to hit the deadline and report it."""
    _write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-hung")
    events_path = tmp_path / "events.jsonl"
    completion = _wait_for_next_completion(events_path, 0, time.monotonic() + 0.1)
    assert completion is None


def test_wait_ignores_events_already_present_before_the_trigger(tmp_path: Path) -> None:
    """Only events appended after the config bump count as this run's completion."""
    _write_tick(
        tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-previous"
    )
    events_path = tmp_path / "events.jsonl"
    completion = _wait_for_next_completion(
        events_path, events_path.stat().st_size, time.monotonic() + 0.1
    )
    assert completion is None


def test_inflight_scan_treats_every_tick_ending_as_finished(tmp_path: Path) -> None:
    """A tick that ended without a restic event is not in flight, so nothing waits on it."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.SNAPSHOT_FAILED,
        tick_id="tick-snapshot",
    )
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
        tick_id="tick-skip",
    )
    _write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-running")
    pending = _scan_for_inflight_tick_ids(tmp_path / "events.jsonl", max_lines=200)
    assert pending == {"tick-running"}


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
    assert _scan_for_inflight_tick_ids(events_path, max_lines=200) == set()
