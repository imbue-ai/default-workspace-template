"""Unit tests for the shared PathWatcher."""

import threading
from pathlib import Path

import pytest

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


# Seen failing once in a loaded parallel run of the whole chat suite, and not
# reproduced in 30 solo runs, so the cause is still unconfirmed: stop() joins a
# watchdog observer thread, which the suite's 10s cap can catch under contention.
@pytest.mark.flaky
def test_path_watcher_stop_is_idempotent(tmp_path: Path) -> None:
    watcher = PathWatcher.build((tmp_path,), lambda: None)
    watcher.start()
    watcher.stop()
    watcher.stop()
