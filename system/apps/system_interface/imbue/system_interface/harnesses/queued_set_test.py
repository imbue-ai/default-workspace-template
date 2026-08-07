"""Unit tests for the shared, harness-agnostic queued-set entity."""

from imbue.system_interface.harnesses.queued_set import QueuedSet


def test_empty_set_snapshots_and_concatenates_to_nothing() -> None:
    queued_set = QueuedSet.build()
    assert queued_set.snapshot() == []
    assert queued_set.concatenated_block() == ""


def test_add_appends_in_fifo_order_and_snapshot_carries_the_wire_shape() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "first", "2026-08-07T00:00:01.000Z", False)
    queued_set.add("id-b", "second", "2026-08-07T00:00:02.000Z", False)

    assert queued_set.snapshot() == [
        {"queued_id": "id-a", "content": "first", "timestamp": "2026-08-07T00:00:01.000Z"},
        {"queued_id": "id-b", "content": "second", "timestamp": "2026-08-07T00:00:02.000Z"},
    ]


def test_resolve_oldest_drops_the_fifo_head() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "first", "t1", False)
    queued_set.add("id-b", "second", "t2", False)

    queued_set.resolve_oldest()

    assert [message.queued_id for message in queued_set.pending] == ["id-b"]


def test_resolve_oldest_on_empty_set_is_a_noop() -> None:
    queued_set = QueuedSet.build()
    queued_set.resolve_oldest()
    assert queued_set.snapshot() == []


def test_resolve_by_id_drops_the_named_entry_not_the_head() -> None:
    # Used by codex, whose leave records name which message left. Resolving a middle
    # entry by id leaves the others in order -- exact, content-free, no FIFO assumption.
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "first", "t1", False)
    queued_set.add("id-b", "second", "t2", False)
    queued_set.add("id-c", "third", "t3", False)

    queued_set.resolve("id-b")

    assert [message.queued_id for message in queued_set.pending] == ["id-a", "id-c"]


def test_resolve_by_unknown_id_is_a_noop() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "first", "t1", False)
    queued_set.resolve("id-does-not-exist")
    assert [message.queued_id for message in queued_set.pending] == ["id-a"]


def test_clear_drops_everything() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "first", "t1", False)
    queued_set.add("id-b", "second", "t2", False)

    queued_set.clear()

    assert queued_set.snapshot() == []


def test_concatenated_block_joins_real_content_with_newlines_in_enqueue_order() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "do the first thing", "t1", False)
    queued_set.add("id-b", "then the second", "t2", False)

    assert queued_set.concatenated_block() == "do the first thing\nthen the second"


def test_phantom_entries_hold_a_fifo_slot_but_never_surface() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-phantom", "<task-notification>...", "t1", True)
    queued_set.add("id-real", "real message", "t2", False)

    # Two entries occupy FIFO slots, but only the real one surfaces.
    assert len(queued_set.pending) == 2
    assert queued_set.snapshot() == [{"queued_id": "id-real", "content": "real message", "timestamp": "t2"}]
    assert queued_set.concatenated_block() == "real message"

    # A leave pops the phantom head (keeping alignment); the real entry survives.
    queued_set.resolve_oldest()
    assert queued_set.snapshot() == [{"queued_id": "id-real", "content": "real message", "timestamp": "t2"}]
    # Its own leave then pops the real entry.
    queued_set.resolve_oldest()
    assert queued_set.snapshot() == []


def test_duplicate_content_is_two_distinct_entries_each_resolved_once() -> None:
    queued_set = QueuedSet.build()
    queued_set.add("id-a", "same text", "t1", False)
    queued_set.add("id-b", "same text", "t2", False)

    queued_set.resolve_oldest()

    assert [message.queued_id for message in queued_set.pending] == ["id-b"]
    assert queued_set.concatenated_block() == "same text"
