"""Tests for the messages the chat holds for a grok agent: claims, settles, waiting sends, the journal and the control lane."""

import json
import threading
from pathlib import Path

import pytest

from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import GrokSendResult
from imbue.chat.harnesses.grok.queue_tracker import GrokSendVerdict
from imbue.chat.harnesses.grok.queue_tracker import InvalidControlCommandError
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import OUTBOX_FILENAME
from imbue.chat.harnesses.grok.queue_tracker import enqueue_control_command
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME

_SENT = GrokSendResult(verdict=GrokSendVerdict.SENT, reason="", failure_kind="")
_BUSY = GrokSendResult(verdict=GrokSendVerdict.BUSY, reason="grok is running a turn", failure_kind="agent_busy")
_BLOCKED = GrokSendResult(verdict=GrokSendVerdict.BLOCKED, reason="paywall", failure_kind="input_blocked")
_FAILED = GrokSendResult(verdict=GrokSendVerdict.FAILED, reason="pane gone", failure_kind="agent_unreachable")


def _tracker(tmp_path: Path, token: str = "session-1") -> GrokQueueTracker:
    return GrokQueueTracker.build(tmp_path / OUTBOX_FILENAME, token)


def _contents(tracker: GrokQueueTracker) -> list[str]:
    return [str(entry["content"]) for entry in tracker.snapshot()]


def _sending_flags(tracker: GrokQueueTracker) -> list[bool]:
    return [bool(entry["is_sending"]) for entry in tracker.snapshot()]


def test_enqueue_publishes_and_wakes_the_worker_before_it_returns(tmp_path: Path) -> None:
    published: list[list[str]] = []
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(
        publish=lambda snapshot: published.append([str(e["content"]) for e in snapshot]),
        wake=lambda: woken.append(True),
    )

    queued_id = tracker.enqueue("first", "t0", is_turn_open=True)
    tracker.enqueue("second", "t1", is_turn_open=True)

    assert queued_id.startswith("grok-")
    assert published == [["first"], ["first", "second"]]
    assert woken == [True, True]
    assert tracker.concatenated_block() == "first\nsecond"


def test_detaching_a_replaced_watcher_keeps_the_hooks_of_the_one_that_attached_since(tmp_path: Path) -> None:
    published: list[str] = []
    woken: list[str] = []
    tracker = _tracker(tmp_path)

    def old_publish(_snapshot: list[dict[str, object]]) -> None:
        published.append("old")

    def new_publish(_snapshot: list[dict[str, object]]) -> None:
        published.append("new")

    tracker.attach(publish=old_publish, wake=lambda: woken.append("old"))
    tracker.attach(publish=new_publish, wake=lambda: woken.append("new"))
    tracker.detach(old_publish)
    tracker.enqueue("after the old watcher stopped", "t0", is_turn_open=True)
    tracker.detach(new_publish)
    tracker.enqueue("after both stopped", "t1", is_turn_open=True)

    assert (published, woken) == (["new"], ["new"])


def test_a_message_with_no_turn_in_the_way_shows_as_sending_and_one_behind_a_turn_as_queued(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("idle", "t0", is_turn_open=False)
    tracker.enqueue("behind a turn", "t1", is_turn_open=True)

    assert _sending_flags(tracker) == [True, False]
    assert tracker.is_sending() is True
    assert tracker.demote_pending() is True
    assert _sending_flags(tracker) == [False, False]
    assert tracker.demote_pending() is False


def test_a_claim_keeps_its_entries_visible_and_refuses_a_second_claim(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("one", "t0", is_turn_open=True)
    tracker.enqueue("two", "t1", is_turn_open=True)

    block, claimed, _generation = tracker.begin_flush()

    assert block == "one\ntwo"
    assert len(claimed) == 2
    assert _sending_flags(tracker) == [True, True]
    assert tracker.begin_flush()[1] == ()


@pytest.mark.parametrize(
    ("contents", "blocks"),
    [
        (["/new"], ["/new"]),
        (["one", "two", "/timestamps", "three"], ["one\ntwo", "/timestamps", "three"]),
        (["  /loop 5m check the build", "after"], ["  /loop 5m check the build", "after"]),
        (["/btw first", "/btw second"], ["/btw first", "/btw second"]),
    ],
    ids=["alone", "between_messages", "leading_space", "two_commands"],
)
def test_a_slash_command_is_claimed_alone_in_send_order(
    tmp_path: Path, contents: list[str], blocks: list[str]
) -> None:
    """grok may run a slash command itself, and in a block it would not be a command at all."""
    tracker = _tracker(tmp_path)
    for index, content in enumerate(contents):
        tracker.enqueue(content, f"t{index}", is_turn_open=True)

    claimed_blocks: list[str] = []
    for _ in contents:
        block, claimed, generation = tracker.begin_flush()
        if not claimed:
            break
        claimed_blocks.append(block)
        tracker.finish_flush(claimed, generation, delivered=claimed, result=_SENT)

    assert claimed_blocks == blocks
    assert tracker.snapshot() == []


def test_a_delivered_flush_drops_its_entries_and_keeps_one_sent_meanwhile(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("claimed", "t0", is_turn_open=True)
    _block, claimed, generation = tracker.begin_flush()
    tracker.enqueue("arrived mid-flush", "t1", is_turn_open=True)

    tracker.finish_flush(claimed, generation, delivered=claimed, result=_SENT)

    assert _contents(tracker) == ["arrived mid-flush"]


def test_a_partial_delivery_keeps_the_entries_grok_did_not_commit(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("one", "t0", is_turn_open=True)
    tracker.enqueue("two", "t1", is_turn_open=True)
    _block, claimed, generation = tracker.begin_flush()

    tracker.finish_flush(claimed, generation, delivered=claimed[:1], result=_SENT)

    assert _contents(tracker) == ["two"]
    assert _sending_flags(tracker) == [False]


def test_an_entry_stops_being_sent_after_its_attempts_but_stays_visible(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("never committed", "t0", is_turn_open=True)
    assert tracker.has_deliverable_entries() is True
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = tracker.begin_flush()
        tracker.finish_flush(claimed, generation, delivered=(), result=_SENT)

    assert tracker.is_exhausted(queued_id) is True
    assert tracker.has_deliverable_entries() is False
    assert tracker.begin_flush()[1] == ()
    assert _contents(tracker) == ["never committed"]


@pytest.mark.parametrize("result", [_BUSY, _BLOCKED], ids=["busy", "blocked"])
def test_a_send_mngr_typed_nothing_for_charges_no_attempt_and_leaves_the_entry_queued(
    tmp_path: Path, result: GrokSendResult
) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("held", "t0", is_turn_open=False)
    for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
        _block, claimed, generation = tracker.begin_flush()
        assert claimed == (queued_id,)
        tracker.refuse_flush(claimed, generation, result)

    assert tracker.is_exhausted(queued_id) is False
    assert _sending_flags(tracker) == [False]
    assert tracker.is_sending() is False


def test_a_tap_release_charges_no_attempt(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("held", "t0", is_turn_open=True)
    for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
        _block, claimed, generation = tracker.begin_flush()
        tracker.release_claim(claimed, generation)

    assert tracker.is_exhausted(queued_id) is False
    assert tracker.is_sending() is False


def test_stop_takes_only_unclaimed_entries_and_the_spared_flush_still_settles(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("in flight", "t0", is_turn_open=True)
    _block, claimed, generation = tracker.begin_flush()
    tracker.enqueue("not yet sent", "t1", is_turn_open=True)

    block, taken = tracker.take_unclaimed()
    tracker.finish_flush(claimed, generation, delivered=claimed, result=_SENT)

    assert (block, len(taken)) == ("not yet sent", 1)
    assert tracker.snapshot() == []


def test_the_restart_takes_everything_and_voids_the_claim(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("in flight", "t0", is_turn_open=True)
    _block, claimed, generation = tracker.begin_flush()
    tracker.enqueue("queued", "t1", is_turn_open=True)

    block, taken = tracker.take_all()
    tracker.enqueue("sent after the stop", "t2", is_turn_open=True)
    tracker.finish_flush(claimed, generation, delivered=(), result=_SENT)

    assert (block, len(taken)) == ("in flight\nqueued", 2)
    assert _contents(tracker) == ["sent after the stop"]
    assert tracker.is_sending() is False


def test_withdraw_takes_back_an_unclaimed_entry_only(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    first = tracker.enqueue("first", "t0", is_turn_open=True)
    _block, claimed, _generation = tracker.begin_flush()
    second = tracker.enqueue("second", "t1", is_turn_open=True)

    assert tracker.withdraw(first) is False
    assert tracker.withdraw(second) is True
    assert tracker.withdraw(second) is False
    assert _contents(tracker) == ["first"]
    assert claimed == (first,)


# A send's wait for its first delivery attempt


def _wait_in_thread(tracker: GrokQueueTracker, queued_id: str) -> tuple[threading.Thread, list[GrokSendResult | None]]:
    results: list[GrokSendResult | None] = []
    waiter = threading.Thread(target=lambda: results.append(tracker.wait_for_send_result(queued_id, 5.0)))
    waiter.start()
    return waiter, results


@pytest.mark.parametrize("result", [_BUSY, _BLOCKED], ids=["busy", "blocked"])
def test_a_waiting_send_gets_the_verdict_of_a_refused_flush(tmp_path: Path, result: GrokSendResult) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)
    waiter, results = _wait_in_thread(tracker, queued_id)
    _block, claimed, generation = tracker.begin_flush()

    tracker.refuse_flush(claimed, generation, result)
    waiter.join(timeout=5.0)

    assert results == [result]


@pytest.mark.parametrize("result", [_BUSY, _BLOCKED], ids=["busy", "blocked"])
def test_a_refusal_of_the_command_ahead_answers_the_waiting_sends_and_leaves_them_queued(
    tmp_path: Path, result: GrokSendResult
) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)
    tracker.enqueue("behind a turn", "t1", is_turn_open=True)
    waiter, results = _wait_in_thread(tracker, queued_id)

    tracker.refuse_pending(result)
    waiter.join(timeout=5.0)

    assert results == [result]
    assert _contents(tracker) == ["idle send", "behind a turn"]
    assert _sending_flags(tracker) == [False, False]
    assert tracker.is_exhausted(queued_id) is False


def test_a_refusal_of_the_command_ahead_leaves_a_claimed_flush_alone(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue("claimed", "t0", is_turn_open=False)
    _block, claimed, generation = tracker.begin_flush()

    tracker.refuse_pending(_BLOCKED)

    assert _sending_flags(tracker) == [True]
    tracker.finish_flush(claimed, generation, delivered=claimed, result=_SENT)
    assert tracker.snapshot() == []


def test_a_waiting_send_gets_the_verdict_once_its_flush_settles(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)
    waiter, results = _wait_in_thread(tracker, queued_id)
    _block, claimed, generation = tracker.begin_flush()

    tracker.finish_flush(claimed, generation, delivered=(), result=_FAILED)
    waiter.join(timeout=5.0)

    assert results == [_FAILED]
    assert tracker.withdraw(queued_id) is True


def test_a_waiting_send_whose_message_a_turn_now_holds_back_gets_no_verdict(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)
    waiter, results = _wait_in_thread(tracker, queued_id)

    tracker.demote_pending()
    waiter.join(timeout=5.0)

    assert results == [None]
    assert _contents(tracker) == ["idle send"]


def test_a_send_held_behind_a_turn_does_not_wait(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("behind a turn", "t0", is_turn_open=True)

    assert tracker.wait_for_send_result(queued_id, 5.0) is None


def test_a_waiting_send_times_out_when_no_worker_settles_it(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)

    assert tracker.wait_for_send_result(queued_id, 0.05) is None
    assert _sending_flags(tracker) == [True]


def test_a_first_send_that_settles_after_its_wait_gave_up_is_not_kept(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("idle send", "t0", is_turn_open=False)
    assert tracker.wait_for_send_result(queued_id, 0.0) is None
    _block, claimed, generation = tracker.begin_flush()

    tracker.finish_flush(claimed, generation, delivered=(), result=_FAILED)

    assert tracker.wait_for_send_result(queued_id, 0.0) is None


# The journal's lifetime (contract Part B)


def test_the_session_token_is_the_launch_markers_mtime_or_none(tmp_path: Path) -> None:
    assert session_token(tmp_path) == ""
    (tmp_path / PROCESS_STARTED_MARKER_FILENAME).write_text("")

    assert session_token(tmp_path) == str((tmp_path / PROCESS_STARTED_MARKER_FILENAME).stat().st_mtime_ns)


def test_the_queue_survives_a_chat_app_restart_but_not_a_grok_relaunch(tmp_path: Path) -> None:
    _tracker(tmp_path, token="session-1").enqueue("held", "t0", is_turn_open=True)

    assert _contents(_tracker(tmp_path, token="session-1")) == ["held"]
    assert _contents(_tracker(tmp_path, token="session-2")) == []


def test_adopting_a_new_session_drops_the_held_messages_and_the_journal(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    tracker.enqueue("held", "t0", is_turn_open=True)

    assert tracker.set_session("session-1") is False
    assert tracker.set_session("session-2") is True
    assert tracker.snapshot() == []
    assert not (tmp_path / OUTBOX_FILENAME).exists()


def test_an_unknown_session_keeps_the_held_messages(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    tracker.enqueue("held", "t0", is_turn_open=True)

    assert tracker.set_session(None) is False
    assert _contents(tracker) == ["held"]
    assert _contents(_tracker(tmp_path, token="session-1")) == ["held"]


def test_a_tracker_built_while_the_session_is_unknown_keeps_the_journal_and_adopts_it(tmp_path: Path) -> None:
    _tracker(tmp_path, token="session-1").enqueue("journalled", "t0", is_turn_open=True)

    tracker = GrokQueueTracker.build(tmp_path / OUTBOX_FILENAME, None)
    tracker.enqueue("held meanwhile", "t1", is_turn_open=True)
    assert _contents(_tracker(tmp_path, token="session-1")) == ["journalled"]

    assert tracker.set_session("session-1") is True
    assert _contents(tracker) == ["journalled", "held meanwhile"]
    assert _contents(_tracker(tmp_path, token="session-1")) == ["journalled", "held meanwhile"]


def test_no_session_journals_nothing(tmp_path: Path) -> None:
    _tracker(tmp_path, token="").enqueue("held", "t0", is_turn_open=True)

    assert not (tmp_path / OUTBOX_FILENAME).exists()
    assert _contents(_tracker(tmp_path, token="")) == []


def test_a_torn_journal_line_is_skipped(tmp_path: Path) -> None:
    kept = json.dumps({"session": "session-1", "queued_id": "grok-1", "content": "kept", "timestamp": "t0"})
    (tmp_path / OUTBOX_FILENAME).write_text(kept + '\n{"session": "session-1", "conte')

    assert _contents(_tracker(tmp_path)) == ["kept"]


def test_an_unreadable_journal_reads_as_empty_and_an_unreadable_launch_marker_as_unknown_and_both_are_logged(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    (tmp_path / OUTBOX_FILENAME).mkdir()
    not_a_state_dir = tmp_path / "not-a-dir"
    not_a_state_dir.write_text("")

    assert _contents(_tracker(tmp_path)) == []
    assert session_token(not_a_state_dir) is None
    assert [record.split(" ", 1)[0] for record in loguru_records if "could not read the" in record] == [
        "WARNING",
        "WARNING",
    ]


def test_a_journal_that_cannot_be_removed_is_logged_and_the_emptied_queue_still_publishes(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    published: list[list[str]] = []
    tracker = _tracker(tmp_path)
    queued_id = tracker.enqueue("held", "t0", is_turn_open=True)
    (tmp_path / OUTBOX_FILENAME).unlink()
    (tmp_path / OUTBOX_FILENAME).mkdir()
    tracker.attach(publish=lambda snapshot: published.append([str(e["content"]) for e in snapshot]), wake=lambda: None)

    assert tracker.withdraw(queued_id) is True

    assert published[-1] == []
    assert [
        record.split(" ", 1)[0] for record in loguru_records if "could not remove the queue journal" in record
    ] == ["WARNING"]


def test_republish_announces_the_queue_unchanged(tmp_path: Path) -> None:
    published: list[list[str]] = []
    tracker = _tracker(tmp_path)
    tracker.enqueue("held", "t0", is_turn_open=True)
    tracker.attach(publish=lambda snapshot: published.append([str(e["content"]) for e in snapshot]), wake=lambda: None)

    tracker.republish()

    assert published == [["held"]]


# The control lane


def test_control_commands_are_held_by_verb_with_the_latest_one_kept(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue_control_command("/model grok-4.5")
    tracker.enqueue_control_command("/effort high")
    tracker.enqueue_control_command("/model grok-4.6")

    assert tracker.next_control_command() == "/model grok-4.6"
    tracker.settle_control_command("/model grok-4.6", _SENT)
    assert tracker.next_control_command() == "/effort high"
    tracker.settle_control_command("/effort high", _SENT)
    assert tracker.next_control_command() is None


def test_a_held_model_switch_goes_out_ahead_of_an_effort_picked_before_it(tmp_path: Path) -> None:
    """An effort reaches the model it was picked for: Grok 4.5 has no xhigh."""
    tracker = _tracker(tmp_path)
    tracker.enqueue_control_command("/effort low")
    tracker.enqueue_control_command("/model grok-4.6")
    tracker.enqueue_control_command("/effort xhigh")

    assert tracker.next_control_command() == "/model grok-4.6"
    tracker.settle_control_command("/model grok-4.6", _SENT)
    assert tracker.next_control_command() == "/effort xhigh"


def test_control_commands_are_never_published_returned_or_merged_into_a_block(tmp_path: Path) -> None:
    published: list[list[str]] = []
    tracker = _tracker(tmp_path)
    tracker.attach(publish=lambda snapshot: published.append([str(e["content"]) for e in snapshot]), wake=lambda: None)
    tracker.enqueue_control_command("/model grok-4.6")
    tracker.enqueue("hello", "t0", is_turn_open=True)

    block, _claimed, _generation = tracker.begin_flush()
    returned, _taken = tracker.take_all()

    assert block == "hello"
    assert returned == "hello"
    assert all("/model grok-4.6" not in contents for contents in published)
    assert tracker.has_held_work() is True
    assert tracker.next_control_command() == "/model grok-4.6"


def test_a_control_command_outlives_a_relaunch_and_a_clear(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, token="session-1")
    tracker.enqueue_control_command("/effort low")

    tracker.set_session("session-2")
    tracker.clear()

    assert tracker.next_control_command() == "/effort low"


@pytest.mark.parametrize("result", [_BUSY, _BLOCKED], ids=["busy", "blocked"])
def test_a_control_command_mngr_typed_nothing_for_is_kept(tmp_path: Path, result: GrokSendResult) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue_control_command("/model grok-4.6")

    tracker.settle_control_command("/model grok-4.6", result)

    assert tracker.next_control_command() == "/model grok-4.6"


def test_a_failed_control_command_is_dropped(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue_control_command("/model grok-4.6")

    tracker.settle_control_command("/model grok-4.6", _FAILED)

    assert tracker.next_control_command() is None


def test_a_command_replaced_while_it_was_sent_is_kept_in_its_new_form(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path)
    tracker.enqueue_control_command("/model grok-4.5")
    sent = tracker.next_control_command()
    tracker.enqueue_control_command("/model grok-4.6")

    assert sent is not None
    tracker.settle_control_command(sent, _SENT)

    assert tracker.next_control_command() == "/model grok-4.6"


@pytest.mark.parametrize("command", ["hello", "/compact", "/model a\n/effort high", ""])
def test_the_control_lane_takes_only_model_and_effort_lines(tmp_path: Path, command: str) -> None:
    with pytest.raises(InvalidControlCommandError):
        _tracker(tmp_path).enqueue_control_command(command)


def test_enqueue_control_command_reaches_the_agents_shared_tracker_and_wakes_its_worker(tmp_path: Path) -> None:
    woken: list[bool] = []
    tracker = get_tracker(tmp_path)
    tracker.attach(publish=lambda _snapshot: None, wake=lambda: woken.append(True))

    enqueue_control_command(tmp_path, "/effort medium")

    assert get_tracker(tmp_path) is tracker
    assert tracker.next_control_command() == "/effort medium"
    assert woken == [True]


def test_a_running_stop_refuses_every_claim_and_wakes_the_worker_when_it_ends(tmp_path: Path) -> None:
    woken: list[bool] = []
    tracker = _tracker(tmp_path)
    tracker.attach(publish=lambda _snapshot: None, wake=lambda: woken.append(True))
    tracker.enqueue("held", "t0", is_turn_open=True)
    woken.clear()

    tracker.pause_flushes()
    assert tracker.begin_flush()[1] == ()
    tracker.resume_flushes()

    assert woken == [True]
    assert len(tracker.begin_flush()[1]) == 1
