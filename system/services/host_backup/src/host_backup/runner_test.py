"""Unit tests for host_backup.runner pure-logic helpers."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from host_backup.capabilities import BackupCapabilities, SnapshotMethod
from host_backup.config import BackupConfig, RetentionSettings
from host_backup.events import TICK_TERMINAL_EVENT_TYPES
from host_backup.runner import (
    CONSECUTIVE_FAILURE_ALARM_THRESHOLD,
    ENV_RECORD_CAPTURE_TIMEOUT_SECONDS,
    _age_out_restore_markers,
    _check_secrets_present,
    _emit_tick_error,
    _load_config_if_changed,
    _LoopState,
    _parse_restic_timestamp,
    _refresh_environment_record,
    _run_restic_backup,
    _should_tick_now,
    _take_snapshot,
)
from host_backup.snapshot import SnapshotResult


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

    minds injects restic.env into a running workspace to enable backups (no
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


def _read_events(events_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (events_dir / "events.jsonl").read_text().splitlines()
    ]


def test_every_way_a_tick_ends_emits_a_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive each way a tick can end and assert the endings match the terminal set.

    Four come from `_run_one_tick`'s step helpers; the last comes from the loop's
    outer handler, which is where a tick that raises ends up.

    `host-backup-now` waits for a member of TICK_TERMINAL_EVENT_TYPES, so a tick
    ending that emits something outside that set strands the caller until its
    timeout. Asserting set equality (not membership) catches drift in both
    directions: a new tick ending whose event was never added to the set, and a
    type in the set that no tick actually emits.
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
        stub = _ResticStub([_completed(returncode)])
        _run_restic_backup(
            state=state,
            config=_build_config(),
            snapshot=_direct_snapshot(),
            env_overrides={},
            backup_fn=stub.backup,
            unlock_fn=stub.unlock,
        )
        observed.add(last_event_type(state.events_dir))

    # An unhandled error caught by the loop's outer handler.
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "tick-error"
    _emit_tick_error(state, ValueError("boom"))
    observed.add(last_event_type(state.events_dir))

    assert observed == TICK_TERMINAL_EVENT_TYPES


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

    assert calls == [["uv", "run", "env-converge", "capture"]]
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


class _ResticStub:
    """Records restic backup/unlock calls and returns scripted results."""

    def __init__(self, backup_results: list[subprocess.CompletedProcess[str]]) -> None:
        self._backup_results = backup_results
        self.backup_calls = 0
        self.unlock_calls = 0

    def backup(self, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        result = self._backup_results[self.backup_calls]
        self.backup_calls += 1
        return result

    def unlock(self, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.unlock_calls += 1
        return _completed(0)


def _events_in(events_dir: Path | None) -> list[dict]:
    assert events_dir is not None
    return [
        json.loads(line)
        for line in (events_dir / "events.jsonl").read_text().splitlines()
    ]


def _run_backup_under_test(
    tmp_path: Path,
    stub: _ResticStub,
    *,
    initial_failures: int = 0,
) -> tuple[_LoopState, bool]:
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-under-test"
    state.consecutive_backup_failures = initial_failures
    succeeded = _run_restic_backup(
        state=state,
        config=_build_config(),
        snapshot=_direct_snapshot(),
        env_overrides={},
        backup_fn=stub.backup,
        unlock_fn=stub.unlock,
    )
    return state, succeeded


_LOCK_STDERR = (
    "unable to create lock in backend: repository is already locked exclusively "
    "by PID 1515556 on host by root"
)


def test_run_restic_backup_unlocks_and_retries_on_stale_lock(tmp_path: Path) -> None:
    """A lock error triggers `restic unlock` and one retry; the retry's success wins."""
    stub = _ResticStub(
        [
            _completed(1, stderr=_LOCK_STDERR),
            _completed(0, stdout='{"message_type":"summary","snapshot_id":"snap1"}'),
        ]
    )
    state, succeeded = _run_backup_under_test(tmp_path, stub, initial_failures=4)

    assert succeeded is True
    assert stub.backup_calls == 2
    assert stub.unlock_calls == 1
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
    stub = _ResticStub([_completed(1, stderr="network unreachable")])
    state, succeeded = _run_backup_under_test(tmp_path, stub)

    assert succeeded is False
    assert stub.backup_calls == 1
    assert stub.unlock_calls == 0
    failed_events = [
        e for e in _events_in(state.events_dir) if e["type"] == "RESTIC_BACKUP_FAILED"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["consecutive_failures"] == 1


def test_run_restic_backup_emits_alarm_after_threshold(tmp_path: Path) -> None:
    """Crossing the consecutive-failure threshold emits a durable escalation event."""
    stub = _ResticStub([_completed(1, stderr="backend error")])
    state, succeeded = _run_backup_under_test(
        tmp_path, stub, initial_failures=CONSECUTIVE_FAILURE_ALARM_THRESHOLD - 1
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
    stub = _ResticStub([_completed(1, stderr="backend error")])
    state, _ = _run_backup_under_test(tmp_path, stub)

    assert state.consecutive_backup_failures == 1
    alarms = [
        e
        for e in _events_in(state.events_dir)
        if e["type"] == "BACKUP_REPEATEDLY_FAILING"
    ]
    assert alarms == []


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


# ---------------------------------------------------------------------------
# Restore-marker age-out
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _snapshots_result(
    snapshots: list[dict[str, object]],
) -> subprocess.CompletedProcess[str]:
    return _completed(0, stdout=json.dumps(snapshots))


def _age_out_config(max_age_days: float) -> BackupConfig:
    return BackupConfig(
        retention=RetentionSettings(restore_marker_max_age_days=max_age_days)
    )


def _age_out_state(tmp_path: Path) -> _LoopState:
    state = _LoopState(_direct_capabilities())
    state.events_dir = tmp_path / "events"
    state.current_tick_id = "tick-age-out"
    return state


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

    state = _age_out_state(tmp_path)
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

    state = _age_out_state(tmp_path)
    _age_out_restore_markers(
        state=state,
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    assert forget_calls == []
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
        state=_age_out_state(tmp_path),
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
        state=_age_out_state(tmp_path),
        config=_age_out_config(7.0),
        env_overrides={},
        list_fn=_list_fn,
        forget_ids_fn=_forget_ids_fn,
        now_fn=lambda: _FIXED_NOW,
    )

    # A failed listing must not forget anything and must not raise.
    assert forget_calls == []
