import threading
from pathlib import Path

import pytest

from routines.errors import RunInFlightError
from routines.locks import held_lock
from routines.locks import waited_lock


def test_a_lock_held_for_an_instant_does_not_refuse_a_run_that_tries_again(tmp_path: Path) -> None:
    """The pause between tries is where the holder lets go, as a probe or a removal does within a moment."""
    lock_path = tmp_path / "run.lock"
    entered = threading.Event()
    released = threading.Event()
    holder_may_release = threading.Event()

    def hold() -> None:
        with held_lock(lock_path, "A run"):
            entered.set()
            holder_may_release.wait()
        released.set()

    holder = threading.Thread(target=hold)
    holder.start()
    entered.wait()
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        holder_may_release.set()
        released.wait()

    try:
        with held_lock(lock_path, "A run", retry_pauses=(0.1, 0.1), pause=pause):
            pass
    finally:
        holder_may_release.set()
        holder.join()

    assert pauses == [0.1]


def test_a_lock_held_past_every_try_is_refused(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    pauses: list[float] = []
    with held_lock(lock_path, "A run"), pytest.raises(RunInFlightError, match="A run is already going"):
        with held_lock(lock_path, "A run", retry_pauses=(0.1, 0.1), pause=pauses.append):
            pass
    assert pauses == [0.1, 0.1]


def test_a_waited_lock_waits_for_its_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "ledger.lock"
    entered = threading.Event()
    holder_may_release = threading.Event()

    def hold() -> None:
        with waited_lock(lock_path):
            entered.set()
            holder_may_release.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    entered.wait()
    taken = threading.Event()

    def take() -> None:
        with waited_lock(lock_path):
            taken.set()

    waiter = threading.Thread(target=take)
    waiter.start()
    assert not taken.wait(0.2), "the waiter took the lock while its holder had it"
    holder_may_release.set()
    assert taken.wait(5)
    holder.join()
    waiter.join()
