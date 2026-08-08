"""Unit tests for the codex queued-message populator + its ledger-signal parser."""

import json

from imbue.system_interface.harnesses.codex.queue_tracker import CodexQueueTracker
from imbue.system_interface.harnesses.codex.session_parser import CodexQueueSignalKind
from imbue.system_interface.harnesses.codex.session_parser import parse_codex_queue_signals


def _enqueue(queued_id: str, content: str, timestamp: str = "2026-08-07T00:00:01Z") -> str:
    return json.dumps(
        {"type": "queued_input", "queued_id": queued_id, "thread_id": "t1", "timestamp": timestamp, "content": content}
    )


def _committed(queued_id: str) -> str:
    return json.dumps({"type": "queued_committed", "queued_id": queued_id, "timestamp": "2026-08-07T00:00:09Z"})


def _retracted(queued_id: str) -> str:
    return json.dumps({"type": "queued_retracted", "queued_id": queued_id, "timestamp": "2026-08-07T00:00:09Z"})


def _feed(tracker: CodexQueueTracker, *lines: str) -> None:
    for line in lines:
        signal = parse_codex_queue_signals(line)
        assert signal is not None, f"expected a signal for: {line}"
        tracker.consume(signal)


# --- parser ---------------------------------------------------------------


def test_parse_recognizes_the_three_record_types() -> None:
    enqueue = parse_codex_queue_signals(_enqueue("q1", "hello"))
    assert enqueue is not None
    assert enqueue.kind is CodexQueueSignalKind.ENQUEUE
    assert (enqueue.queued_id, enqueue.content) == ("q1", "hello")

    committed = parse_codex_queue_signals(_committed("q1"))
    assert committed is not None and committed.kind is CodexQueueSignalKind.LEAVE
    retracted = parse_codex_queue_signals(_retracted("q1"))
    assert retracted is not None and retracted.kind is CodexQueueSignalKind.LEAVE


def test_parse_returns_none_for_junk_blank_and_missing_id() -> None:
    assert parse_codex_queue_signals("") is None
    assert parse_codex_queue_signals("{not json") is None
    assert parse_codex_queue_signals(json.dumps({"type": "queued_input", "queued_id": "", "content": "x"})) is None
    assert parse_codex_queue_signals(json.dumps({"type": "queued_input", "queued_id": "q", "content": "  "})) is None
    assert parse_codex_queue_signals(json.dumps({"type": "other", "queued_id": "q"})) is None


# --- tracker --------------------------------------------------------------


def test_enqueue_surfaces_then_committed_clears() -> None:
    tracker = CodexQueueTracker.build()
    _feed(tracker, _enqueue("q1", "do gmail next"))
    assert tracker.snapshot() == [{"queued_id": "q1", "content": "do gmail next", "timestamp": "2026-08-07T00:00:01Z"}]

    _feed(tracker, _committed("q1"))
    assert tracker.snapshot() == []


def test_retracted_clears_the_entry() -> None:
    tracker = CodexQueueTracker.build()
    _feed(tracker, _enqueue("q1", "never mind"), _retracted("q1"))
    assert tracker.snapshot() == []


def test_resolution_is_by_id_even_for_duplicate_content() -> None:
    tracker = CodexQueueTracker.build()
    _feed(tracker, _enqueue("q1", "same"), _enqueue("q2", "same"), _committed("q1"))
    # Committing q1 by id leaves q2 -- positional resolution could not guarantee this.
    assert [m["queued_id"] for m in tracker.snapshot()] == ["q2"]


def test_full_replay_is_self_correcting() -> None:
    # Feeding the whole ledger from the start nets to exactly the still-parked set,
    # since each enqueue has exactly one terminating record (conservation).
    tracker = CodexQueueTracker.build()
    _feed(
        tracker,
        _enqueue("q1", "one"),
        _enqueue("q2", "two"),
        _committed("q1"),
        _enqueue("q3", "three"),
        _retracted("q2"),
    )
    assert [m["queued_id"] for m in tracker.snapshot()] == ["q3"]


def test_on_idle_backstop_clears() -> None:
    tracker = CodexQueueTracker.build()
    _feed(tracker, _enqueue("q1", "stranded"))
    tracker.on_idle()
    assert tracker.snapshot() == []


def test_concatenated_block_joins_pending_content() -> None:
    tracker = CodexQueueTracker.build()
    _feed(tracker, _enqueue("q1", "first"), _enqueue("q2", "second"))
    assert tracker.concatenated_block() == "first\nsecond"
