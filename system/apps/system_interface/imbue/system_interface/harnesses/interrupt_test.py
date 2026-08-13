"""Unit tests for the shared stop-button lock helpers."""

import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.harnesses.interrupt import agent_message_lock
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


def test_agent_message_lock_blocks_a_bounded_contender(tmp_path: Path) -> None:
    # The blocking variant and the bounded variant share one lock: while the blocking one is
    # held, a bounded contender gives up with False.
    with agent_message_lock(tmp_path):
        with try_hold_message_lock(tmp_path, wait_seconds=0.05, poll_interval_seconds=0.01) as held:
            assert held is False
