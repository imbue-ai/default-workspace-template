"""Unit tests for the `host-backup-now` waiters and exit-code contract."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from host_backup.cli import (
    _TAIL_READ_MAX_BYTES,
    EXIT_BACKUP_FAILED,
    EXIT_BACKUP_SUCCEEDED,
    EXIT_BACKUPS_NOT_CONFIGURED,
    EXIT_NO_COMPLETION_OBSERVED,
    _EventsLogFollower,
    _exit_code_for_completion,
    _read_tail_lines,
    _scan_for_inflight_tick_id,
    _wait_for_next_completion,
    _wait_for_tick_to_end,
    backup_now_main,
)
from host_backup.config import BACKUP_TOML_PATH
from host_backup.events import (
    EVENTS_LOG_ROTATION_BYTES,
    BackupEventType,
    make_event,
    rotate_events_log_if_over,
    write_event,
)

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
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        *mid_tick_events,
        terminal_event,
        tick_id="tick-under-test",
    )
    completion = _wait_for_next_completion(
        follower, time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )
    assert completion is not None
    assert completion["type"] == terminal_event.value
    assert _exit_code_for_completion(completion) == expected_exit_code


def test_wait_times_out_when_the_tick_never_resolves(tmp_path: Path) -> None:
    """A tick that emits nothing terminal still has to hit the deadline and report it."""
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    _write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-hung")
    completion = _wait_for_next_completion(follower, time.monotonic() + 0.1)
    assert completion is None


def test_wait_ignores_events_already_present_before_the_trigger(tmp_path: Path) -> None:
    """Only events appended after the config bump count as this run's completion."""
    _write_tick(
        tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-previous"
    )
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    completion = _wait_for_next_completion(follower, time.monotonic() + 0.1)
    assert completion is None


@pytest.mark.parametrize(
    "new_log_filler_bytes",
    [
        pytest.param(0, id="new-log-shorter-than-the-old-offset"),
        pytest.param(
            EVENTS_LOG_ROTATION_BYTES, id="new-log-longer-than-the-old-offset"
        ),
    ],
)
def test_wait_follows_the_log_across_a_rotation(
    tmp_path: Path, new_log_filler_bytes: int
) -> None:
    """The runner rotates the log at the top of the very tick the command triggered,
    so that tick's events all land in a fresh file. A waiter that kept a byte offset
    into the path read the new file from the old one's end -- nothing at all when the
    new file is shorter, only what follows the tick when it is longer -- and timed
    out on a tick that had finished."""
    events_path = tmp_path / "events.jsonl"
    _write_tick(
        tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-previous"
    )
    with events_path.open("r+b") as fh:
        fh.seek(EVENTS_LOG_ROTATION_BYTES)
        fh.write(b"\n")
    follower = _EventsLogFollower(events_path)

    rotate_events_log_if_over(tmp_path)
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
        tick_id="tick-triggered",
    )
    with events_path.open("ab") as fh:
        fh.write(b"\0" * new_log_filler_bytes + b"\n")

    completion = _wait_for_next_completion(
        follower, time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )

    assert completion is not None
    assert completion["tick_id"] == "tick-triggered"
    assert (
        completion["type"] == BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value
    )


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
    pending = _scan_for_inflight_tick_id(tmp_path / "events.jsonl", max_lines=200)
    assert pending == "tick-running"


def test_a_tick_that_never_finished_is_not_in_flight_once_a_later_tick_started(
    tmp_path: Path,
) -> None:
    """A tick killed mid-restic (an OOM shed, a services restart) never emits a terminal
    event. The runner runs one tick at a time, so a later tick starting means it is dead."""
    _write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-killed")
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )
    assert _scan_for_inflight_tick_id(tmp_path / "events.jsonl", max_lines=200) is None


@pytest.fixture
def backup_events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The events dir `host-backup-now` resolves, with backup.toml's relative path under
    `tmp_path`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state" / "events" / "backup"


def test_backup_now_triggers_nothing_when_the_inflight_tick_outlasts_the_timeout(
    tmp_path: Path, backup_events_dir: Path
) -> None:
    """A tick triggered after the deadline is one nobody waits for, so none is triggered."""
    _write_tick(backup_events_dir, BackupEventType.BACKUP_STARTED, tick_id="tick-busy")

    result = CliRunner().invoke(backup_now_main, ["--timeout", "0.2"])

    assert result.exit_code == EXIT_NO_COMPLETION_OBSERVED
    assert not (tmp_path / BACKUP_TOML_PATH).exists()


def test_backup_now_triggers_past_a_tick_that_never_finished(
    tmp_path: Path, backup_events_dir: Path
) -> None:
    _write_tick(
        backup_events_dir, BackupEventType.BACKUP_STARTED, tick_id="tick-killed"
    )
    _write_tick(
        backup_events_dir,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )

    result = CliRunner().invoke(backup_now_main, ["--timeout", "0.2"])

    # No runner is reading the bumped config here, so the triggered tick never ends.
    assert result.exit_code == EXIT_NO_COMPLETION_OBSERVED
    assert (tmp_path / BACKUP_TOML_PATH).exists()


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
    assert _scan_for_inflight_tick_id(events_path, max_lines=200) is None


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
    assert _scan_for_inflight_tick_id(events_path, max_lines=200) is None

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
    assert _scan_for_inflight_tick_id(events_path, max_lines=200) == "in-flight"


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


def test_the_inflight_wait_ends_on_its_own_tick_only(tmp_path: Path) -> None:
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    _write_tick(tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-other")
    assert not _wait_for_tick_to_end(follower, "tick-busy", time.monotonic() + 0.1)

    _write_tick(tmp_path, BackupEventType.RESTIC_BACKUP_FAILED, tick_id="tick-busy")
    assert _wait_for_tick_to_end(
        follower, "tick-busy", time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )


def test_the_inflight_wait_follows_a_tick_that_started_after_the_one_it_waited_for(
    tmp_path: Path,
) -> None:
    """The waited tick's runner was restarted mid-wait: its startup tick is the one in
    flight now, and the dead tick never ends."""
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    _write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )
    assert _wait_for_tick_to_end(
        follower, "tick-killed", time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    ("ticks", "expected_exit_code", "expected_report"),
    [
        pytest.param(
            (),
            0,
            {"inflight_tick_id": None, "finished": True},
            id="nothing-in-flight",
        ),
        pytest.param(
            (("tick-busy", (BackupEventType.BACKUP_STARTED,)),),
            EXIT_NO_COMPLETION_OBSERVED,
            {"inflight_tick_id": "tick-busy", "finished": False},
            id="still-running-at-the-timeout",
        ),
    ],
)
def test_wait_only_reports_the_inflight_tick_and_triggers_nothing(
    tmp_path: Path,
    backup_events_dir: Path,
    ticks: tuple[tuple[str, tuple[BackupEventType, ...]], ...],
    expected_exit_code: int,
    expected_report: dict[str, object],
) -> None:
    for tick_id, types in ticks:
        _write_tick(backup_events_dir, *types, tick_id=tick_id)

    result = CliRunner().invoke(backup_now_main, ["--wait-only", "--timeout", "0.2"])

    assert result.exit_code == expected_exit_code
    assert json.loads(result.stdout) == expected_report
    assert not (tmp_path / BACKUP_TOML_PATH).exists()
