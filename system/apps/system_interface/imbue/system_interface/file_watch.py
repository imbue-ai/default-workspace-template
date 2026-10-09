"""Changes watched with watchdog: one file's, by a handler that fires on the mutating events naming the file, shared
by the app inventory (the registry) and the avatar status reader (mngr's agents event file); and a whole tree's, by
a handler that fires on every mutating event under it."""

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


class TreeChangeHandler(FileSystemEventHandler):
    """Fires ``on_change`` on every mutating event anywhere under the watched directory, file or directory alike:
    the same mutation events ``FileChangeHandler`` subscribes to, so a read of a file under it never fires."""

    on_change: Callable[[], None]

    def _fire(self, event: FileSystemEvent) -> None:
        self.on_change()

    on_modified = _fire
    on_created = _fire
    on_deleted = _fire
    on_moved = _fire
    on_closed = _fire


def make_file_change_handler(basename: str, on_change: Callable[[], None]) -> FileChangeHandler:
    handler = FileChangeHandler()
    handler.basename = basename
    handler.on_change = on_change
    return handler


def make_tree_change_handler(on_change: Callable[[], None]) -> TreeChangeHandler:
    handler = TreeChangeHandler()
    handler.on_change = on_change
    return handler


def _start_observer(handler: FileSystemEventHandler, directory: Path, is_recursive: bool) -> BaseObserver | None:
    observer = _Observer()
    observer.schedule(handler, str(directory), recursive=is_recursive)
    observer.daemon = True
    try:
        observer.start()
    except OSError as e:
        logger.opt(exception=e).error("Failed to watch {}", directory)
        return None
    return observer


def start_file_watch(path: Path, on_change: Callable[[], None]) -> BaseObserver | None:
    """Watch ``path``'s directory, which must exist, calling ``on_change`` whenever the file is written, replaced,
    or removed; None (logged) when the watch cannot start."""
    return _start_observer(make_file_change_handler(path.name, on_change), path.parent, is_recursive=False)


def start_tree_watch(directory: Path, on_change: Callable[[], None]) -> BaseObserver | None:
    """Watch ``directory``, which must exist, and everything under it, calling ``on_change`` whenever a file or
    directory in it is created, written, replaced, or removed; None (logged) when the watch cannot start."""
    return _start_observer(make_tree_change_handler(on_change), directory, is_recursive=True)


def stop_file_watch(observer: BaseObserver) -> None:
    observer.stop()
    observer.join(timeout=5)
