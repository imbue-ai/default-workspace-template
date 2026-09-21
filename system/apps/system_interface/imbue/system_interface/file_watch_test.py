"""Tests for the shared single-file watch."""

from pathlib import Path

from watchdog.events import DirModifiedEvent
from watchdog.events import FileDeletedEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent

from imbue.system_interface.file_watch import make_file_change_handler


def test_the_handler_fires_for_the_watched_file_alone(tmp_path: Path) -> None:
    fired: list[bool] = []
    handler = make_file_change_handler("apps.toml", lambda: fired.append(True))
    handler.on_modified(FileModifiedEvent(str(tmp_path / "apps.toml")))
    # An atomic replacement is a move whose destination is the file.
    handler.on_moved(FileMovedEvent(str(tmp_path / "apps.toml.tmp-1"), str(tmp_path / "apps.toml")))
    handler.on_deleted(FileDeletedEvent(str(tmp_path / "apps.toml")))
    handler.on_modified(FileModifiedEvent(str(tmp_path / "apps.toml.tmp-2")))
    handler.on_modified(DirModifiedEvent(str(tmp_path)))
    assert len(fired) == 3
