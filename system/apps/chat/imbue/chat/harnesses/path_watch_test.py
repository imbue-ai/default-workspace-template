"""Unit tests for the shared PathWatcher."""

import threading
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.utils import BaseThread

from imbue.chat.harnesses.path_watch import PathWatcher
from imbue.chat.harnesses.path_watch import SharedWatchObserver
from imbue.chat.testing import wait_until_true


class _StopGatedDirPath(Path):
    """A real directory path whose ``is_dir()`` blocks until its gate is set.

    This exists to pin one interleaving: the watcher's loop thread calls
    ``is_dir()`` on every watched path before it can create the watchdog
    observer, so gating that call holds the loop thread short of observer
    creation until the test says otherwise. Everything else about the path is a
    real ``Path`` -- the directory is watched for real.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._gate = threading.Event()

    def with_segments(self, *pathsegments: Any) -> "_StopGatedDirPath":
        return type(self)(*pathsegments)

    def open_gate_when(self, gate: threading.Event) -> None:
        """Let ``is_dir()`` through only once ``gate`` is set."""
        self._gate = gate

    def is_dir(self) -> bool:
        assert self._gate.wait(timeout=10.0), "the gate was never opened"
        return super().is_dir()


def _live_watchdog_threads() -> set[BaseThread]:
    """Every live watchdog thread in this process (observers and their emitters).

    A leaked observer is invisible to an ordinary test -- watchdog's threads are
    daemons, so the process still exits cleanly -- and this is the only thing
    that actually distinguishes "released" from "still running".
    """
    return {thread for thread in threading.enumerate() if isinstance(thread, BaseThread) and thread.is_alive()}


def test_path_watcher_derives_on_start_and_keeps_running(tmp_path: Path) -> None:
    """The watcher calls on_change once at start and then keeps calling it (the
    poll-interval safety net), even for a file that does not exist yet."""
    fired = threading.Event()
    count = {"n": 0}

    def on_change() -> None:
        count["n"] += 1
        fired.set()

    # Watch a not-yet-created file, exercising the parent-dir watch path.
    watcher = PathWatcher.build((tmp_path / "settings.json",), on_change, min_cycle_interval_seconds=0.0)
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


def test_path_watcher_stop_releases_an_observer_the_loop_thread_creates_late(tmp_path: Path) -> None:
    """A single start()/stop() pair leaves no running watchdog observer behind.

    The observer is created by the loop thread but torn down by whoever calls
    ``stop()``, so ``stop()`` can read the observer before the loop thread has
    made one. The gate forces exactly that order: the loop thread cannot reach
    the creation site until the stop has already been requested. ``stop()`` must
    still end with nothing running -- either the loop thread declines to create
    an observer, or ``stop()`` stops the one it created.
    """
    before = _live_watchdog_threads()
    watched_dir = _StopGatedDirPath(tmp_path)
    watcher = PathWatcher.build((watched_dir,), lambda: None, min_cycle_interval_seconds=0.0)
    # Hold the loop thread short of observer creation until a stop is requested.
    watched_dir.open_gate_when(watcher._stop_event)

    watcher.start()
    watcher.stop()

    leaked = _live_watchdog_threads() - before
    assert not leaked, f"stop() left watchdog threads running: {sorted(type(t).__name__ for t in leaked)}"


# Failed once in a loaded parallel run of the whole chat suite, cause unconfirmed, so
# this stays marked flaky. The timeout covers the budget the code under test declares:
# stop() joins the watchdog observer and then the watcher thread for up to 5s each, and
# the test calls stop() twice, so the body can outlast the suite's 10s cap.
@pytest.mark.flaky
@pytest.mark.timeout(30)
def test_path_watcher_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = PathWatcher.build((tmp_path,), lambda: None, min_cycle_interval_seconds=0.0)
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
    chat's writes wake every watcher; a burst of wakes costs at most one refresh per
    minimum interval."""
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
    gaps = [later - earlier for earlier, later in pairwise(cycles)]
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


class _RecordingHandler(FileSystemEventHandler):
    """Sets ``changed`` on the first event naming a file other than the ones a test only reads."""

    def __init__(self) -> None:
        self.changed = threading.Event()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in ("opened", "closed", "closed_no_write"):
            self.changed.set()


def _wait_for_thread_count(expected: int) -> None:
    """Wait for the live watchdog thread count to reach ``expected``: an emitter's threads exit a moment after its
    watch is unscheduled."""
    wait_until_true(lambda: len(_live_watchdog_threads()) == expected, 5.0, f"{expected} live watchdog threads")


def test_watchers_sharing_an_observer_watch_one_directory_with_the_threads_of_one(tmp_path: Path) -> None:
    """Five watchers of one directory through one observer hold the threads one watcher holds, and stopping
    some of them takes nothing away from the rest."""
    before = len(_live_watchdog_threads())
    shared = SharedWatchObserver.build()
    derived = [threading.Event() for _ in range(5)]
    watchers = [
        PathWatcher.build((tmp_path,), derived_event.set, min_cycle_interval_seconds=0.0, watch_observer=shared)
        for derived_event in derived
    ]
    try:
        # A watcher derives only after subscribing, so each derive means its watch is in place.
        watchers[0].start()
        assert derived[0].wait(timeout=5.0)
        threads_for_one = len(_live_watchdog_threads()) - before
        assert threads_for_one > 0

        for watcher, derived_event in zip(watchers[1:], derived[1:], strict=True):
            watcher.start()
            assert derived_event.wait(timeout=5.0)
        assert len(_live_watchdog_threads()) - before == threads_for_one

        for watcher in watchers[:4]:
            watcher.stop()
        assert len(_live_watchdog_threads()) - before == threads_for_one
    finally:
        for watcher in watchers:
            watcher.stop()
        shared.close()
    _wait_for_thread_count(before)


def test_shared_observer_delivers_a_directory_event_to_every_subscriber(tmp_path: Path) -> None:
    shared = SharedWatchObserver.build()
    first, second = _RecordingHandler(), _RecordingHandler()
    try:
        assert shared.subscribe(tmp_path, False, first)
        assert shared.subscribe(tmp_path, False, second)
        (tmp_path / "written-91537.txt").write_text("x")
        assert first.changed.wait(timeout=5.0)
        assert second.changed.wait(timeout=5.0)
    finally:
        shared.close()


def test_the_last_unsubscribe_of_a_directory_gives_back_exactly_the_threads_its_watch_added(tmp_path: Path) -> None:
    """A directory's watch outlives every subscriber but the last, which still hears its events; the last one's
    unsubscribe releases the watch and nothing else the observer holds."""
    kept_dir, released_dir = tmp_path / "kept", tmp_path / "released"
    kept_dir.mkdir()
    released_dir.mkdir()
    shared = SharedWatchObserver.build()
    staying, leaving, other = _RecordingHandler(), _RecordingHandler(), _RecordingHandler()
    try:
        assert shared.subscribe(kept_dir, False, other)
        threads_without_released_dir = len(_live_watchdog_threads())
        assert shared.subscribe(released_dir, False, staying)
        assert shared.subscribe(released_dir, False, leaving)
        threads_with_released_dir = len(_live_watchdog_threads())
        assert threads_with_released_dir > threads_without_released_dir

        shared.unsubscribe(released_dir, False, leaving)
        assert len(_live_watchdog_threads()) == threads_with_released_dir
        (released_dir / "written-28461.txt").write_text("x")
        assert staying.changed.wait(timeout=5.0)
        assert not leaving.changed.is_set()

        shared.unsubscribe(released_dir, False, staying)
        _wait_for_thread_count(threads_without_released_dir)
        (kept_dir / "written-28462.txt").write_text("x")
        assert other.changed.wait(timeout=5.0)
    finally:
        shared.close()


def test_a_closed_shared_observer_stops_its_threads_and_refuses_new_subscriptions(tmp_path: Path) -> None:
    before = len(_live_watchdog_threads())
    shared = SharedWatchObserver.build()
    assert shared.subscribe(tmp_path, True, _RecordingHandler())
    assert len(_live_watchdog_threads()) > before

    shared.close()

    _wait_for_thread_count(before)
    assert not shared.subscribe(tmp_path, True, _RecordingHandler())
    assert len(_live_watchdog_threads()) == before
