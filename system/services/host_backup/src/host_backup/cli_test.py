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
    _read_tail_lines,
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
        # restic ran and failed -- the ending exit code 1 has always stood for.
        pytest.param(
            (BackupEventType.SNAPSHOT_CREATED,),
            BackupEventType.RESTIC_BACKUP_FAILED,
            EXIT_BACKUP_FAILED,
            id="restic-failed",
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


def test_the_inflight_scan_never_reads_the_whole_events_log(tmp_path: Path) -> None:
    """The reported OOM kill: every event embeds the full stdout of the restic command
    it reports, so the log reaches gigabytes on an old workspace and reading it whole
    got `host-backup-now` killed by the watchdog before it did anything at all."""
    events_path = tmp_path / "events.jsonl"
    padding = "x" * 200_000
    with events_path.open("w") as fh:
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
    assert events_path.stat().st_size > 8 * 1024 * 1024

    read_bytes = 0
    real_open = Path.open

    def counting_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = real_open(self, *args, **kwargs)
        if self != events_path:
            return handle
        real_read = handle.read

        def counting_read(*read_args):  # type: ignore[no-untyped-def]
            nonlocal read_bytes
            chunk = real_read(*read_args)
            read_bytes += len(chunk)
            return chunk

        handle.read = counting_read  # type: ignore[method-assign]
        return handle

    Path.open = counting_open  # type: ignore[method-assign]
    try:
        pending = _scan_for_inflight_tick_ids(events_path, max_lines=200)
    finally:
        Path.open = real_open  # type: ignore[method-assign]

    # The tick still in flight is found -- it is the newest event, so the bounded
    # window always covers it -- without the file's size ever being read.
    assert pending == {"in-flight"}
    assert read_bytes <= 8 * 1024 * 1024
    assert read_bytes < events_path.stat().st_size


def test_the_tail_read_drops_the_line_its_window_cut_in_half(tmp_path: Path) -> None:
    # A window that starts mid-file lands mid-line; that fragment is not an event and
    # must not be handed to the caller as one.
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("first-line-is-long\nsecond\nthird\n")

    assert _read_tail_lines(events_path, max_lines=10, max_bytes=14) == ["second", "third"]
    assert _read_tail_lines(events_path, max_lines=10, max_bytes=10_000) == [
        "first-line-is-long",
        "second",
        "third",
    ]


def test_a_huge_restic_stdout_is_capped_at_both_ends_when_it_is_written(
    tmp_path: Path,
) -> None:
    """Nothing rotates the events log, so an uncapped progress stream grew it by
    megabytes a day. What an operator reads -- the opening lines and the final
    summary -- has to survive the cap, so both ends are kept."""
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

    assert json.loads((events_dir / "events.jsonl").read_text())["stdout"] == "repo locked"
