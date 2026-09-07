"""A shared "watch these paths -> call this callback" utility.

The one "watch these paths, wake this loop" block shared by everything that tails
files: the model tracking (re-deriving an agent's choice whenever its live
``model_state.json`` changes) and every store-backed session watcher
(claude/codex/pi, via ``StoreBackedWatcher``). This wraps
the one shared primitive that already exists --
:class:`~imbue.system_interface.watcher_common.WakeOnChangeHandler` plus
``POLL_INTERVAL_SECONDS`` -- into a small object that watches a set of paths and
invokes ``on_change`` on every real filesystem event (with the poll interval as a
safety net for missed events).

Per-path rule: an existing directory is watched recursively (so codex's rotating
rollout files under a stable sessions root all wake the loop without rescheduling);
anything else is watched via its parent directory (so a not-yet-created file --
claude's ``settings.json`` before first launch -- is caught the moment it appears).
Directories that do not exist yet are retried on each loop, mirroring the codex
watcher's lazy observer start.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.observers import Observer

from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler


class PathWatcher:
    """Watches a fixed set of paths and calls ``on_change`` when any of them change.

    ``on_change`` is invoked once at start (so the initial value is derived) and
    then on every wake -- a watchdog event or the poll-interval timeout. It must be
    cheap and idempotent: callers rely on their own no-op guard to suppress
    redundant work, exactly as the activity recompute does.
    """

    _paths: tuple[Path, ...]
    _on_change: Callable[[], None]
    _wake_event: threading.Event
    _stop_event: threading.Event
    # The watchdog Observer, once started. ``Any`` because watchdog's Observer is a
    # factory alias, not a type expression the checker accepts.
    _observer: Any
    _watched_dirs: set[str]
    _thread: threading.Thread | None

    @classmethod
    def build(cls, paths: tuple[Path, ...], on_change: Callable[[], None]) -> "PathWatcher":
        self = cls.__new__(cls)
        self._paths = paths
        self._on_change = on_change
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer = None
        self._watched_dirs = set()
        self._thread = None
        return self

    def start(self) -> None:
        """Begin watching in a background thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="path-watcher")
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and release the observer. Idempotent."""
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        self._ensure_observers()
        self._on_change()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            # Retry scheduling any dir that has since appeared, then re-derive.
            self._ensure_observers()
            self._on_change()

    def _ensure_observers(self) -> None:
        """Schedule a watchdog handler for every watchable dir not already watched.

        For an existing directory path, watch it recursively; otherwise watch the
        path's parent directory (catching a file that does not exist yet). A dir
        that is still absent is skipped and retried on the next loop.
        """
        handler = WakeOnChangeHandler(self._wake_event)
        for path in self._paths:
            if path.is_dir():
                target, recursive = path, True
            else:
                target, recursive = path.parent, False
            if not target.is_dir():
                continue
            key = f"{target}:{recursive}"
            if key in self._watched_dirs:
                continue
            if self._observer is None:
                # Assign only AFTER start(), so a concurrent stop() never sees (and
                # joins) an observer that was created but not yet started.
                observer = Observer()
                observer.start()
                self._observer = observer
            try:
                self._observer.schedule(handler, str(target), recursive=recursive)
                self._watched_dirs.add(key)
            except OSError as e:
                logger.debug("PathWatcher failed to schedule {}: {}", target, e)
