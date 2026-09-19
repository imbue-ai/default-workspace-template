import threading
import time

from imbue.chat.heap_trim import HeapTrimmer
from imbue.chat.heap_trim import resolve_malloc_trim


def test_the_trimmer_keeps_trimming_on_its_own_thread() -> None:
    """The point of the trimmer is the repeat: one trim at startup would return the
    heap once and never again."""
    calls: list[float] = []
    trimmer = HeapTrimmer.build(trim=lambda: calls.append(time.monotonic()) or 0, interval_seconds=0.01)

    threads_before = set(threading.enumerate())
    trimmer.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        new_threads = set(threading.enumerate()) - threads_before
        assert len(new_threads) == 1
        assert next(iter(new_threads)).name == "heap-trim"
    finally:
        trimmer.stop()

    assert len(calls) >= 3
    # Stopped means stopped: no further calls after stop() returns.
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) == settled


def test_a_platform_without_malloc_trim_starts_no_thread() -> None:
    """macOS and musl have no ``malloc_trim``; that is a reason to do nothing, not to
    fail at startup."""
    trimmer = HeapTrimmer.build(interval_seconds=0.01, resolve_trim=lambda: None)

    threads_before = set(threading.enumerate())
    trimmer.start()
    try:
        assert set(threading.enumerate()) == threads_before
        trimmer.trim_once()
    finally:
        trimmer.stop()


def test_stop_is_idempotent_and_safe_without_start() -> None:
    trimmer = HeapTrimmer.build(trim=lambda: 0, interval_seconds=0.01)
    trimmer.stop()
    trimmer.start()
    trimmer.stop()
    trimmer.stop()


def test_the_real_malloc_trim_is_callable_where_the_platform_has_it() -> None:
    """The resolved symbol is the actual allocator call the app relies on, so exercise
    it rather than trusting the lookup."""
    trim = resolve_malloc_trim()
    if trim is None:
        return
    assert trim() in (0, 1)
