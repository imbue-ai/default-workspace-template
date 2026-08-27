"""Unit tests for agy's held queue: claims, settles, journal lifetime and publication."""

import json
from pathlib import Path

from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.system_interface.harnesses.antigravity.queue_tracker import session_token


def _tracker(tmp_path: Path, token: str = "session-1") -> AntigravityQueueTracker:
    return AntigravityQueueTracker.build(tmp_path / "agy_outbox.jsonl", token)


def _contents(tracker: AntigravityQueueTracker) -> list[str]:
    return [str(entry["content"]) for entry in tracker.snapshot()]


# --- ordering and visibility ------------------------------------------------------------


def test_enqueue_then_snapshot_preserves_order(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("first", "t0")
    tracker.enqueue("second", "t1")
    assert _contents(tracker) == ["first", "second"]
    assert tracker.concatenated_block() == "first\nsecond"


def test_enqueue_publishes_before_it_returns(tmp_path: Path) -> None:
    """The chip must exist by the time the POST returns (contract A1a/A2)."""
    published: list[list[str]] = []
    tracker = _tracker(tmp_path)
    tracker.attach(publish=lambda snap: published.append([str(e["content"]) for e in snap]), wake=lambda: None)
    tracker.enqueue("beep", "t0")
    assert published == [["beep"]]


def test_enqueue_wakes_the_only_typist(tmp_path: Path) -> None:
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(publish=lambda _snap: None, wake=lambda: woken.append(True))
    tracker.enqueue("beep", "t0")
    assert woken == [True]


def test_a_claimed_flush_keeps_entries_visible_as_sending(tmp_path: Path) -> None:
    """Removing them on claim and re-showing the turn later is the blink E1 describes."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _block, claimed, _gen = tracker.begin_flush()
    assert claimed
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [True]
    assert _contents(tracker) == ["beep"]


# --- the claim is the mutual exclusion ---------------------------------------------------


def test_a_second_claim_is_refused_while_one_is_open(tmp_path: Path) -> None:
    """This single check is what makes the worker, the tap and stop mutually exclusive."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _block, claimed, _gen = tracker.begin_flush()
    assert claimed
    assert tracker.begin_flush() == ("", (), 0)


def test_a_delivered_flush_drops_the_entries(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    block, claimed, generation = tracker.begin_flush()
    assert block == "beep"
    tracker.finish_flush(claimed, generation, delivered=claimed)
    assert tracker.snapshot() == []


def test_a_partial_delivery_resolves_only_the_covered_entries(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("one", "t0")
    tracker.enqueue("two", "t1")
    _block, claimed, generation = tracker.begin_flush()
    tracker.finish_flush(claimed, generation, delivered=claimed[:1])
    assert _contents(tracker) == ["two"]


def test_an_undelivered_flush_returns_the_entries_to_the_queue(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _block, claimed, generation = tracker.begin_flush()
    tracker.finish_flush(claimed, generation, delivered=())
    assert _contents(tracker) == ["beep"]
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [False]


def test_a_message_appended_during_a_flush_is_not_swallowed(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("claimed", "t0")
    _block, claimed, generation = tracker.begin_flush()
    tracker.enqueue("arrived-mid-flush", "t1")
    tracker.finish_flush(claimed, generation, delivered=claimed)
    assert _contents(tracker) == ["arrived-mid-flush"]


def test_a_tap_release_does_not_charge_a_delivery_attempt(tmp_path: Path) -> None:
    """The tap claims only to grey its button; it never attempts a delivery."""
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("beep", "t0")
    for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
        _block, claimed, generation = tracker.begin_flush()
        tracker.release_claim(claimed, generation)
    assert tracker.is_exhausted(queued_id) is False
    assert tracker.deliverable_block() == "beep"


# --- the attempt ceiling -----------------------------------------------------------------


def test_an_entry_stops_being_retried_after_the_attempt_ceiling(tmp_path: Path) -> None:
    """A delivery we cannot verify, retried forever, duplicates the user's message."""
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("beep", "t0")
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = tracker.begin_flush()
        tracker.finish_flush(claimed, generation, delivered=())
    assert tracker.is_exhausted(queued_id) is True
    assert tracker.deliverable_block() == "", "an exhausted entry is not retyped"
    assert _contents(tracker) == ["beep"], "but it stays visible rather than vanishing"


# --- generations: stop and session changes void claimed work -----------------------------


def test_take_unclaimed_leaves_a_claimed_entry_alone(tmp_path: Path) -> None:
    """Returning an in-flight entry to the composer while its send may still land is how one
    message becomes both Delivered and Returned."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("in-flight", "t0")
    _block, _claimed, _gen = tracker.begin_flush()
    tracker.enqueue("not-yet-sent", "t1")
    block, taken = tracker.take_unclaimed()
    assert block == "not-yet-sent"
    assert len(taken) == 1
    assert _contents(tracker) == ["in-flight"]


def test_a_settle_carrying_a_stale_generation_is_dropped(tmp_path: Path) -> None:
    """A flush that outlived a stop must not resurrect or double-resolve its entries."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    _block, claimed, generation = tracker.begin_flush()
    # A stop's RESTART path: it kills the send, so it voids the claim too.
    tracker.take_all()
    tracker.enqueue("sent-after-the-stop", "t1")
    tracker.finish_flush(claimed, generation, delivered=())
    # The restart took "beep" and voided its claim, so the dead flush's settle changes nothing.
    assert _contents(tracker) == ["sent-after-the-stop"]
    assert tracker.is_sending() is False


# --- journal lifetime (contract Part B) --------------------------------------------------


def test_the_queue_survives_a_backend_restart(tmp_path: Path) -> None:
    """The agy session is still alive, so its queue is still real."""
    first = _tracker(tmp_path)
    first.enqueue("beep", "t0")
    assert _contents(_tracker(tmp_path)) == ["beep"]


def test_the_queue_does_not_survive_a_new_session(tmp_path: Path) -> None:
    first = _tracker(tmp_path, token="session-1")
    first.enqueue("beep", "t0")
    assert _contents(_tracker(tmp_path, token="session-2")) == []


def test_set_session_discards_rather_than_carrying_the_queue_over(tmp_path: Path) -> None:
    """Part B: NEVER revived, NEVER auto-sent on resume. Swapping the token in place while
    keeping the entries would deliver a dead session's queue into a fresh agy."""
    tracker = _tracker(tmp_path, token="session-1")
    tracker.enqueue("beep", "t0")
    assert tracker.set_session("session-2") is True
    assert tracker.snapshot() == []
    assert not (tmp_path / "agy_outbox.jsonl").exists()


def test_set_session_to_the_same_token_is_a_no_op(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    tracker.enqueue("beep", "t0")
    assert tracker.set_session("session-1") is False
    assert _contents(tracker) == ["beep"]


def test_a_missing_marker_means_no_session_so_nothing_is_journalled(tmp_path: Path) -> None:
    """An empty token once compared equal to itself on replay, so a queue journalled with no
    marker survived every later restart and was auto-sent into a fresh session."""
    assert session_token(tmp_path) == ""
    tracker = _tracker(tmp_path, token="")
    tracker.enqueue("beep", "t0")
    assert not (tmp_path / "agy_outbox.jsonl").exists()
    assert _contents(_tracker(tmp_path, token="")) == []


def test_a_torn_journal_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    outbox = tmp_path / "agy_outbox.jsonl"
    good = json.dumps({"session": "session-1", "queued_id": "q1", "content": "kept", "timestamp": "t0"})
    outbox.write_text(good + '\n{"session": "session-1", "conte')
    assert _contents(_tracker(tmp_path, token="session-1")) == ["kept"]


def test_clear_empties_the_queue_and_the_journal(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    tracker.clear()
    assert tracker.snapshot() == []
    assert _contents(_tracker(tmp_path)) == []


def test_republish_announces_the_queue_unchanged(tmp_path: Path) -> None:
    """Level-triggered visibility: an untrack/re-track cycle otherwise leaves live entries
    with no chips until the next mutation (A1a's forbidden 'resurfaces later')."""
    published: list[list[str]] = []
    tracker = _tracker(tmp_path)
    tracker.enqueue("beep", "t0")
    tracker.attach(publish=lambda snap: published.append([str(e["content"]) for e in snap]), wake=lambda: None)
    tracker.republish()
    assert published == [["beep"]]


def test_take_unclaimed_does_not_void_the_flush_it_spares(tmp_path: Path) -> None:
    """It deliberately leaves a claimed entry to its in-flight send. Voiding that flush would
    make its settle stale, return the entry to the queue, and deliver a block agy had already
    committed a second time."""
    tracker = _tracker(tmp_path)
    tracker.enqueue("in-flight", "t0")
    _block, claimed, generation = tracker.begin_flush()
    tracker.enqueue("not-yet-sent", "t1")
    tracker.take_unclaimed()
    tracker.finish_flush(claimed, generation, delivered=claimed)
    assert _contents(tracker) == [], "the spared flush must still be able to settle"
