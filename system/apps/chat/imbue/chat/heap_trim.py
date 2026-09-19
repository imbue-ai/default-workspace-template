"""Hand the allocator's free heap back to the OS on a timer.

The app's steady-state work is a stream of short-lived allocations: every agent
event arrives as JSON, becomes pydantic models, is folded into the agent view and
dropped again. Those allocations are freed, but glibc keeps the pages: they sit in
the per-thread arenas the app's threads allocate from, and the process's RSS is the
high-water mark of everything it ever allocated at once rather than what it holds.
Measured on a workspace replaying its own agent stream: 176 MB of RSS growth with
Python's own tracked memory flat to within 0.2 MB, and ``malloc_trim`` returned
142 MB of it immediately.

Nothing here changes what the app retains -- that is a question about the app's
own structures. This only stops the memory it has already released from counting
against the workspace's memory budget (and, under earlyoom, against the chat app's
odds of being the process that gets shed).
"""

import ctypes
import threading
from collections.abc import Callable
from typing import Final

from loguru import logger

# Every minute: the allocator reclaims lazily, so the interval only bounds how long
# freed pages linger, and the call itself walks free lists rather than live objects.
DEFAULT_TRIM_INTERVAL_SECONDS: Final[float] = 60.0


def resolve_malloc_trim() -> Callable[[], int] | None:
    """glibc's ``malloc_trim``, or None where there is none (macOS, musl).

    The workspace runs glibc, but the app's tests and a developer's laptop may not,
    and a missing symbol is a reason to skip trimming rather than to fail.
    """
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = libc.malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return lambda: int(trim(0))


class HeapTrimmer:
    """Calls ``trim`` every ``interval_seconds`` on a thread of its own."""

    _trim: Callable[[], int] | None
    _interval_seconds: float
    _stop_event: threading.Event
    _thread: threading.Thread | None

    @classmethod
    def build(
        cls,
        trim: Callable[[], int] | None = None,
        interval_seconds: float = DEFAULT_TRIM_INTERVAL_SECONDS,
        resolve_trim: Callable[[], Callable[[], int] | None] = resolve_malloc_trim,
    ) -> "HeapTrimmer":
        self = cls.__new__(cls)
        self._trim = trim if trim is not None else resolve_trim()
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        return self

    def start(self) -> None:
        """Begin trimming. Idempotent, and a no-op where there is no ``malloc_trim``."""
        if self._trim is None:
            logger.debug("No malloc_trim on this platform; the chat app will not trim its heap")
            return
        if self._thread is not None:
            return
        # Cleared here, not in ``stop``: a start after a stop must run, and the flag a
        # previous stop set would otherwise end the new thread's first wait immediately.
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heap-trim")
        self._thread.start()

    def stop(self) -> None:
        """Stop trimming. Idempotent; safe if ``start`` never ran."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def trim_once(self) -> None:
        """Run one trim now, on the calling thread. A no-op without ``malloc_trim``."""
        if self._trim is None:
            return
        self._trim()

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._interval_seconds):
            self.trim_once()
