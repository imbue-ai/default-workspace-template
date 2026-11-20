"""Unit tests for the messages the chat holds for a grok agent: claims, settles, the stop's pause, and the journal."""

import os
import threading
from pathlib import Path
from typing import Any

from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import OUTBOX_FILENAME
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME

_RENDEZVOUS_SECONDS = 3.0
_MANY_HELD_MESSAGES = 150


def _tracker(tmp_path: Path, token: str = "session-1") -> GrokQueueTracker:
    return GrokQueueTracker.build(tmp_path / OUTBOX_FILENAME, token)


def _contents(tracker: GrokQueueTracker) -> list[str]:
    return [str(entry["content"]) for entry in tracker.snapshot()]


def _hold(tracker: GrokQueueTracker, *texts: str) -> None:
    for text in texts:
        tracker.enqueue(text, "t0", is_turn_open=True)


def test_enqueue_publishes_the_entry_and_wakes_the_worker_before_it_returns(tmp_path: Path) -> None:
    published: list[list[str]] = []
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(
        publish=lambda snapshot: published.append([e["content"] for e in snapshot]), wake=lambda: woken.append(True)
    )

    tracker.enqueue("first", "t0", is_turn_open=True)

    assert published == [["first"]]
    assert woken == [True]


def test_an_entry_with_no_turn_in_the_way_shows_as_sending_until_the_worker_finds_one(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("on its way", "t0", is_turn_open=False)
    tracker.enqueue("parked", "t1", is_turn_open=True)
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [True, False]

    assert tracker.demote_pending() is True
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [False, False]
    assert tracker.demote_pending() is False


def test_a_claim_keeps_its_entries_on_screen_as_sending_and_refuses_a_second_claim(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "first", "second")

    block, claimed, _generation = tracker.begin_flush()

    assert block == "first\nsecond"
    assert len(claimed) == 2
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [True, True]
    assert tracker.begin_flush()[1] == ()
    assert tracker.begin_tap_claim()[1] == ()


def test_a_delivered_flush_drops_its_entries_and_keeps_one_held_meanwhile(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "first")
    _block, claimed, generation = tracker.begin_flush()
    _hold(tracker, "sent during the flush")

    tracker.finish_flush(claimed, generation, delivered=claimed)

    assert _contents(tracker) == ["sent during the flush"]


def test_an_entry_stops_being_claimed_once_its_attempts_are_spent_and_stays_on_screen(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "never seen")
    for _attempt in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = tracker.begin_flush()
        assert claimed
        tracker.finish_flush(claimed, generation, delivered=())

    assert tracker.begin_flush()[1] == ()
    assert tracker.has_deliverable_entries() is False
    assert _contents(tracker) == ["never seen"]


def test_a_released_claim_spends_no_attempt(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "held")
    for _release in range(MAX_DELIVERY_ATTEMPTS + 1):
        _block, claimed, generation = tracker.begin_tap_claim()
        tracker.release_claim(claimed, generation)

    assert tracker.has_deliverable_entries() is True
    assert tracker.is_sending() is False


def test_stop_takes_what_a_tap_claimed_but_not_what_a_flush_is_typing(tmp_path: Path) -> None:
    tapped = _tracker(tmp_path / "tapped")
    _hold(tapped, "tap claimed")
    tapped.begin_tap_claim()
    assert tapped.take_unclaimed()[0] == "tap claimed"
    assert tapped.snapshot() == []

    flushed = _tracker(tmp_path / "flushed")
    _hold(flushed, "being typed")
    _block, claimed, generation = flushed.begin_flush()
    _hold(flushed, "held")
    assert flushed.take_unclaimed()[0] == "held"
    flushed.finish_flush(claimed, generation, delivered=claimed)
    assert flushed.snapshot() == []


def test_trackers_publishing_at_once_each_read_the_others_tap_state(tmp_path: Path) -> None:
    """A tracker publishes under its lock, and the chat-list push its publish runs reads every grok chat's tap state."""
    agent_dirs = [tmp_path / "first", tmp_path / "second"]
    for agent_dir in agent_dirs:
        agent_dir.mkdir()
    trackers = [_tracker(agent_dir) for agent_dir in agent_dirs]
    both_publishing = threading.Barrier(len(trackers), timeout=_RENDEZVOUS_SECONDS)
    tap_states: list[tuple[bool, bool]] = []
    for tracker, other in zip(trackers, reversed(trackers), strict=True):

        def read_the_other(_snapshot: list[dict[str, Any]], other: GrokQueueTracker = other) -> None:
            both_publishing.wait()
            tap_states.append((other.is_sending(), other.has_deliverable_entries()))

        tracker.attach(publish=read_the_other, wake=lambda: None)
    holds = [threading.Thread(target=_hold, args=(tracker, "held"), daemon=True) for tracker in trackers]

    for hold in holds:
        hold.start()
    for hold in holds:
        hold.join(timeout=_RENDEZVOUS_SECONDS)

    assert not any(hold.is_alive() for hold in holds)
    assert tap_states == [(False, True), (False, True)]


def test_a_paused_queue_refuses_a_flush_claim_until_it_resumes(tmp_path: Path) -> None:
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(publish=lambda _snapshot: None, wake=lambda: woken.append(True))
    _hold(tracker, "held")
    woken.clear()

    tracker.pause_flushes()
    assert tracker.begin_flush()[1] == ()
    tracker.resume_flushes()

    assert woken == [True]
    assert tracker.begin_flush()[1] != ()


def test_a_relaunch_voids_the_open_claim_so_its_settle_changes_nothing(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    _hold(tracker, "first")
    _block, claimed, generation = tracker.begin_flush()

    tracker.set_session("session-2")
    _hold(tracker, "after")
    tracker.finish_flush(claimed, generation, delivered=())

    assert _contents(tracker) == ["after"]
    assert tracker.begin_flush()[1] != ()


def test_note_flush_began_answers_when_an_earlier_flush_of_the_entries_began(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "first")
    _block, claimed, generation = tracker.begin_flush()
    assert tracker.note_flush_began(claimed, 100.0) is None
    tracker.finish_flush(claimed, generation, delivered=())

    _block, claimed, generation = tracker.begin_flush()
    assert tracker.note_flush_began(claimed, 200.0) == 100.0
    tracker.finish_flush(claimed, generation, delivered=claimed)
    _hold(tracker, "second")
    _block, claimed, _generation = tracker.begin_flush()
    assert tracker.note_flush_began(claimed, 300.0) is None


def test_the_queue_survives_a_chat_app_restart_but_not_a_grok_relaunch(tmp_path: Path) -> None:
    _hold(_tracker(tmp_path, token="session-1"), "held")

    assert _contents(_tracker(tmp_path, token="session-1")) == ["held"]
    assert _contents(_tracker(tmp_path, token="session-2")) == []


def test_set_session_drops_the_held_messages_of_a_grok_that_relaunched(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    _hold(tracker, "held")
    _block, claimed, generation = tracker.begin_flush()

    assert tracker.set_session("session-1") is False
    assert tracker.set_session("session-2") is True
    tracker.finish_flush(claimed, generation, delivered=())

    assert tracker.snapshot() == []
    assert not (tmp_path / OUTBOX_FILENAME).exists()


def test_no_session_journals_nothing(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="")
    _hold(tracker, "held")

    assert _contents(tracker) == ["held"]
    assert not (tmp_path / OUTBOX_FILENAME).exists()


def test_every_held_message_survives_a_chat_app_restart(tmp_path: Path) -> None:
    texts = [f"message {index}" for index in range(_MANY_HELD_MESSAGES)]
    _hold(_tracker(tmp_path), *texts)

    assert _contents(_tracker(tmp_path)) == texts


def test_an_entry_mngr_typed_shows_as_sending_until_its_flush_settles_but_a_restart_does_not_replay_it(
    tmp_path: Path,
) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "typed")
    _block, claimed, generation = tracker.begin_flush()
    _hold(tracker, "held during the flush")

    tracker.note_typed(claimed, generation)

    assert [(entry["content"], entry["is_sending"]) for entry in tracker.snapshot()] == [
        ("typed", True),
        ("held during the flush", False),
    ]
    assert _contents(_tracker(tmp_path)) == ["held during the flush"]


def test_spent_delivery_attempts_survive_a_chat_app_restart(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    _hold(tracker, "never seen")
    for _attempt in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = tracker.begin_flush()
        tracker.finish_flush(claimed, generation, delivered=())

    restarted = _tracker(tmp_path)

    assert _contents(restarted) == ["never seen"]
    assert restarted.has_deliverable_entries() is False
    assert restarted.begin_flush()[1] == ()


def test_a_torn_journal_line_is_skipped(tmp_path: Path) -> None:
    _hold(_tracker(tmp_path), "kept")
    with (tmp_path / OUTBOX_FILENAME).open("a") as outbox:
        outbox.write('{"session": "session-1", "queued_id": "grok-torn", "con')

    assert _contents(_tracker(tmp_path)) == ["kept"]


def test_the_session_token_is_the_launch_markers_mtime(tmp_path: Path) -> None:
    assert session_token(tmp_path) == ""
    marker = tmp_path / PROCESS_STARTED_MARKER_FILENAME
    marker.touch()
    os.utime(marker, ns=(1_000_000_000, 2_000_000_000))

    assert session_token(tmp_path) == "2000000000"


def test_get_tracker_is_one_tracker_per_agent_state_dir(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent-grok-tracker-identity"
    agent_state_dir.mkdir()

    assert get_tracker(agent_state_dir) is get_tracker(agent_state_dir)
