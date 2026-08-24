"""Unit tests for agy's held-queue store."""

from pathlib import Path

from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker


def _tracker(tmp_path: Path, session: str = "s1") -> AntigravityQueueTracker:
    return AntigravityQueueTracker.build(tmp_path / "agy_outbox.jsonl", session)


def test_enqueue_then_snapshot_preserves_order(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "2026-01-01T00:00:00Z")
    tracker.enqueue("bop", "2026-01-01T00:00:01Z")
    assert [entry["content"] for entry in tracker.snapshot()] == ["beep", "bop"]
    assert tracker.concatenated_block() == "beep\nbop"


def test_a_claimed_flush_keeps_entries_visible_as_sending(tmp_path: Path) -> None:
    """They must NOT vanish while the flush is in flight: blanking them and re-showing
    the turn later is the blink contract E1 describes."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    tracker.enqueue("bop", "t1")
    block, claimed = tracker.begin_flush()
    assert block == "beep\nbop"
    assert len(claimed) == 2
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [True, True]


def test_a_second_flush_claim_is_refused_while_one_is_open(tmp_path: Path) -> None:
    """The caller is level-triggered, so the claim has to be idempotent or a single
    queue gets delivered twice."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    tracker.begin_flush()
    assert tracker.begin_flush() == ("", ())


def test_a_delivered_flush_drops_the_entries(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _, claimed = tracker.begin_flush()
    tracker.finish_flush(claimed, is_delivered=True)
    assert tracker.snapshot() == []


def test_a_failed_flush_returns_the_entries_to_the_queue(tmp_path: Path) -> None:
    """Not delivered means still queued -- never silently dropped."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _, claimed = tracker.begin_flush()
    tracker.finish_flush(claimed, is_delivered=False)
    snapshot = tracker.snapshot()
    assert [entry["content"] for entry in snapshot] == ["beep"]
    assert snapshot[0]["is_sending"] is False


def test_a_message_appended_during_a_flush_is_not_swallowed(tmp_path: Path) -> None:
    """The flush claims a set of ids, not "the queue", so a send landing mid-flush
    survives the claim settling and waits for the next one."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _, claimed = tracker.begin_flush()
    tracker.enqueue("late", "t1")
    tracker.finish_flush(claimed, is_delivered=True)
    assert [entry["content"] for entry in tracker.snapshot()] == ["late"]


def test_the_queue_survives_a_backend_restart(tmp_path: Path) -> None:
    """The session is still alive, so the contract's "never silently dropped while the
    session lives" applies -- a system_interface bounce must not lose the queue."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    revived = _tracker(tmp_path)
    assert [entry["content"] for entry in revived.snapshot()] == ["beep"]


def test_the_queue_does_NOT_survive_a_new_session(tmp_path: Path) -> None:
    """The contract is absolute: a queue is never replayed or delivered across an
    agent restart. The session token is what enforces it."""
    tracker = _tracker(tmp_path, session="old-session")
    tracker.enqueue("beep", "t0")
    revived = _tracker(tmp_path, session="new-session")
    assert revived.snapshot() == []


def test_a_torn_journal_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    outbox = tmp_path / "agy_outbox.jsonl"
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    with outbox.open("a") as handle:
        handle.write('{"session": "s1", "queued_id": "half')
    revived = _tracker(tmp_path)
    assert [entry["content"] for entry in revived.snapshot()] == ["beep"]


def test_clear_drops_everything_and_delivers_nothing(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    tracker.clear()
    assert tracker.snapshot() == []
    # And the journal is emptied too, so a backend restart does not revive it.
    assert _tracker(tmp_path).snapshot() == []
