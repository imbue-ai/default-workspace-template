"""Unit tests for the shared PathWatcher."""

import threading
import time
from pathlib import Path

from imbue.chat.harnesses.path_watch import PathWatcher


def test_path_watcher_derives_on_start_and_keeps_running(tmp_path: Path) -> None:
    """The watcher calls on_change once at start and then keeps calling it (the
    poll-interval safety net), even for a file that does not exist yet."""
    fired = threading.Event()
    count = {"n": 0}

    def on_change() -> None:
        count["n"] += 1
        fired.set()

    # Watch a not-yet-created file, exercising the parent-dir watch path.
    watcher = PathWatcher.build((tmp_path / "settings.json",), on_change)
    watcher.start()
    try:
        assert fired.wait(timeout=5.0)
        fired.clear()
        # A change (or the poll) drives at least one more call.
        (tmp_path / "settings.json").write_text("{}")
        assert fired.wait(timeout=5.0)
    finally:
        watcher.stop()

    assert count["n"] >= 2


def test_path_watcher_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = PathWatcher.build((tmp_path,), lambda: None)
    watcher.start()
    watcher.stop()
    watcher.stop()


def _keep_waking(watcher: PathWatcher, seconds: float) -> None:
    """Set the watcher's wake flag continuously for ``seconds``: what a stream of writes to
    a watched tree does, one watchdog event after another."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        watcher._wake_event.set()


def test_a_stream_of_wakes_is_batched_into_one_refresh_per_minimum_interval(tmp_path: Path) -> None:
    """Every chat's transcript watcher watches its account's whole projects tree, so any
    chat's write woke every watcher, and each wake was a full refresh. With a minimum
    interval, a burst of wakes costs at most one refresh per interval."""
    started_at: list[float] = []
    lock = threading.Lock()

    def on_change() -> None:
        with lock:
            started_at.append(time.monotonic())

    interval = 0.2
    watcher = PathWatcher.build((tmp_path,), on_change, min_cycle_interval_seconds=interval)
    watcher.start()
    try:
        waker = threading.Thread(target=_keep_waking, args=(watcher, 0.6))
        waker.start()
        waker.join()
    finally:
        watcher.stop()

    with lock:
        cycles = list(started_at)
    # 0.6s of wakes at one refresh per 0.2s, plus the initial derive and slack for the
    # last refresh landing just after the burst.
    assert 2 <= len(cycles) <= 6
    gaps = [later - earlier for earlier, later in zip(cycles, cycles[1:])]
    assert min(gaps) >= interval * 0.9


def test_stopping_a_batched_watcher_does_not_wait_out_its_interval(tmp_path: Path) -> None:
    derived = threading.Event()
    watcher = PathWatcher.build((tmp_path,), derived.set, min_cycle_interval_seconds=30.0)
    watcher.start()
    assert derived.wait(timeout=5.0)
    watcher._wake_event.set()

    stop_started_at = time.monotonic()
    watcher.stop()
    assert time.monotonic() - stop_started_at < 2.0
