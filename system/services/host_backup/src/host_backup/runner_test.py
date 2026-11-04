"""Unit tests for host_backup.runner pure-logic helpers."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from imbue.imbue_common.logging import ROTATED_JSONL_PATTERN

from host_backup.capabilities import BackupCapabilities, SnapshotMethod
from host_backup.config import (
    SLOW_BACKUP_NOTICE_TIMESTAMP_PATH,
    BackupConfig,
    RetentionSettings,
)
from host_backup.events import (
    EVENTS_LOG_ROTATION_BYTES,
    TICK_TERMINAL_EVENT_TYPES,
    BackupEventType,
    make_event,
    record_abandoned_tick,
    write_event,
)
from host_backup.runner import (
    CONSECUTIVE_FAILURE_ALARM_THRESHOLD,
    ENV_RECORD_CAPTURE_TIMEOUT_SECONDS,
    SLOW_BACKUP_NOTICE_INTERVAL_SECONDS,
    _age_out_restore_markers,
    _check_secrets_present,
    _cleanup_snapshot,
    _emit_tick_error,
    _free_space_over_quota,
    _load_config_if_changed,
    _LoopState,
    _maybe_run_prune,
    _parse_restic_timestamp,
    _refresh_environment_record,
    _run_forget,
    _run_one_tick,
    _run_restic_backup,
    _should_tick_now,
    _take_snapshot,
)
from host_backup.snapshot import SnapshotResult
from host_backup.usage import StoreUsage


def _direct_capabilities() -> BackupCapabilities:
    return BackupCapabilities(method=SnapshotMethod.DIRECT)


def _build_config(
    *,
    backup_interval_seconds: float = 3600.0,
    minimum_backup_gap_seconds: float = 60.0,
) -> BackupConfig:
    return BackupConfig(
        backup_interval_seconds=backup_interval_seconds,
        minimum_backup_gap_seconds=minimum_backup_gap_seconds,
    )


def test_should_tick_now_fires_on_startup() -> None:
    state = _LoopState(_direct_capabilities())
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=None
    )
    assert decision is True
    assert reason == "startup"


def test_should_tick_now_refuses_during_min_gap() -> None:
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic()
    config = _build_config(minimum_backup_gap_seconds=60.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=None
    )
    assert decision is False
    assert reason == "min_gap_not_elapsed"


def test_should_tick_now_fires_on_config_mtime_change() -> None:
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic() - 120.0  # past the min gap
    state.last_backup_toml_mtime = 1000.0
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=2000.0, env_mtime=None
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_fires_on_env_mtime_change() -> None:
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_restic_env_mtime = 500.0
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=900.0
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_fires_when_backup_toml_first_appears() -> None:
    """backup.toml appearing (mtime None -> value) counts as a config change.

    bootstrap no longer seeds backup.toml, so this transition is exactly what
    `host-backup-now` produces when it creates the file to force a tick.
    """
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_backup_toml_mtime = None
    state.last_restic_env_mtime = 500.0
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=500.0
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_fires_when_restic_env_first_appears() -> None:
    """restic.env appearing (mtime None -> value) counts as a config change.

    Imbue Studio injects restic.env into a running workspace to enable backups (no
    template is seeded anymore), so the first backup must fire promptly rather
    than waiting out backup_interval_seconds.
    """
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_backup_toml_mtime = 1000.0
    state.last_restic_env_mtime = None
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=800.0
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_refuses_before_interval_elapses() -> None:
    state = _LoopState(_direct_capabilities())
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_backup_toml_mtime = 1000.0
    state.last_restic_env_mtime = 500.0
    config = _build_config(backup_interval_seconds=3600.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=500.0
    )
    assert decision is False
    assert reason == "interval_not_elapsed"


def test_should_tick_now_fires_after_interval_elapses() -> None:
    state = _LoopState(_direct_capabilities())
    # Pretend the prior tick ended 4000 seconds ago (just over the default
    # 3600s interval).
    state.last_tick_end_monotonic = time.monotonic() - 4000.0
    state.last_backup_toml_mtime = 1000.0
    state.last_restic_env_mtime = 500.0
    config = _build_config(backup_interval_seconds=3600.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=500.0
    )
    assert decision is True
    assert reason == "interval"


def test_take_snapshot_emits_snapshot_failed_event_on_failure(tmp_path: Path) -> None:
    """A failed snapshot step writes a SNAPSHOT_FAILED event and returns None.

    Without this, a non-zero helper result.json (or any snapshot error) was only
    logged ephemerally and never appeared in the durable events stream, unlike
    restic failures.
    """
    events_dir = tmp_path / "events"
    # OUTER_TRIGGER with no paths makes make_snapshot_taker raise SnapshotError,
    # exercising the failure branch without needing a (timing-dependent) helper.
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.OUTER_TRIGGER))
    state.events_dir = events_dir
    state.current_tick_id = "tick-under-test"

    result = _take_snapshot(state=state)

    assert result is None
    events = [
        json.loads(line)
        for line in (events_dir / "events.jsonl").read_text().splitlines()
    ]
    failed_events = [event for event in events if event["type"] == "SNAPSHOT_FAILED"]
    assert len(failed_events) == 1
    assert failed_events[0]["tick_id"] == "tick-under-test"
    assert failed_events[0]["method"] == "OUTER_TRIGGER"
    assert failed_events[0]["error_message"]
    # A tick that took no backup, counted as one -- but not yet loud.
    assert failed_events[0]["consecutive_failures"] == 1
    assert state.consecutive_backup_failures == 1
    assert not [
        event for event in events if event["type"] == "BACKUP_REPEATEDLY_FAILING"
    ]


def test_take_snapshot_emits_the_alarm_after_the_threshold(tmp_path: Path) -> None:
    """Ticks that keep dying at the snapshot step escalate, the same as ticks that die at restic.

    A workspace whose snapshots the backup cannot read fails here every time,
    so if this step sat outside the escalation it would back up never and alarm
    never.
    """
    events_dir = tmp_path / "events"
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.OUTER_TRIGGER))
    state.events_dir = events_dir
    state.current_tick_id = "tick-under-test"
    state.consecutive_backup_failures = CONSECUTIVE_FAILURE_ALARM_THRESHOLD - 1

    assert _take_snapshot(state=state) is None

    assert state.consecutive_backup_failures == CONSECUTIVE_FAILURE_ALARM_THRESHOLD
    alarms = [
        event
        for event in _read_events(events_dir)
        if event["type"] == "BACKUP_REPEATEDLY_FAILING"
    ]
    assert len(alarms) == 1
    assert alarms[0]["consecutive_failures"] == CONSECUTIVE_FAILURE_ALARM_THRESHOLD
    assert alarms[0]["threshold"] == CONSECUTIVE_FAILURE_ALARM_THRESHOLD


def _read_events(events_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (events_dir / "events.jsonl").read_text().splitlines()
    ]


def test_every_way_a_tick_ends_emits_a_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive each way a tick can end and assert the endings match the terminal set.

    Four come from `_run_one_tick`'s step helpers; one comes from the loop's outer
    handler, which is where a tick that raises ends up; and the last is recorded by
    a restarted service for the tick its predecessor was killed in.

    `host-backup-now` waits for a member of TICK_TERMINAL_EVENT_TYPES, so a tick
    ending that emits something outside that set strands the caller until its
    timeout. Asserting set equality (not membership) pins the set to the endings
    enumerated below, so a type nothing emits cannot linger in it; a *new* way
    for a tick to end is only covered once it is driven here too.
    """
    # `_check_secrets_present` reads the relative data/.secrets/restic.env, so an
    # empty cwd is what makes "backups are not configured" the outcome.
    monkeypatch.chdir(tmp_path)
    observed: set[str] = set()

    def last_event_type(events_dir: Path) -> str:
        return str(_read_events(events_dir)[-1]["type"])

    # Backups not configured: the tick stops before restic ever runs.
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "no-secrets"
    assert _check_secrets_present(state=state) is None
    observed.add(last_event_type(state.events_dir))

    # The snapshot step aborts the tick (OUTER_TRIGGER with no paths configured).
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.OUTER_TRIGGER))
    state.events_dir = tmp_path / "snapshot-failed"
    assert _take_snapshot(state=state) is None
    observed.add(last_event_type(state.events_dir))

    # restic itself fails, and succeeds.
    for returncode, events_subdir in ((1, "restic-failed"), (0, "restic-ok")):
        state = _LoopState(_direct_capabilities())
        state.events_dir = tmp_path / events_subdir
        _run_restic_backup(
            state=state,
            config=_build_config(),
            snapshot=_direct_snapshot(),
            env_overrides={},
            backup_fn=_ScriptedRestic([_completed(returncode)]),
            unlock_fn=_ScriptedRestic([]),
        )
        observed.add(last_event_type(state.events_dir))

    # The account is over its backup storage quota: the tick frees space instead
    # of backing up, and the skip event (the first one written) ends the tick.
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "over-quota"
    _free_space_over_quota(
        state=state,
        config=_build_config(),
        env_overrides={},
        usage=_over_quota_usage(),
        forget_fn=_ScriptedRestic([_completed(0)]),
        prune_fn=_ScriptedRestic([_completed(0)]),
        unlock_fn=_ScriptedRestic([]),
    )
    observed.add(str(_read_events(state.events_dir)[0]["type"]))

    # An unhandled error caught by the loop's outer handler.
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "tick-error"
    _emit_tick_error(state, ValueError("boom"))
    observed.add(last_event_type(state.events_dir))

    # The service restarted while a tick was running.
    abandoned_dir = tmp_path / "abandoned"
    write_event(
        abandoned_dir,
        make_event(BackupEventType.BACKUP_STARTED, tick_id="tick-killed"),
    )
    record_abandoned_tick(abandoned_dir)
    observed.add(last_event_type(abandoned_dir))

    assert observed == TICK_TERMINAL_EVENT_TYPES


def test_a_tick_starts_a_fresh_events_log_once_the_old_one_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rotation happens before the tick writes anything, so the tick's events all
    land in one file: `host-backup-now` reads the triggered tick from the fresh file,
    and a tick split across two would leave its start in one and its end in the other."""
    # An empty cwd has no restic.env, so the tick ends before restic runs.
    monkeypatch.chdir(tmp_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    with (events_dir / "events.jsonl").open("wb") as fh:
        fh.seek(EVENTS_LOG_ROTATION_BYTES)
        fh.write(b"\n")
    state = _LoopState(_direct_capabilities())
    state.events_dir = events_dir
    state.current_tick_id = "tick-under-test"

    _run_one_tick(state=state, config=_build_config(), trigger_reason="config_change")

    rotated_logs = [
        child
        for child in events_dir.iterdir()
        if ROTATED_JSONL_PATTERN.match(child.name)
    ]
    assert len(rotated_logs) == 1
    assert rotated_logs[0].stat().st_size == EVENTS_LOG_ROTATION_BYTES + 1
    events = _read_events(events_dir)
    assert [event["type"] for event in events] == [
        "CONFIG_RELOADED",
        "BACKUP_STARTED",
        "TICK_SKIPPED_DUE_TO_MISSING_SECRETS",
    ]
    assert {event["tick_id"] for event in events} == {"tick-under-test"}


def test_cleanup_runs_with_no_snapshot_result_in_hand(tmp_path: Path) -> None:
    """Cleanup must work without a SnapshotResult: a tick aborted at the snapshot step has none.

    That is the tick whose snapshot the outer helper has already created, so it
    is exactly the one whose snapshots would otherwise pile up unreclaimed.
    """
    events_dir = tmp_path / "events"
    # OUTER_TRIGGER with no paths makes make_snapshot_taker raise, which is the
    # cheapest way to reach cleanup's own failure report -- the branch that used
    # to read the snapshot result for the path it names.
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.OUTER_TRIGGER))
    state.events_dir = events_dir
    state.current_tick_id = "tick-under-test"

    _cleanup_snapshot(state=state, snapshot=None)

    deleted_events = [
        event
        for event in _read_events(events_dir)
        if event["type"] == "SNAPSHOT_DELETED"
    ]
    assert len(deleted_events) == 1
    assert deleted_events[0]["method"] == "OUTER_TRIGGER"
    assert deleted_events[0]["success"] is False
    assert deleted_events[0]["snapshot_path"] == ""
    assert deleted_events[0]["error_message"]


def test_refresh_environment_record_emits_success_event(tmp_path: Path) -> None:
    """A clean capture emits ENV_RECORD_CAPTURE_COMPLETED with success=True."""
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["timeout"] == ENV_RECORD_CAPTURE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    _refresh_environment_record(state=state, run_fn=fake_run)

    assert calls == [["uv", "run", "--no-sync", "env-converge", "capture"]]
    events = _read_events(state.events_dir)
    capture_events = [e for e in events if e["type"] == "ENV_RECORD_CAPTURE_COMPLETED"]
    assert len(capture_events) == 1
    assert capture_events[0]["tick_id"] == "tick-under-test"
    assert capture_events[0]["success"] is True
    assert capture_events[0]["exit_code"] == 0
    assert capture_events[0]["stderr"] == ""


def test_refresh_environment_record_failure_is_best_effort(tmp_path: Path) -> None:
    """A failed capture emits success=False with the stderr tail and does not raise.

    The record refresh must never block the backup itself: a slightly stale
    record in the snapshot beats no snapshot.
    """
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"

    def failing_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=3, stdout="", stderr="probe exploded"
        )

    _refresh_environment_record(state=state, run_fn=failing_run)

    capture_events = [
        e
        for e in _read_events(state.events_dir)
        if e["type"] == "ENV_RECORD_CAPTURE_COMPLETED"
    ]
    assert len(capture_events) == 1
    assert capture_events[0]["success"] is False
    assert capture_events[0]["exit_code"] == 3
    assert capture_events[0]["stderr"] == "probe exploded"


def test_refresh_environment_record_tolerates_unlaunchable_capture(
    tmp_path: Path,
) -> None:
    """A capture that cannot run at all (missing binary, timeout) still just events.

    Covers pre-cutover workspaces where env-converge does not exist: the
    backup must proceed and the event records exit_code=None.
    """
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"

    def raising_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv not found")

    _refresh_environment_record(state=state, run_fn=raising_run)

    capture_events = [
        e
        for e in _read_events(state.events_dir)
        if e["type"] == "ENV_RECORD_CAPTURE_COMPLETED"
    ]
    assert len(capture_events) == 1
    assert capture_events[0]["success"] is False
    assert capture_events[0]["exit_code"] is None
    assert "uv not found" in str(capture_events[0]["stderr"])


def _direct_snapshot() -> SnapshotResult:
    return SnapshotResult(
        method=SnapshotMethod.DIRECT,
        snapshot_path="/home/user/.mngr",
        read_path=Path("/home/user/.mngr"),
        duration_seconds=0.0,
    )


def _completed(
    returncode: int, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["restic"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _ScriptedRestic:
    """A restic call that returns scripted results in order and records each call's keyword arguments."""

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = results
        self.calls = 0
        self.call_kwargs: list[dict[str, object]] = []

    def __call__(
        self, *_args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        result = self._results[self.calls]
        self.calls += 1
        self.call_kwargs.append(kwargs)
        return result


def _events_in(events_dir: Path | None) -> list[dict]:
    assert events_dir is not None
    return [
        json.loads(line)
        for line in (events_dir / "events.jsonl").read_text().splitlines()
    ]


def _state_recording_events(tmp_path: Path) -> _LoopState:
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"
    return state


def _run_backup_under_test(
    tmp_path: Path,
    backup: _ScriptedRestic,
    *,
    unlock: _ScriptedRestic | None = None,
    initial_failures: int = 0,
) -> tuple[_LoopState, bool]:
    state = _state_recording_events(tmp_path)
    state.consecutive_backup_failures = initial_failures
    succeeded = _run_restic_backup(
        state=state,
        config=_build_config(),
        snapshot=_direct_snapshot(),
        env_overrides={},
        backup_fn=backup,
        unlock_fn=unlock if unlock is not None else _ScriptedRestic([]),
    )
    return state, succeeded


def test_run_restic_backup_passes_extra_excludes_after_the_excludes(
    tmp_path: Path,
) -> None:
    backup = _ScriptedRestic(
        [_completed(0, stdout='{"message_type":"summary","snapshot_id":"snap1"}')]
    )
    _run_restic_backup(
        state=_state_recording_events(tmp_path),
        config=BackupConfig(
            excludes=("**/only-this",), extra_excludes=("**/and-this",)
        ),
        snapshot=_direct_snapshot(),
        env_overrides={},
        backup_fn=backup,
        unlock_fn=_ScriptedRestic([]),
    )

    assert backup.call_kwargs[0]["excludes"] == ("**/only-this", "**/and-this")


_LOCK_STDERR = (
    "unable to create lock in backend: repository is already locked exclusively "
    "by PID 1515556 on host by root"
)


def test_run_restic_backup_unlocks_and_retries_on_stale_lock(tmp_path: Path) -> None:
    """A lock error triggers `restic unlock` and one retry; the retry's success wins."""
    backup = _ScriptedRestic(
        [
            _completed(1, stderr=_LOCK_STDERR),
            _completed(0, stdout='{"message_type":"summary","snapshot_id":"snap1"}'),
        ]
    )
    unlock = _ScriptedRestic([_completed(0)])
    state, succeeded = _run_backup_under_test(
        tmp_path, backup, unlock=unlock, initial_failures=4
    )

    assert succeeded is True
    assert backup.calls == 2
    assert unlock.calls == 1
    assert state.consecutive_backup_failures == 0  # reset on success
    succeeded_events = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "RESTIC_BACKUP_SUCCEEDED"
    ]
    assert len(succeeded_events) == 1
    assert succeeded_events[0]["snapshot_id"] == "snap1"


def test_run_restic_backup_does_not_unlock_on_unrelated_failure(tmp_path: Path) -> None:
    """A non-lock failure is never retried and never runs unlock."""
    backup = _ScriptedRestic([_completed(1, stderr="network unreachable")])
    unlock = _ScriptedRestic([])
    state, succeeded = _run_backup_under_test(tmp_path, backup, unlock=unlock)

    assert succeeded is False
    assert backup.calls == 1
    assert unlock.calls == 0
    failed_events = [
        e for e in _events_in(state.events_dir) if e["type"] == "RESTIC_BACKUP_FAILED"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["consecutive_failures"] == 1


def test_run_restic_backup_emits_alarm_after_threshold(tmp_path: Path) -> None:
    """Crossing the consecutive-failure threshold emits a durable escalation event."""
    backup = _ScriptedRestic([_completed(1, stderr="backend error")])
    state, succeeded = _run_backup_under_test(
        tmp_path, backup, initial_failures=CONSECUTIVE_FAILURE_ALARM_THRESHOLD - 1
    )

    assert succeeded is False
    assert state.consecutive_backup_failures == CONSECUTIVE_FAILURE_ALARM_THRESHOLD
    alarms = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "BACKUP_REPEATEDLY_FAILING"
    ]
    assert len(alarms) == 1
    assert alarms[0]["consecutive_failures"] == CONSECUTIVE_FAILURE_ALARM_THRESHOLD
    assert alarms[0]["threshold"] == CONSECUTIVE_FAILURE_ALARM_THRESHOLD


def test_run_restic_backup_no_alarm_below_threshold(tmp_path: Path) -> None:
    """A single failure records the count but does not raise the alarm."""
    backup = _ScriptedRestic([_completed(1, stderr="backend error")])
    state, _ = _run_backup_under_test(tmp_path, backup)

    assert state.consecutive_backup_failures == 1
    alarms = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "BACKUP_REPEATEDLY_FAILING"
    ]
    assert alarms == []


_SLOW_BACKUP_SUMMARY_STDOUT = json.dumps(
    {
        "message_type": "summary",
        "snapshot_id": "snap-slow",
        "files_new": 3,
        "files_changed": 41,
        "files_unmodified": 865722,
        "dirs_unmodified": 150112,
        "total_files_processed": 865766,
    }
)


def _run_backup_with_threshold(
    tmp_path: Path, *, slow_backup_threshold_seconds: float
) -> _LoopState:
    state = _state_recording_events(tmp_path)
    _run_restic_backup(
        state=state,
        config=BackupConfig(
            slow_backup_threshold_seconds=slow_backup_threshold_seconds
        ),
        snapshot=_direct_snapshot(),
        env_overrides={},
        backup_fn=_ScriptedRestic([_completed(0, stdout=_SLOW_BACKUP_SUMMARY_STDOUT)]),
        unlock_fn=_ScriptedRestic([]),
    )
    return state


def _slow_events(state: _LoopState) -> list[dict]:
    return [e for e in _events_in(state.events_dir) if e["type"] == "BACKUP_SLOW"]


def test_a_backup_over_the_threshold_records_a_slow_notice_with_restics_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The notice's gate file lives under the relative data/.state/.
    monkeypatch.chdir(tmp_path)
    # A scripted backup returns at once, so a zero threshold is the only one it can pass.
    state = _run_backup_with_threshold(tmp_path, slow_backup_threshold_seconds=0.0)

    slow = _slow_events(state)
    assert len(slow) == 1
    assert slow[0]["total_files_processed"] == 865766
    assert slow[0]["files_unmodified"] == 865722
    assert slow[0]["dirs_unmodified"] == 150112
    assert (tmp_path / SLOW_BACKUP_NOTICE_TIMESTAMP_PATH).exists()


def test_a_backup_under_the_threshold_records_no_slow_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state = _run_backup_with_threshold(tmp_path, slow_backup_threshold_seconds=3600.0)

    assert _slow_events(state) == []
    assert not (tmp_path / SLOW_BACKUP_NOTICE_TIMESTAMP_PATH).exists()


@pytest.mark.parametrize(
    ("last_notice_age_seconds", "expected_notice_count"),
    [
        pytest.param(3600.0, 0, id="notified-an-hour-ago"),
        pytest.param(
            SLOW_BACKUP_NOTICE_INTERVAL_SECONDS + 3600.0,
            1,
            id="notified-over-a-day-ago",
        ),
    ],
)
def test_the_slow_notice_is_recorded_at_most_once_a_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    last_notice_age_seconds: float,
    expected_notice_count: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    gate = tmp_path / SLOW_BACKUP_NOTICE_TIMESTAMP_PATH
    gate.parent.mkdir(parents=True)
    gate.write_text("earlier notice")
    noticed_at = time.time() - last_notice_age_seconds
    os.utime(gate, (noticed_at, noticed_at))

    state = _run_backup_with_threshold(tmp_path, slow_backup_threshold_seconds=0.0)

    assert len(_slow_events(state)) == expected_notice_count


# forget / prune under a stale lock

# What restic prints when an exclusive lock (forget, prune) meets the
# non-exclusive lock a dead container left behind; exit code 11.
_NON_EXCLUSIVE_LOCK_STDERR = (
    "unable to create lock in backend: repository is already locked by "
    "PID 1928420 on efa2d6b8510d by root (UID 0, GID 0)"
)


def _forget_exit_codes(state: _LoopState) -> list[object]:
    return [
        e["exit_code"]
        for e in _events_in(state.events_dir)
        if e["type"] == "FORGET_COMPLETED"
    ]


_FORGET_LOCKED = _completed(11, stderr=_NON_EXCLUSIVE_LOCK_STDERR)


@pytest.mark.parametrize(
    ("forget_results", "unlock_results", "expected_unlock_calls", "expected_exit_code"),
    [
        pytest.param(
            [_FORGET_LOCKED, _completed(0)],
            [_completed(0)],
            1,
            0,
            id="stale-lock-cleared-retry-wins",
        ),
        pytest.param(
            [_FORGET_LOCKED, _FORGET_LOCKED],
            [_completed(0)],
            1,
            11,
            id="live-lock-kept-one-retry-only",
        ),
        pytest.param(
            [_FORGET_LOCKED],
            [_completed(1, stderr="unable to open repository")],
            1,
            11,
            id="unlock-failed-no-retry",
        ),
        pytest.param(
            [_completed(1, stderr="network unreachable")],
            [],
            0,
            1,
            id="unrelated-failure-no-unlock",
        ),
    ],
)
def test_run_forget_unlock_and_retry(
    tmp_path: Path,
    forget_results: list[subprocess.CompletedProcess[str]],
    unlock_results: list[subprocess.CompletedProcess[str]],
    expected_unlock_calls: int,
    expected_exit_code: int,
) -> None:
    """Only a lock error runs `restic unlock`; forget is retried once, and only after
    a successful unlock. The recorded exit code is the last forget's."""
    forget = _ScriptedRestic(forget_results)
    unlock = _ScriptedRestic(unlock_results)
    state = _state_recording_events(tmp_path)

    _run_forget(
        state=state,
        config=_build_config(),
        env_overrides={},
        forget_fn=forget,
        unlock_fn=unlock,
    )

    assert forget.calls == len(forget_results)
    assert unlock.calls == expected_unlock_calls
    assert _forget_exit_codes(state) == [expected_exit_code]


def test_prune_unlocks_and_retries_on_a_stale_lock_and_records_the_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prune gate file lives under the relative data/.state/.
    monkeypatch.chdir(tmp_path)
    prune = _ScriptedRestic(
        [_completed(11, stderr=_NON_EXCLUSIVE_LOCK_STDERR), _completed(0)]
    )
    unlock = _ScriptedRestic([_completed(0)])
    state = _state_recording_events(tmp_path)

    _maybe_run_prune(
        state=state,
        config=_build_config(),
        env_overrides={},
        prune_fn=prune,
        unlock_fn=unlock,
    )

    assert prune.calls == 2
    assert unlock.calls == 1
    assert (tmp_path / "data/.state/last-restic-prune").exists()


def test_age_out_restore_markers_unlocks_and_retries_on_a_stale_lock(
    tmp_path: Path,
) -> None:
    listed = _snapshots_result(
        [{"id": "old-marker", "time": "2026-07-10T00:00:00Z", "tags": ["restored"]}]
    )
    forget_ids = _ScriptedRestic(
        [_completed(11, stderr=_NON_EXCLUSIVE_LOCK_STDERR), _completed(0)]
    )
    unlock = _ScriptedRestic([_completed(0)])
    state = _state_recording_events(tmp_path)

    _age_out_restore_markers(
        state=state,
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=lambda _tags, _env: listed,
        forget_ids_fn=forget_ids,
        unlock_fn=unlock,
        now_fn=lambda: _FIXED_NOW,
    )

    assert forget_ids.calls == 2
    assert unlock.calls == 1
    forgotten = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "RESTORE_MARKERS_FORGOTTEN"
    ]
    assert [e["exit_code"] for e in forgotten] == [0]


def test_load_config_if_changed_caches_until_mtime_moves(tmp_path: Path) -> None:
    """The config is re-parsed only when backup.toml's mtime changes.

    (Keeps tolerant-parse warnings to one occurrence per edit rather than one
    per poll.) The function trusts the caller-supplied mtime, so the test
    drives it with sentinel values while changing the file's content on disk.
    """
    path = tmp_path / "backup.toml"
    path.write_text("backup_interval_seconds = 1800\n")
    state = _LoopState(_direct_capabilities())

    first = _load_config_if_changed(state, 111.0, path)
    assert first.backup_interval_seconds == 1800.0
    assert state.last_loaded_backup_toml_mtime == 111.0

    # Same mtime: the exact cached object is returned, even though the file's
    # content changed on disk (no re-parse happens).
    path.write_text("backup_interval_seconds = 900\n")
    cached = _load_config_if_changed(state, 111.0, path)
    assert cached is first
    assert cached.backup_interval_seconds == 1800.0

    # Mtime moved: reload happens and picks up the new content.
    reloaded = _load_config_if_changed(state, 222.0, path)
    assert reloaded.backup_interval_seconds == 900.0
    assert state.last_loaded_backup_toml_mtime == 222.0


# Restore-marker age-out

_FIXED_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _snapshots_result(
    snapshots: list[dict[str, object]],
) -> subprocess.CompletedProcess[str]:
    return _completed(0, stdout=json.dumps(snapshots))


def _age_out_config(max_age_days: float) -> BackupConfig:
    return BackupConfig(
        retention=RetentionSettings(restore_marker_max_age_days=max_age_days)
    )


def test_parse_restic_timestamp_handles_z_and_nanoseconds() -> None:
    assert _parse_restic_timestamp("2026-07-24T18:47:38Z") == datetime(
        2026, 7, 24, 18, 47, 38, tzinfo=timezone.utc
    )
    # Nanosecond precision is clamped to microseconds, not rejected.
    assert _parse_restic_timestamp("2026-07-24T18:47:38.123456789Z") == datetime(
        2026, 7, 24, 18, 47, 38, 123456, tzinfo=timezone.utc
    )


def test_parse_restic_timestamp_normalizes_offset_to_utc() -> None:
    # 18:47:38 at -07:00 is 01:47:38 the next day in UTC.
    assert _parse_restic_timestamp("2026-07-24T18:47:38-07:00") == datetime(
        2026, 7, 25, 1, 47, 38, tzinfo=timezone.utc
    )


def test_parse_restic_timestamp_returns_none_for_garbage() -> None:
    assert _parse_restic_timestamp("") is None
    assert _parse_restic_timestamp("not-a-timestamp") is None


def test_age_out_restore_markers_forgets_old_and_keeps_recent(tmp_path: Path) -> None:
    list_calls: list[tuple[str, ...]] = []
    forget_calls: list[tuple[str, ...]] = []

    def _list_fn(
        tags: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        list_calls.append(tags)
        return _snapshots_result(
            [
                {
                    "id": "old-marker",
                    "time": "2026-07-10T00:00:00Z",
                    "tags": ["restored"],
                },
                {
                    "id": "recent-marker",
                    "time": "2026-07-22T00:00:00Z",
                    "tags": ["pre-restore"],
                },
            ]
        )

    def _forget_ids_fn(
        ids: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        forget_calls.append(ids)
        return _completed(0)

    state = _state_recording_events(tmp_path)
    _age_out_restore_markers(
        state=state,
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    # It queried both marker tags and forgot only the marker past the 7-day cutoff.
    assert list_calls == [("restored", "pre-restore")]
    assert forget_calls == [("old-marker",)]
    forgotten_events = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "RESTORE_MARKERS_FORGOTTEN"
    ]
    assert len(forgotten_events) == 1
    assert forgotten_events[0]["forgotten_count"] == 1


def test_age_out_restore_markers_noop_when_all_recent(tmp_path: Path) -> None:
    forget_calls: list[tuple[str, ...]] = []

    def _list_fn(
        _tags: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        return _snapshots_result(
            [{"id": "recent", "time": "2026-07-23T00:00:00Z", "tags": ["restored"]}]
        )

    def _forget_ids_fn(
        ids: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        forget_calls.append(ids)
        return _completed(0)

    state = _state_recording_events(tmp_path)
    _age_out_restore_markers(
        state=state,
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    assert forget_calls == []
    assert state.events_dir is not None
    assert not (state.events_dir / "events.jsonl").exists()


def test_age_out_restore_markers_disabled_when_max_age_zero(tmp_path: Path) -> None:
    calls: list[str] = []

    def _list_fn(
        _tags: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append("list")
        return _completed(0)

    def _forget_ids_fn(
        _ids: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append("forget")
        return _completed(0)

    _age_out_restore_markers(
        state=_state_recording_events(tmp_path),
        config=_age_out_config(0.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    # A zero cutoff keeps markers forever: nothing is even queried.
    assert calls == []


def test_age_out_restore_markers_tolerates_list_failure(tmp_path: Path) -> None:
    forget_calls: list[tuple[str, ...]] = []

    def _list_fn(
        _tags: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="repository is already locked")

    def _forget_ids_fn(
        ids: tuple[str, ...], _env: object
    ) -> subprocess.CompletedProcess[str]:
        forget_calls.append(ids)
        return _completed(0)

    _age_out_restore_markers(
        state=_state_recording_events(tmp_path),
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    # A failed listing must not forget anything and must not raise.
    assert forget_calls == []


def _over_quota_usage(*, headroom_bytes: int = 64 * 1024 * 1024) -> StoreUsage:
    return StoreUsage(
        used_bytes=900,
        account_used_bytes=1_500,
        quota_bytes=1_000,
        hard_limit_bytes=1_000 + headroom_bytes,
        mode="active",
    )


def _under_quota_usage() -> StoreUsage:
    return StoreUsage(
        used_bytes=900,
        account_used_bytes=1_000,
        quota_bytes=1_000,
        hard_limit_bytes=2_000,
        mode="active",
    )


def _write_store_restic_env(root: Path) -> None:
    env_path = root / "data/.secrets/restic.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "RESTIC_REPOSITORY=rest:https://backups.example.com/u/agent-x/\n"
        "RESTIC_REST_USERNAME=agent-x\nRESTIC_REST_PASSWORD=tok\nRESTIC_PASSWORD=pw\n"
    )


def test_a_tick_over_the_soft_quota_frees_space_instead_of_backing_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `_check_secrets_present` reads the relative data/.secrets/restic.env.
    monkeypatch.chdir(tmp_path)
    _write_store_restic_env(tmp_path)
    state = _state_recording_events(tmp_path)
    freed: list[dict[str, object]] = []

    _run_one_tick(
        state=state,
        config=_build_config(),
        trigger_reason="interval",
        usage_fn=lambda env: _over_quota_usage(),
        free_space_fn=lambda **kwargs: freed.append(kwargs),
    )

    (call,) = freed
    assert call["usage"] == _over_quota_usage()
    env_overrides = call["env_overrides"]
    assert isinstance(env_overrides, dict)
    assert env_overrides["RESTIC_REST_PASSWORD"] == "tok"
    assert [e["type"] for e in _events_in(state.events_dir)] == [
        "CONFIG_RELOADED",
        "BACKUP_STARTED",
    ]


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(None, id="no-quota-information"),
        pytest.param(_under_quota_usage(), id="under-quota"),
    ],
)
def test_a_tick_proceeds_to_its_snapshot_when_not_over_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: StoreUsage | None
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_store_restic_env(tmp_path)
    # OUTER_TRIGGER with no paths configured fails at the snapshot step, which
    # proves the tick went past the usage probe without running restic.
    state = _LoopState(BackupCapabilities(method=SnapshotMethod.OUTER_TRIGGER))
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"

    _run_one_tick(
        state=state,
        config=_build_config(),
        trigger_reason="interval",
        usage_fn=lambda env: usage,
        free_space_fn=lambda **kwargs: pytest.fail("the tick must not free space"),
    )

    assert "SNAPSHOT_FAILED" in [e["type"] for e in _events_in(state.events_dir)]
    assert "BACKUP_SKIPPED_OVER_QUOTA" not in [
        e["type"] for e in _events_in(state.events_dir)
    ]


def test_the_over_quota_tick_records_the_usage_then_forgets_and_prunes_within_half_the_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prune gate file lives under the relative data/.state/; a fresh one
    # must not stop the over-quota prune, which exists to free space now.
    monkeypatch.chdir(tmp_path)
    gate = tmp_path / "data/.state/last-restic-prune"
    gate.parent.mkdir(parents=True)
    gate.write_text("just now")
    forget = _ScriptedRestic([_completed(0)])
    prune = _ScriptedRestic([_completed(0)])
    state = _state_recording_events(tmp_path)

    _free_space_over_quota(
        state=state,
        config=_build_config(),
        env_overrides={"RESTIC_REPOSITORY": "rest:https://x/u/w/"},
        usage=_over_quota_usage(headroom_bytes=64 * 1024 * 1024),
        forget_fn=forget,
        prune_fn=prune,
        unlock_fn=_ScriptedRestic([]),
    )

    events = _events_in(state.events_dir)
    assert [e["type"] for e in events] == [
        "BACKUP_SKIPPED_OVER_QUOTA",
        "FORGET_COMPLETED",
        "PRUNE_COMPLETED",
    ]
    skipped = events[0]
    assert skipped["tick_id"] == "tick-under-test"
    assert (skipped["used_bytes"], skipped["account_used_bytes"]) == (900, 1_500)
    assert (skipped["quota_bytes"], skipped["hard_limit_bytes"]) == (
        1_000,
        1_000 + 64 * 1024 * 1024,
    )
    assert forget.calls == 1
    assert prune.calls == 1
    assert prune.call_kwargs[0]["max_repack_bytes"] == 32 * 1024 * 1024
    assert state.consecutive_backup_failures == 0


def test_a_store_refusing_the_upload_is_recorded_as_a_quota_failure(
    tmp_path: Path,
) -> None:
    refused = _completed(
        1,
        stderr=(
            "Save(<data/f5098c7a77>) failed: unexpected HTTP response (507): 507 Insufficient Storage\n"
            "Fatal: unable to save snapshot: unexpected HTTP response (507): 507 Insufficient Storage"
        ),
    )
    state, succeeded = _run_backup_under_test(tmp_path, _ScriptedRestic([refused]))

    assert succeeded is False
    (failed,) = [
        e for e in _events_in(state.events_dir) if e["type"] == "RESTIC_BACKUP_FAILED"
    ]
    assert failed["reason"] == "quota_exceeded"


def test_an_ordinary_backup_failure_carries_no_reason(tmp_path: Path) -> None:
    state, succeeded = _run_backup_under_test(
        tmp_path, _ScriptedRestic([_completed(1, stderr="network unreachable")])
    )

    assert succeeded is False
    (failed,) = [
        e for e in _events_in(state.events_dir) if e["type"] == "RESTIC_BACKUP_FAILED"
    ]
    assert failed["reason"] == ""


def test_a_fresh_prune_gate_stops_the_routine_prune_but_not_a_forced_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    gate = tmp_path / "data/.state/last-restic-prune"
    gate.parent.mkdir(parents=True)
    gate.write_text("just now")
    routine = _ScriptedRestic([_completed(0)])
    forced = _ScriptedRestic([_completed(0)])
    state = _state_recording_events(tmp_path)

    _maybe_run_prune(
        state=state,
        config=_build_config(),
        env_overrides={},
        prune_fn=routine,
        unlock_fn=_ScriptedRestic([]),
    )
    _maybe_run_prune(
        state=state,
        config=_build_config(),
        env_overrides={},
        prune_fn=forced,
        unlock_fn=_ScriptedRestic([]),
        is_forced=True,
        max_repack_bytes=5,
    )

    assert routine.calls == 0
    assert forced.calls == 1
    assert forced.call_kwargs[0]["max_repack_bytes"] == 5
    assert [e["type"] for e in _events_in(state.events_dir)] == [
        "PRUNE_SKIPPED",
        "PRUNE_COMPLETED",
    ]
