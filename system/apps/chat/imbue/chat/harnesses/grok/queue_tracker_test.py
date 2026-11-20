"""Unit tests for the messages the chat holds for a grok agent: claims, settles, the stop's pause, the journal, and
the model bar's control lane."""

import os
from pathlib import Path

from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import OUTBOX_FILENAME
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.chat.harnesses.grok.testing import effort_command
from imbue.chat.harnesses.grok.testing import model_command
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME


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


def test_a_control_command_is_grok_s_own_slash_command() -> None:
    assert model_command("grok-4.5").text == "/model grok-4.5"
    assert effort_command("xhigh").text == "/effort xhigh"


def test_a_held_control_command_wakes_the_worker_and_is_never_published_or_journalled(tmp_path: Path) -> None:
    published: list[list[str]] = []
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(
        publish=lambda snapshot: published.append([e["content"] for e in snapshot]), wake=lambda: woken.append(True)
    )

    tracker.hold_control_command(model_command("grok-4.5"))

    assert woken == [True]
    assert published == []
    assert tracker.has_control_commands() is True
    assert tracker.has_entries() is False
    assert not (tmp_path / OUTBOX_FILENAME).exists()


def test_the_control_lane_offers_model_before_effort_and_keeps_the_newest_of_each(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.hold_control_command(effort_command("low"))
    tracker.hold_control_command(model_command("grok-4.6"))
    tracker.hold_control_command(effort_command("xhigh"))
    tracker.hold_control_command(model_command("grok-4.5"))

    first = tracker.next_control_command()
    assert first == model_command("grok-4.5")
    tracker.settle_control_command(first, is_delivered=True)
    second = tracker.next_control_command()
    assert second == effort_command("xhigh")
    tracker.settle_control_command(second, is_delivered=True)

    assert tracker.next_control_command() is None
    assert tracker.has_control_commands() is False


def test_no_control_command_is_offered_while_a_claim_is_open_or_a_stop_runs(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.hold_control_command(model_command("grok-4.5"))
    _hold(tracker, "held")

    _block, claimed, generation = tracker.begin_tap_claim()
    assert tracker.next_control_command() is None
    tracker.release_claim(claimed, generation)
    tracker.pause_flushes()
    assert tracker.next_control_command() is None
    tracker.resume_flushes()

    assert tracker.next_control_command() == model_command("grok-4.5")


def test_a_control_command_mngr_could_not_confirm_stays_held_until_its_attempts_are_spent(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.hold_control_command(effort_command("low"))

    for _attempt in range(MAX_DELIVERY_ATTEMPTS - 1):
        tracker.settle_control_command(effort_command("low"), is_delivered=False)
        assert tracker.next_control_command() == effort_command("low")
    tracker.settle_control_command(effort_command("low"), is_delivered=False)

    assert tracker.next_control_command() is None


def test_a_newer_pick_of_the_same_kind_survives_the_settle_of_the_one_it_replaced(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.hold_control_command(model_command("grok-4.6"))
    being_typed = tracker.next_control_command()
    assert being_typed is not None
    tracker.hold_control_command(model_command("grok-4.5"))

    tracker.settle_control_command(being_typed, is_delivered=True)

    assert tracker.next_control_command() == model_command("grok-4.5")


def test_a_newer_pick_starts_with_every_attempt_left(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.hold_control_command(effort_command("low"))
    for _attempt in range(MAX_DELIVERY_ATTEMPTS - 1):
        tracker.settle_control_command(effort_command("low"), is_delivered=False)

    tracker.hold_control_command(effort_command("low"))
    tracker.settle_control_command(effort_command("low"), is_delivered=False)

    assert tracker.next_control_command() == effort_command("low")


def test_the_control_lane_outlives_a_grok_relaunch_and_the_held_messages_stop_takes(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    tracker.hold_control_command(model_command("grok-4.5"))
    _hold(tracker, "held")

    assert tracker.take_unclaimed()[0] == "held"
    tracker.set_session("session-2")

    assert tracker.next_control_command() == model_command("grok-4.5")
