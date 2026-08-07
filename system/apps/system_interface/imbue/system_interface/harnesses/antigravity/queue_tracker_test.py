"""Tests for :class:`antigravity.queue_tracker.AntigravityQueueTracker` -- the
send-sourced outbox populator over the shared :class:`QueuedSet`, with the
coalescing-aware verbatim front-run ``leave``."""

from __future__ import annotations

from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.model import QueueBehavior


def _contents(tracker: AntigravityQueueTracker) -> list[str]:
    return [entry["content"] for entry in tracker.snapshot()]


def test_enqueue_then_snapshot_in_order() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("first", "")
    tracker.enqueue("second", "")
    assert _contents(tracker) == ["first", "second"]


def test_single_drain_pops_one() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("first", "")
    tracker.enqueue("second", "")
    tracker.leave("first")
    assert _contents(tracker) == ["second"]


def test_coalesced_drain_pops_the_whole_front_run() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("A", "")
    tracker.enqueue("B", "")
    tracker.enqueue("C", "")
    tracker.leave("A\nB\nC")
    assert _contents(tracker) == []


def test_coalesced_drain_pops_only_its_run() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("A", "")
    tracker.enqueue("B", "")
    tracker.enqueue("C", "")
    tracker.leave("A\nB")
    assert _contents(tracker) == ["C"]


def test_multiline_entry_is_matched_whole_not_split() -> None:
    # Queue "A" then "B\nC": a drain of "A\nB\nC" is the k=2 join, so it pops exactly
    # two entries -- the naive pop-per-line would wrongly pop three.
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("A", "")
    tracker.enqueue("B\nC", "")
    tracker.enqueue("D", "")
    tracker.leave("A\nB\nC")
    assert _contents(tracker) == ["D"]


def test_duplicate_contents_pop_as_their_run() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("dup", "")
    tracker.enqueue("dup", "")
    tracker.leave("dup\ndup")
    assert _contents(tracker) == []


def test_duplicate_ids_are_distinct() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("dup", "")
    tracker.enqueue("dup", "")
    ids = [entry["queued_id"] for entry in tracker.snapshot()]
    assert len(set(ids)) == 2


def test_unmatched_drain_pops_nothing() -> None:
    # A turn we never enqueued (typed straight into agy's terminal) matches no
    # front-run and must not disturb the parked bubbles.
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("parked", "")
    tracker.leave("typed in the terminal")
    assert _contents(tracker) == ["parked"]


def test_leave_on_empty_is_a_noop() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.leave("anything")
    assert tracker.snapshot() == []


def test_whitespace_normalized_matching() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("hello  ", "")
    tracker.enqueue("world", "")
    tracker.leave("hello\r\nworld\n")
    assert _contents(tracker) == []


def test_task_notification_is_a_phantom_slot() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("<task-notification> background thing", "")
    tracker.enqueue("real message", "")
    # The phantom holds a FIFO slot but never surfaces.
    assert _contents(tracker) == ["real message"]


def test_normal_behavior_never_pops_a_coalesced_run() -> None:
    tracker = AntigravityQueueTracker.build(QueueBehavior.NORMAL)
    tracker.enqueue("A", "")
    tracker.enqueue("B", "")
    tracker.leave("A\nB")
    # NORMAL only checks k=1, so the coalesced join matches nothing.
    assert _contents(tracker) == ["A", "B"]
    tracker.leave("A")
    assert _contents(tracker) == ["B"]


def test_on_idle_clears() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("straggler", "")
    tracker.on_idle()
    assert tracker.snapshot() == []


def test_concatenated_block_joins_real_entries() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("<task-notification> hidden", "")
    tracker.enqueue("A", "")
    tracker.enqueue("B", "")
    assert tracker.concatenated_block() == "A\nB"


def test_reset_forgets_everything() -> None:
    tracker = AntigravityQueueTracker.build()
    tracker.enqueue("gone", "")
    tracker.reset()
    assert tracker.snapshot() == []
