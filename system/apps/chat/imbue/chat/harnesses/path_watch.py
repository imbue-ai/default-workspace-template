"""A shared "watch these paths -> call this callback" utility.

The one "watch these paths, wake this loop" block shared by everything that tails
files: the model tracking (re-deriving an agent's choice whenever its live
``model_state.json`` changes) and every store-backed session watcher
(claude/codex/pi, via ``StoreBackedWatcher``). This wraps
the one shared primitive that already exists --
:class:`~imbue.chat.watcher_common.WakeOnChangeHandler` plus
``POLL_INTERVAL_SECONDS`` -- into a small object that watches a set of paths and
invokes ``on_change`` on real filesystem events, batched to at most one call per
``min_cycle_interval_seconds`` (with the poll interval as a safety net for missed
events).

Per-path rule: an existing directory is watched recursively (so codex's rotating
rollout files under a stable sessions root all wake the loop without rescheduling);
anything else is watched via its parent directory (so a not-yet-created file --
claude's ``settings.json`` before first launch -- is caught the moment it appears).
Directories that do not exist yet are retried on each loop, mirroring the codex
watcher's lazy observer start.

Every watcher of one app watches through one :class:`SharedWatchObserver`, so watchers
of the same directory share its OS watch and watchdog's threads (one per observer, two per
watched directory): every claude chat on an account watches that account's whole projects
tree.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from imbue.chat.watcher_common import POLL_INTERVAL_SECONDS
from imbue.chat.watcher_common import WakeOnChangeHandler

# A watched directory: its path and whether its subdirectories are watched too.
_WatchKey = tuple[str, bool]


class SharedWatchObserver:
    """One watchdog observer that many path watchers share: one OS watch per directory, however many of them watch it."""

    _lock: threading.Lock
    # The watchdog Observer, started on the first subscription. ``Any`` because watchdog's
    # Observer is a factory alias, not a type expression the checker accepts.
    _observer: Any
    _watch_by_key: dict[_WatchKey, ObservedWatch]
    _handlers_by_key: dict[_WatchKey, set[FileSystemEventHandler]]
    _is_closed: bool

    @classmethod
    def build(cls) -> "SharedWatchObserver":
        self = cls.__new__(cls)
        self._lock = threading.Lock()
        self._observer = None
        self._watch_by_key = {}
        self._handlers_by_key = {}
        self._is_closed = False
        return self

    def subscribe(self, directory: Path, is_recursive: bool, handler: FileSystemEventHandler) -> bool:
        """Deliver ``directory``'s events to ``handler``; False once closed. Raises ``OSError`` when it cannot be watched."""
        key: _WatchKey = (str(directory), is_recursive)
        with self._lock:
            if self._is_closed:
                return False
            if self._observer is None:
                observer = Observer()
                observer.start()
                self._observer = observer
            watch = self._watch_by_key.get(key)
            if watch is not None:
                self._observer.add_handler_for_watch(handler, watch)
                self._handlers_by_key[key].add(handler)
                return True
            try:
                watch = self._observer.schedule(handler, key[0], recursive=is_recursive)
            except OSError:
                # watchdog registers the handler before starting the emitter that just failed.
                with suppress(KeyError):
                    self._observer.remove_handler_for_watch(handler, ObservedWatch(key[0], recursive=is_recursive))
                raise
            self._watch_by_key[key] = watch
            self._handlers_by_key[key] = {handler}
            return True

    def unsubscribe(self, directory: Path, is_recursive: bool, handler: FileSystemEventHandler) -> None:
        """Stop delivering ``directory``'s events to ``handler``, releasing the OS watch with its last subscriber,
        and the observer's own thread with the last watch: the next subscription starts a fresh one."""
        key: _WatchKey = (str(directory), is_recursive)
        with self._lock:
            handlers = self._handlers_by_key.get(key)
            if handlers is None or handler not in handlers:
                return
            handlers.discard(handler)
            watch = self._watch_by_key[key]
            if handlers:
                self._observer.remove_handler_for_watch(handler, watch)
                return
            del self._handlers_by_key[key]
            del self._watch_by_key[key]
            self._observer.unschedule(watch)
            if self._watch_by_key:
                return
            idle_observer = self._observer
            self._observer = None
        _stop_observer(idle_observer)

    def close(self) -> None:
        """Stop the observer and every watch it holds; later subscriptions are refused. Idempotent."""
        with self._lock:
            self._is_closed = True
            observer = self._observer
            self._observer = None
            self._watch_by_key.clear()
            self._handlers_by_key.clear()
        if observer is not None:
            _stop_observer(observer)


def _stop_observer(observer: Any) -> None:
    observer.stop()
    observer.join(timeout=5.0)


class PathWatcher:
    """Watches a fixed set of paths and calls ``on_change`` when any of them change.

    ``on_change`` is invoked once at start (so the initial value is derived) and
    then on every wake -- a watchdog event or the poll-interval timeout -- with wakes
    closer together than ``min_cycle_interval_seconds`` batched into one call. It must be
    cheap and idempotent: callers rely on their own no-op guard to suppress
    redundant work, exactly as the activity recompute does.
    """

    _paths: tuple[Path, ...]
    _on_change: Callable[[], None]
    _wake_event: threading.Event
    _stop_event: threading.Event
    _handler: WakeOnChangeHandler
    _watch_observer: SharedWatchObserver
    # Whether the observer is this watcher's alone, to close with it.
    _is_watch_observer_owned: bool
    # Guards the subscription handoff between the loop thread (which subscribes) and
    # whoever calls ``stop()`` (which unsubscribes).
    _subscription_lock: threading.Lock
    _watched: set[_WatchKey]
    _thread: threading.Thread | None
    _min_cycle_interval_seconds: float

    @classmethod
    def build(
        cls,
        paths: tuple[Path, ...],
        on_change: Callable[[], None],
        min_cycle_interval_seconds: float,
        watch_observer: SharedWatchObserver | None = None,
    ) -> "PathWatcher":
        """``min_cycle_interval_seconds`` batches wakes: a wake that comes sooner than that
        after the previous ``on_change`` started waits out the rest of it, so a burst of
        writes costs one call instead of one per write. Without a ``watch_observer`` the
        watcher watches through one of its own."""
        self = cls.__new__(cls)
        self._paths = paths
        self._on_change = on_change
        self._min_cycle_interval_seconds = min_cycle_interval_seconds
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._handler = WakeOnChangeHandler(self._wake_event)
        self._is_watch_observer_owned = watch_observer is None
        self._watch_observer = SharedWatchObserver.build() if watch_observer is None else watch_observer
        self._subscription_lock = threading.Lock()
        self._watched = set()
        self._thread = None
        return self

    def start(self) -> None:
        """Begin watching in a background thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="path-watcher")
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and release this watcher's watches. Idempotent."""
        self._stop_event.set()
        self._wake_event.set()
        # Join the loop thread before reading the subscriptions, because the loop thread is
        # what makes them: reading first would miss one it is only about to make, leaving
        # its OS watch running forever.
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # Take the subscriptions under the lock that guards making them, so each is either
        # already visible here (and released below) or never made at all --
        # ``_ensure_subscriptions`` refuses to subscribe once the stop event is set. This
        # holds even if the join above timed out.
        with self._subscription_lock:
            watched = list(self._watched)
            self._watched.clear()
        for directory, is_recursive in watched:
            self._watch_observer.unsubscribe(Path(directory), is_recursive, self._handler)
        if self._is_watch_observer_owned:
            self._watch_observer.close()

    def _run(self) -> None:
        self._ensure_subscriptions()
        cycle_started_at = time.monotonic()
        self._on_change()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            remaining = cycle_started_at + self._min_cycle_interval_seconds - time.monotonic()
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)
            # Cleared after the batching wait, so the wakes that land during it are absorbed.
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            # Retry subscribing to any dir that has since appeared, then re-derive.
            self._ensure_subscriptions()
            cycle_started_at = time.monotonic()
            self._on_change()

    def _ensure_subscriptions(self) -> None:
        """Subscribe to every watchable dir not already watched.

        For an existing directory path, watch it recursively; otherwise watch the
        path's parent directory (catching a file that does not exist yet). A dir
        that is still absent is skipped and retried on the next loop.
        """
        for path in self._paths:
            if path.is_dir():
                target, is_recursive = path, True
            else:
                target, is_recursive = path.parent, False
            if not target.is_dir():
                continue
            key: _WatchKey = (str(target), is_recursive)
            if key in self._watched:
                continue
            with self._subscription_lock:
                if self._stop_event.is_set():
                    return
                try:
                    is_subscribed = self._watch_observer.subscribe(target, is_recursive, self._handler)
                except OSError as e:
                    logger.debug("PathWatcher failed to schedule {}: {}", target, e)
                    continue
                if not is_subscribed:
                    return
                self._watched.add(key)
