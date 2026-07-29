"""Unit tests for the `host-backup-now` waiters and exit-code contract."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

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


def _write_tick(events_dir: Path, *types: BackupEventType, tick_id: str) -> None:
    for event_type in types:
        write_event(events_dir, make_event(event_type, tick_id=tick_id))


@pytest.mark.parametrize(
    ("mid_tick_events", "terminal_event", "expected_exit_code"),
    [
        # The reported hang: a tick that never reaches restic.
        pytest.param(
            (),
            BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
            EXIT_BACKUPS_NOT_CONFIGURED,
            id="not-configured",
        ),
        # A snapshot failure aborts the tick before restic runs.
        pytest.param(
            (),
            BackupEventType.SNAPSHOT_FAILED,
            EXIT_BACKUP_FAILED,
            id="snapshot-failed",
        ),
        # An unhandled error, recorded by the loop's outer handler.
        pytest.param(
            (), BackupEventType.TICK_ERROR, EXIT_BACKUP_FAILED, id="tick-error"
        ),
        # The happy path, which must resolve on the restic outcome rather than on
        # the mid-tick event preceding it.
        pytest.param(
            (BackupEventType.SNAPSHOT_CREATED,),
            BackupEventType.RESTIC_BACKUP_SUCCEEDED,
            EXIT_BACKUP_SUCCEEDED,
            id="succeeded",
        ),
    ],
)
def test_wait_ends_on_every_tick_ending_and_maps_it_to_an_exit_code(
    tmp_path: Path,
    mid_tick_events: tuple[BackupEventType, ...],
    terminal_event: BackupEventType,
    expected_exit_code: int,
) -> None:
    """Every way a tick can end has to end the wait and pick out its own exit code."""
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        *mid_tick_events,
        terminal_event,
        tick_id="tick-under-test",
    )
    completion = _wait_for_next_completion(
        tmp_path / "events.jsonl", 0, time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )
    assert completion is not None
    assert completion["type"] == terminal_event.value
    assert _exit_code_for_completion(completion) == expected_exit_code


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
