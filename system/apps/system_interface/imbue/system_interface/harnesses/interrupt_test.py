"""Unit tests for the shared stop-button lock helpers and the locked restart-drain."""

import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.harnesses.interrupt import restart_drain_under_message_lock
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock


@contextmanager
def _hold_lock_via_other_fd(agent_state_dir: Path) -> Generator[None, None, None]:
    """Hold the agent's ``message.lock`` through a SEPARATE open file description, the way an
    in-flight mngr send does -- so ``try_hold_message_lock`` contends with it even in-process."""
    lock_path = agent_state_dir / MESSAGE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)


def test_try_hold_message_lock_acquires_when_free(tmp_path: Path) -> None:
    with try_hold_message_lock(tmp_path) as held:
        assert held is True


def test_try_hold_message_lock_yields_false_when_held(tmp_path: Path) -> None:
    # A send holds the lock through the whole bounded wait -> the stop gets False (and holds
    # nothing), which is its signal to fall back to the restart hammer.
    with _hold_lock_via_other_fd(tmp_path):
        with try_hold_message_lock(tmp_path, wait_seconds=0.1, poll_interval_seconds=0.01) as held:
            assert held is False


def test_try_hold_message_lock_acquires_once_the_holder_releases(tmp_path: Path) -> None:
    # It is a bounded WAIT, not an instant give-up: a send that releases within the window lets
    # the stop through, so a just-parked message is captured under the lock.
    ticks = {"n": 0}

    def _fake_now() -> float:
        return ticks["n"]

    lock_path = tmp_path / MESSAGE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    def _fake_sleep(_seconds: float) -> None:
        # Simulate the holder releasing after the first poll, then advance the clock.
        if ticks["n"] == 0:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        ticks["n"] += 1

    try:
        with try_hold_message_lock(tmp_path, wait_seconds=1.0, now=_fake_now, sleep=_fake_sleep) as held:
            assert held is True
    finally:
        holder.close()


def test_try_hold_message_lock_releases_so_a_later_acquire_succeeds(tmp_path: Path) -> None:
    with try_hold_message_lock(tmp_path) as first:
        assert first is True
    # The block exited and unlocked, so a fresh contender can take it immediately.
    with try_hold_message_lock(tmp_path, wait_seconds=0.1) as second:
        assert second is True


def _agent_info(agent_state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="stub-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "config",
    )


class _ParkingWatcher:
    """A watcher stand-in whose mirror gains a late-parked message on refresh (``get_all_events``),
    the way a send that was still in flight at the caller's last mirror read parks one."""

    def __init__(self, initial_block: str, refreshed_block: str) -> None:
        self._block = initial_block
        self._refreshed_block = refreshed_block
        self.refresh_calls = 0
        self.clear_calls = 0

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        self.refresh_calls += 1
        self._block = self._refreshed_block
        return []

    def get_queued_block(self) -> str:
        return self._block

    def clear_queue(self) -> None:
        self.clear_calls += 1


def test_restart_drain_under_message_lock_captures_a_late_parked_message(tmp_path: Path) -> None:
    # The refresh and the capture run under the lock, so a message that parked after the
    # caller's last mirror read is in the returned block (message conservation) rather than
    # dying silently with the SIGKILLed process.
    watcher = _ParkingWatcher("first message", "first message\n\nparked mid-stop")
    restarts: list[bool] = []
    block = restart_drain_under_message_lock(
        _agent_info(tmp_path), watcher, lambda: restarts.append(True) or (True, "ok"), lambda: None
    )
    assert block == "first message\n\nparked mid-stop"
    assert watcher.refresh_calls == 1
    assert restarts == [True]
    assert watcher.clear_calls == 1


def test_restart_drain_under_message_lock_hammers_when_the_lock_stays_held(tmp_path: Path) -> None:
    # Stop wins, bounded: a send holding the lock past the wait does not stall the drain -- it
    # still refreshes (a best-effort re-capture) and restarts without the lock. The injected
    # clock jumps straight past the bounded-wait deadline so the acquire gives up immediately.
    watcher = _ParkingWatcher("first message", "first message\n\nparked mid-stop")
    restarts: list[bool] = []
    ticks = iter([0.0, 1000.0])
    with _hold_lock_via_other_fd(tmp_path):
        block = restart_drain_under_message_lock(
            _agent_info(tmp_path),
            watcher,
            lambda: restarts.append(True) or (True, "ok"),
            lambda: None,
            now=lambda: next(ticks, 1000.0),
            sleep=lambda _seconds: None,
        )
    assert block == "first message\n\nparked mid-stop"
    assert watcher.refresh_calls == 1
    assert restarts == [True]
