"""Unit tests for the `host-backup-now` waiters and exit-code contract."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from host_backup.cli import (
    EXIT_BACKUP_FAILED,
    EXIT_BACKUP_SUCCEEDED,
    EXIT_BACKUPS_NOT_CONFIGURED,
    EXIT_NO_COMPLETION_OBSERVED,
    _EventsLogFollower,
    _exit_code_for_completion,
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
from host_backup.testing import write_tick

# Long enough that a waiter which fails to recognise a terminal event is
# unambiguously stuck rather than merely slow, short enough that the test still
# finishes if that regression reappears.
_GENEROUS_TIMEOUT_SECONDS = 3.0


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
    write_tick(
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
    write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-hung")
    completion = _wait_for_next_completion(follower, time.monotonic() + 0.1)
    assert completion is None


def test_wait_ignores_events_already_present_before_the_trigger(tmp_path: Path) -> None:
    """Only events appended after the config bump count as this run's completion."""
    write_tick(
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
    write_tick(
        tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-previous"
    )
    with events_path.open("r+b") as fh:
        fh.seek(EVENTS_LOG_ROTATION_BYTES)
        fh.write(b"\n")
    follower = _EventsLogFollower(events_path)

    rotate_events_log_if_over(tmp_path)
    write_tick(
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
    write_tick(backup_events_dir, BackupEventType.BACKUP_STARTED, tick_id="tick-busy")

    result = CliRunner().invoke(backup_now_main, ["--timeout", "0.2"])

    assert result.exit_code == EXIT_NO_COMPLETION_OBSERVED
    assert not (tmp_path / BACKUP_TOML_PATH).exists()


def test_backup_now_triggers_past_a_tick_that_never_finished(
    tmp_path: Path, backup_events_dir: Path
) -> None:
    write_tick(backup_events_dir, BackupEventType.BACKUP_STARTED, tick_id="tick-killed")
    write_tick(
        backup_events_dir,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )

    result = CliRunner().invoke(backup_now_main, ["--timeout", "0.2"])

    # No runner is reading the bumped config here, so the triggered tick never ends.
    assert result.exit_code == EXIT_NO_COMPLETION_OBSERVED
    assert (tmp_path / BACKUP_TOML_PATH).exists()


def test_the_inflight_wait_ends_on_its_own_tick_only(tmp_path: Path) -> None:
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    write_tick(tmp_path, BackupEventType.RESTIC_BACKUP_SUCCEEDED, tick_id="tick-other")
    assert not _wait_for_tick_to_end(follower, "tick-busy", time.monotonic() + 0.1)

    write_tick(tmp_path, BackupEventType.RESTIC_BACKUP_FAILED, tick_id="tick-busy")
    assert _wait_for_tick_to_end(
        follower, "tick-busy", time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )


def test_the_inflight_wait_follows_a_tick_that_started_after_the_one_it_waited_for(
    tmp_path: Path,
) -> None:
    """The waited tick's runner was restarted mid-wait: its startup tick is the one in
    flight now, and the dead tick never ends."""
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-after-restart",
    )
    assert _wait_for_tick_to_end(
        follower, "tick-killed", time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )


def _write_aged_tick(
    events_dir: Path, event_type: BackupEventType, tick_id: str, age: timedelta
) -> None:
    at = datetime.now(timezone.utc) - age
    for kind in (BackupEventType.BACKUP_STARTED, event_type):
        write_event(
            events_dir,
            make_event(kind, tick_id=tick_id, timestamp=at.isoformat()),
        )


@pytest.mark.parametrize(
    ("ticks", "expected_exit_code", "expected_outcome"),
    [
        pytest.param(
            ((BackupEventType.RESTIC_BACKUP_SUCCEEDED, timedelta(minutes=50)),),
            EXIT_BACKUP_SUCCEEDED,
            BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
            id="recent-success",
        ),
        # The success is older than two of the default one-hour intervals: the
        # service is not running, or every tick since has failed.
        pytest.param(
            ((BackupEventType.RESTIC_BACKUP_SUCCEEDED, timedelta(hours=3)),),
            EXIT_BACKUP_FAILED,
            BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
            id="stale-success",
        ),
        pytest.param(
            (
                (BackupEventType.RESTIC_BACKUP_SUCCEEDED, timedelta(minutes=50)),
                (BackupEventType.RESTIC_BACKUP_FAILED, timedelta(minutes=5)),
            ),
            EXIT_BACKUP_SUCCEEDED,
            BackupEventType.RESTIC_BACKUP_FAILED.value,
            id="one-failure-after-a-recent-success",
        ),
        # Secrets removed since the last success: no restore point can be taken now.
        pytest.param(
            (
                (BackupEventType.RESTIC_BACKUP_SUCCEEDED, timedelta(minutes=50)),
                (
                    BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
                    timedelta(minutes=5),
                ),
            ),
            EXIT_BACKUPS_NOT_CONFIGURED,
            BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value,
            id="not-configured",
        ),
        pytest.param((), EXIT_BACKUP_FAILED, None, id="never-ran"),
    ],
)
def test_check_reports_the_newest_outcomes_and_triggers_nothing(
    tmp_path: Path,
    backup_events_dir: Path,
    ticks: tuple[tuple[BackupEventType, timedelta], ...],
    expected_exit_code: int,
    expected_outcome: str | None,
) -> None:
    for index, (event_type, age) in enumerate(ticks):
        _write_aged_tick(backup_events_dir, event_type, f"tick-{index}", age)

    result = CliRunner().invoke(backup_now_main, ["--check"])

    assert result.exit_code == expected_exit_code
    assert json.loads(result.stdout)["newest_outcome"] == expected_outcome
    assert not (tmp_path / BACKUP_TOML_PATH).exists()


def test_the_wait_for_the_triggered_tick_skips_a_restart_and_reports_the_next_tick(
    tmp_path: Path,
) -> None:
    """A services restart kills the triggered tick; the restarted service records it
    abandoned and backs up again as its first tick, which is the outcome to report."""
    follower = _EventsLogFollower(tmp_path / "events.jsonl")
    write_tick(tmp_path, BackupEventType.BACKUP_STARTED, tick_id="tick-triggered")
    write_tick(tmp_path, BackupEventType.TICK_ABANDONED, tick_id="tick-triggered")
    write_tick(
        tmp_path,
        BackupEventType.BACKUP_STARTED,
        BackupEventType.RESTIC_BACKUP_SUCCEEDED,
        tick_id="tick-startup",
    )

    completion = _wait_for_next_completion(
        follower, time.monotonic() + _GENEROUS_TIMEOUT_SECONDS
    )

    assert completion is not None
    assert completion["tick_id"] == "tick-startup"
    assert _exit_code_for_completion(completion) == EXIT_BACKUP_SUCCEEDED
