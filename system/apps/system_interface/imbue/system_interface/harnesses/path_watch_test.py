"""Unit tests for the shared PathWatcher."""

import threading
from pathlib import Path

from imbue.system_interface.harnesses.path_watch import PathWatcher


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


def test_path_watcher_survives_a_raising_callback(tmp_path: Path) -> None:
    """A callback that raises must not end the thread and stop all future updates.

    The callback reads files other processes delete (an agent's model state during
    teardown), so a transient failure has to leave the watcher watching.
    """
    fired_again = threading.Event()
    count = {"n": 0}

    def on_change() -> None:
        count["n"] += 1
        if count["n"] == 1:
            raise FileNotFoundError(2, "No such file or directory", str(tmp_path / "gone.json"))
        fired_again.set()

    watcher = PathWatcher.build((tmp_path / "settings.json",), on_change)
    watcher.start()
    try:
        assert fired_again.wait(timeout=10.0), "watcher stopped calling on_change after it raised"
    finally:
        watcher.stop()

    assert count["n"] >= 2


def test_path_watcher_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = PathWatcher.build((tmp_path,), lambda: None)
    watcher.start()
    watcher.stop()
    watcher.stop()
