import threading

import pytest

from imbue.chat.heap_trim import HeapTrimmer
from imbue.chat.heap_trim import resolve_malloc_trim
from imbue.mngr.utils.polling import poll_until


def _heap_trim_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "heap-trim"]


def test_the_trimmer_keeps_trimming_on_its_own_thread() -> None:
    """The point of the trimmer is the repeat: one trim at startup would return the
    heap once and never again."""
    calls: list[int] = []
    trimmer = HeapTrimmer.build(trim=lambda: calls.append(1) or 0, interval_seconds=0.01)

    trimmer.start()
    try:
        assert poll_until(lambda: len(calls) >= 3, timeout=5.0, poll_interval=0.01), (
            f"expected repeated trims, got {len(calls)}"
        )
        assert len(_heap_trim_threads()) == 1
    finally:
        trimmer.stop()

    # ``stop`` joins, so the thread that could call again is gone by the time it returns.
    assert _heap_trim_threads() == []


def test_a_platform_without_malloc_trim_starts_no_thread() -> None:
    """macOS and musl have no ``malloc_trim``; that is a reason to do nothing, not to
    fail at startup."""
    trimmer = HeapTrimmer.build(interval_seconds=0.01, resolve_trim=lambda: None)

    trimmer.start()
    try:
        assert _heap_trim_threads() == []
        trimmer.trim_once()
    finally:
        trimmer.stop()


def test_stop_is_idempotent_and_safe_without_start() -> None:
    trimmer = HeapTrimmer.build(trim=lambda: 0, interval_seconds=0.01)
    trimmer.stop()
    trimmer.start()
    trimmer.stop()
    trimmer.stop()


def test_a_trimmer_started_again_after_a_stop_really_trims() -> None:
    """A restarted trimmer must trim, not spawn a thread that exits on its first wait
    because the previous stop's flag is still set."""
    calls: list[int] = []
    trimmer = HeapTrimmer.build(trim=lambda: calls.append(1) or 0, interval_seconds=0.01)

    trimmer.start()
    assert poll_until(lambda: len(calls) >= 1, timeout=5.0, poll_interval=0.01)
    trimmer.stop()

    after_stop = len(calls)
    trimmer.start()
    try:
        assert poll_until(lambda: len(calls) > after_stop, timeout=5.0, poll_interval=0.01), (
            "a trimmer restarted after a stop never trimmed again"
        )
    finally:
        trimmer.stop()


def test_the_real_malloc_trim_is_callable_where_the_platform_has_it() -> None:
    """The resolved symbol is the actual allocator call the app relies on, so exercise
    it rather than trusting the lookup."""
    trim = resolve_malloc_trim()
    if trim is None:
        pytest.skip("no malloc_trim on this platform; there is no symbol to call")
    assert trim() in (0, 1)
