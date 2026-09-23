"""Unit tests for the shared PathWatcher."""

import threading
from pathlib import Path
from typing import Any

import pytest
from watchdog.utils import BaseThread

from imbue.chat.harnesses.path_watch import PathWatcher


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
    watcher = PathWatcher.build((watched_dir,), lambda: None)
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
    watcher = PathWatcher.build((tmp_path,), lambda: None)
    watcher.start()
    watcher.stop()
    watcher.stop()
