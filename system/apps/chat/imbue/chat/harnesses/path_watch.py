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
watcher's lazy observer start, and so is a watched directory that was deleted, whose watch
watchdog stops for good.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from imbue.chat.watcher_common import POLL_INTERVAL_SECONDS
from imbue.chat.watcher_common import WakeOnChangeHandler


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
    # Guards the observer handoff between the loop thread (which creates it) and
    # whoever calls ``stop()`` (which releases it).
    _observer_lock: threading.Lock
    # The watchdog Observer, once started. ``Any`` because watchdog's Observer is a
    # factory alias, not a type expression the checker accepts.
    _observer: Any
    _watch_by_key: dict[str, ObservedWatch]
    _thread: threading.Thread | None
    _min_cycle_interval_seconds: float

    @classmethod
    def build(
        cls, paths: tuple[Path, ...], on_change: Callable[[], None], min_cycle_interval_seconds: float
    ) -> "PathWatcher":
        """``min_cycle_interval_seconds`` batches wakes: a wake that comes sooner than that
        after the previous ``on_change`` started waits out the rest of it, so a burst of
        writes costs one call instead of one per write."""
        self = cls.__new__(cls)
        self._paths = paths
        self._on_change = on_change
        self._min_cycle_interval_seconds = min_cycle_interval_seconds
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer_lock = threading.Lock()
        self._observer = None
        self._watch_by_key = {}
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
        # Join the loop thread before reading the observer, because the loop thread is
        # what creates it: reading first would miss an observer the loop thread is only
        # about to start, leaving its emitter thread and OS watch running forever.
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # Take the observer under the lock that guards its creation, so an observer is
        # either already visible here (and stopped below) or never created at all --
        # ``_start_observer_if_needed`` refuses to create one once the stop event is set.
        # This holds even if the join above timed out.
        with self._observer_lock:
            observer = self._observer
            self._observer = None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5.0)

    def _run(self) -> None:
        self._ensure_observers()
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
            # Retry scheduling any dir that has since appeared, then re-derive.
            self._ensure_observers()
            cycle_started_at = time.monotonic()
            self._on_change()

    def _ensure_observers(self) -> None:
        """Schedule a watchdog handler for every watchable dir not already watched.

        For an existing directory path, watch it recursively; otherwise watch the
        path's parent directory (catching a file that does not exist yet). A dir
        that is still absent is skipped and retried on the next loop.
        """
        with self._observer_lock:
            running_observer = self._observer
        if running_observer is not None:
            self._forget_stopped_watches(running_observer)
        handler = WakeOnChangeHandler(self._wake_event)
        for path in self._paths:
            if path.is_dir():
                target, recursive = path, True
            else:
                target, recursive = path.parent, False
            if not target.is_dir():
                continue
            key = f"{target}:{recursive}"
            if key in self._watch_by_key:
                continue
            observer = self._start_observer_if_needed()
            if observer is None:
                return
            try:
                self._watch_by_key[key] = observer.schedule(handler, str(target), recursive=recursive)
            except OSError as e:
                logger.debug("PathWatcher failed to schedule {}: {}", target, e)

    def _forget_stopped_watches(self, observer: Any) -> None:
        """Unschedule each watch whose emitter stopped, which watchdog does once the watched directory is
        deleted, or whose emitter thread died.

        Scheduling the same path again would reuse the stopped emitter, so the watch is removed
        first and its key dropped, leaving the loop to schedule a fresh one.
        """
        running_watches = {
            emitter.watch
            for emitter in list(observer.emitters)
            if emitter.is_alive() and emitter.should_keep_running()
        }
        for key, watch in list(self._watch_by_key.items()):
            if watch not in running_watches:
                logger.debug("PathWatcher's watch on {} stopped; watching it again once it exists", watch.path)
                observer.unschedule(watch)
                del self._watch_by_key[key]

    def _start_observer_if_needed(self) -> Any:
        """Return the running watchdog observer, starting it on first use.

        Returns ``None`` once a stop has been requested, so a stop that arrives while
        this thread is still working its way here can never be overtaken by a freshly
        started observer that nobody owns. ``stop()`` takes this same lock, which makes
        the two outcomes exhaustive: either it sees an observer created here and stops
        it, or the stop event is already set and nothing is created.

        Assigning only AFTER ``start()`` also keeps ``stop()`` from joining an observer
        that was created but never started.
        """
        with self._observer_lock:
            if self._stop_event.is_set():
                return None
            if self._observer is None:
                observer = Observer()
                observer.start()
                self._observer = observer
            return self._observer
