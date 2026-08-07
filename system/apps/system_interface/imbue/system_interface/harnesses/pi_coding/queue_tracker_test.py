"""Tests for :class:`pi_coding.queue_tracker.PiQueueTracker` -- the inbox-fed queue
populator over the shared :class:`QueuedSet`."""

from __future__ import annotations

from imbue.system_interface.harnesses.pi_coding.queue_tracker import PiQueueTracker


def _contents(tracker: PiQueueTracker) -> list[str]:
    return [entry["content"] for entry in tracker.snapshot()]


def test_enqueue_then_snapshot_in_order() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "first", "")
    tracker.enqueue(1, "second", "")
    assert _contents(tracker) == ["first", "second"]


def test_leave_drops_the_oldest() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "first", "")
    tracker.enqueue(1, "second", "")
    tracker.leave()
    assert _contents(tracker) == ["second"]


def test_leave_on_empty_is_a_noop() -> None:
    tracker = PiQueueTracker.build()
    tracker.leave()
    assert tracker.snapshot() == []


def test_task_notification_is_a_phantom_slot() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "<task-notification> background thing", "")
    tracker.enqueue(1, "real message", "")
    # The phantom holds a FIFO slot but never surfaces.
    assert _contents(tracker) == ["real message"]
    # ...and a single leave resolves the phantom head, leaving the real one.
    tracker.leave()
    assert _contents(tracker) == ["real message"]


def test_blank_message_is_a_phantom() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "   ", "")
    assert tracker.snapshot() == []


def test_clear_and_on_idle_empty_the_queue() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "a", "")
    tracker.on_idle()
    assert tracker.snapshot() == []
    tracker.enqueue(1, "b", "")
    tracker.clear()
    assert tracker.snapshot() == []


def test_concatenated_block_joins_real_entries_only() -> None:
    tracker = PiQueueTracker.build()
    tracker.enqueue(0, "<task-notification> bg", "")
    tracker.enqueue(1, "one", "")
    tracker.enqueue(2, "two", "")
    assert tracker.concatenated_block() == "one\ntwo"


def test_queued_ids_are_stable_and_distinct() -> None:
    a = PiQueueTracker.build()
    a.enqueue(0, "dup", "")
    a.enqueue(1, "dup", "")
    ids = [entry["queued_id"] for entry in a.snapshot()]
    # Same content at different inbox positions -> distinct ids.
    assert ids[0] != ids[1]
    # ...and a replay reproduces the same ids.
    b = PiQueueTracker.build()
    b.enqueue(0, "dup", "")
    b.enqueue(1, "dup", "")
    assert [entry["queued_id"] for entry in b.snapshot()] == ids
