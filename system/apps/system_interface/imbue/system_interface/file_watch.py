"""One file's changes, watched with watchdog: the handler that fires on the mutating events naming the file and
the observer that carries it, shared by the app inventory (the registry) and the avatar status reader (mngr's
agents event file)."""

import os
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer
from watchdog.observers.api import BaseObserver


class FileChangeHandler(FileSystemEventHandler):
    """Fires ``on_change`` on mutating events whose path is the watched file.

    Subscribes to the mutation events rather than ``on_any_event``: watchdog's default inotify mask includes the
    open and close-no-write events a read of the file raises, which would loop. An atomic replacement is a move
    whose destination is the file, so moves count too, and so does a deletion.
    """

    basename: str
    on_change: Callable[[], None]

    def _maybe_fire(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(str(path)) == self.basename for path in paths):
            self.on_change()

    on_modified = _maybe_fire
    on_created = _maybe_fire
    on_deleted = _maybe_fire
    on_moved = _maybe_fire
    on_closed = _maybe_fire


def make_file_change_handler(basename: str, on_change: Callable[[], None]) -> FileChangeHandler:
    handler = FileChangeHandler()
    handler.basename = basename
    handler.on_change = on_change
    return handler


def start_file_watch(path: Path, on_change: Callable[[], None]) -> BaseObserver | None:
    """Watch ``path``'s directory, which must exist, calling ``on_change`` whenever the file is written, replaced,
    or removed; None (logged) when the watch cannot start."""
    observer = _Observer()
    observer.schedule(make_file_change_handler(path.name, on_change), str(path.parent))
    observer.daemon = True
    try:
        observer.start()
    except OSError as e:
        logger.opt(exception=e).error("Failed to watch {}", path)
        return None
    return observer


def stop_file_watch(observer: BaseObserver) -> None:
    observer.stop()
    observer.join(timeout=5)
