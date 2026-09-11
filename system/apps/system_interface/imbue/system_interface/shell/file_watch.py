"""Watching one file for changes: the registry and the update notice both live in a directory other
programs rewrite atomically, so the shell watches the directory and filters on the basename."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer


class FileChangeHandler(FileSystemEventHandler):
    """Fires ``on_change`` on mutating events whose path is the watched file.

    Subscribes to the mutation events rather than ``on_any_event``: watchdog's default inotify
    mask includes the open and close-no-write events a read of the file raises, which would loop.
    The writers replace the file atomically, so moves count too.
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


def watch_file(path: Path, on_change: Callable[[], None]) -> Any | None:
    """Start watching ``path`` (its directory is created if missing) and answer the running observer,
    or ``None`` when the watch could not start: a shell without a watch degrades to reading the file
    on demand, not to crashing."""
    watch_dir = path.parent
    watch_dir.mkdir(parents=True, exist_ok=True)
    observer = _Observer()
    observer.schedule(make_file_change_handler(path.name, on_change), str(watch_dir))
    observer.daemon = True
    try:
        observer.start()
    except OSError as e:
        logger.opt(exception=e).error("Failed to watch {}", path)
        return None
    return observer


def stop_watch(observer: Any | None) -> None:
    if observer is None:
        return
    observer.stop()
    observer.join(timeout=5)
