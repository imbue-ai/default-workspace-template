"""Tests for grok's send: it holds the message for the flush worker, and answers with the worker's first verdict."""

import os
import threading
from pathlib import Path

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import GrokSendResult
from imbue.chat.harnesses.grok.queue_tracker import GrokSendVerdict
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.queue_tracker import REFUSED_VERDICTS
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.chat.harnesses.grok.session import GrokHarnessSession
from imbue.chat.harnesses.grok.testing import write_live_grok_pid
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.interrupt import InterruptToComposer
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME
from imbue.mngr_grok.api import read_grok_pid

_SESSION = "01a156d6-f927-7091-91a4-f6692a5800da"


def _unused_interrupter(agent_info: AgentInfo) -> InterruptToComposer:
    raise AssertionError("a send never builds an interrupter")


def _session(
    agent_state_dir: Path, typed: list[str], *, is_grok_running: bool = True, is_starting_up: bool = False
) -> GrokHarnessSession:
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    if is_grok_running:
        write_live_grok_pid(agent_state_dir)
    session = GrokHarnessSession.build(
        SessionDeps(
            harness=HarnessType.GROK,
            state_dir=agent_state_dir,
            model_state_path=agent_state_dir / "model_state.json",
            send_to_harness=lambda text: (typed.append(text), True)[1],
            notify_agents_changed=lambda: None,
            is_harness_starting_up=lambda: is_starting_up,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: (),
            build_interrupter=_unused_interrupter,
            build_shoulder_tap=lambda agent_info: None,
        )
    )
    session.send_result_wait_seconds = 5.0
    return session


def _attach_scripted_worker(tracker: GrokQueueTracker, result: GrokSendResult) -> list[str]:
    """Settle every wake's claim with ``result`` on a thread of its own, as the flush worker would; returns the blocks."""
    claimed_blocks: list[str] = []

    def settle() -> None:
        block, claimed, generation = tracker.begin_flush()
        if not claimed:
            return
        claimed_blocks.append(block)
        if result.verdict in REFUSED_VERDICTS:
            tracker.refuse_flush(claimed, generation, result)
        else:
            delivered = claimed if result.verdict is GrokSendVerdict.SENT else ()
            tracker.finish_flush(claimed, generation, delivered=delivered, result=result)

    tracker.attach(publish=lambda _snapshot: None, wake=lambda: threading.Thread(target=settle).start())
    return claimed_blocks


def _contents(tracker: GrokQueueTracker) -> list[str]:
    return [str(entry["content"]) for entry in tracker.snapshot()]


def test_the_pid_record_is_the_one_mngr_grok_reads(tmp_path: Path) -> None:
    write_live_grok_pid(tmp_path)

    assert read_grok_pid(tmp_path) == os.getpid()


@pytest.mark.parametrize(
    ("is_grok_running", "is_starting_up"), [(False, False), (True, True)], ids=["not_running", "starting_up"]
)
def test_a_grok_that_is_not_up_is_not_ready_and_holds_nothing(
    tmp_path: Path, is_grok_running: bool, is_starting_up: bool
) -> None:
    """The route starts grok and retries, and a start that fails returns the text to the composer."""
    typed: list[str] = []
    session = _session(tmp_path / "agent-down", typed, is_grok_running=is_grok_running, is_starting_up=is_starting_up)

    assert session.send("hello", "m1") is SendOutcome.NOT_READY
    assert get_tracker(tmp_path / "agent-down").snapshot() == []
    assert typed == []


def test_a_message_sent_behind_a_turn_is_queued_at_once_and_never_typed(tmp_path: Path) -> None:
    typed: list[str] = []
    agent_state_dir = tmp_path / "agent-busy"
    session = _session(agent_state_dir, typed)
    write_root_turn(agent_state_dir, _SESSION, "prompt-1")
    tracker = get_tracker(agent_state_dir)
    tracker.attach(publish=lambda _snapshot: None, wake=lambda: None)

    assert session.send("also this", "m1") is SendOutcome.OK
    assert session.send("and this", "m2") is SendOutcome.OK

    assert typed == []
    assert _contents(tracker) == ["also this", "and this"]
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [False, False]
    assert session.is_tap_available(has_queued=True) is True


def test_the_tap_is_not_offered_once_every_held_message_spent_its_attempts(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent-exhausted"
    session = _session(agent_state_dir, [])
    write_root_turn(agent_state_dir, _SESSION, "prompt-1")
    tracker = get_tracker(agent_state_dir)
    tracker.attach(publish=lambda _snapshot: None, wake=lambda: None)
    session.send("never committed", "m1")
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        _block, claimed, generation = tracker.begin_flush()
        tracker.finish_flush(
            claimed,
            generation,
            delivered=(),
            result=GrokSendResult(verdict=GrokSendVerdict.UNCONFIRMED, reason="", failure_kind=""),
        )

    assert session.is_tap_available(has_queued=True) is False


def test_a_message_sent_to_an_idle_grok_answers_once_the_worker_sent_it(tmp_path: Path) -> None:
    typed: list[str] = []
    agent_state_dir = tmp_path / "agent-idle"
    session = _session(agent_state_dir, typed)
    tracker = get_tracker(agent_state_dir)
    claimed_blocks = _attach_scripted_worker(
        tracker, GrokSendResult(verdict=GrokSendVerdict.SENT, reason="", failure_kind="")
    )

    assert session.send("hello", "m1") is SendOutcome.OK

    assert claimed_blocks == ["hello"]
    assert tracker.snapshot() == []
    assert typed == [], "the session never types; the worker does"


@pytest.mark.parametrize(
    "result",
    [
        GrokSendResult(
            verdict=GrokSendVerdict.BLOCKED,
            reason="grok reports that the account has no subscription that includes Grok Build",
            failure_kind="input_blocked",
        ),
        GrokSendResult(verdict=GrokSendVerdict.FAILED, reason="the pane is gone", failure_kind="agent_unreachable"),
    ],
    ids=["gate_screen", "unreachable"],
)
def test_a_send_mngr_refused_fails_with_its_words_and_leaves_the_queue(tmp_path: Path, result: GrokSendResult) -> None:
    """A message grok never took fails its POST, which puts the text back in the composer (contract Send)."""
    agent_state_dir = tmp_path / "agent-gated"
    session = _session(agent_state_dir, [])
    tracker = get_tracker(agent_state_dir)
    _attach_scripted_worker(tracker, result)

    with pytest.raises(SendFailedError) as raised:
        session.send("hello", "m1")

    assert (raised.value.detail, raised.value.kind) == (result.reason, result.failure_kind)
    assert tracker.snapshot() == []


def test_a_send_behind_a_model_switch_mngr_refused_for_a_gate_fails_with_its_words(tmp_path: Path) -> None:
    """The worker sends the held switch first; the gate that refused it refuses the message too (contract Send)."""
    agent_state_dir = tmp_path / "agent-gated-switch"
    session = _session(agent_state_dir, [])
    tracker = get_tracker(agent_state_dir)
    gate = GrokSendResult(
        verdict=GrokSendVerdict.BLOCKED, reason="grok shows its consent screen", failure_kind="input_blocked"
    )
    tracker.attach(
        publish=lambda _snapshot: None,
        wake=lambda: threading.Thread(target=tracker.refuse_pending, args=(gate,)).start(),
    )

    with pytest.raises(SendFailedError) as raised:
        session.send("hello", "m1")

    assert (raised.value.detail, raised.value.kind) == (gate.reason, gate.failure_kind)
    assert tracker.snapshot() == []


def test_a_send_grok_turned_out_busy_for_answers_ok_and_stays_queued(tmp_path: Path) -> None:
    """A turn that began between the gate and mngr's send is a "not yet", not a failure."""
    agent_state_dir = tmp_path / "agent-raced"
    session = _session(agent_state_dir, [])
    tracker = get_tracker(agent_state_dir)
    busy = GrokSendResult(verdict=GrokSendVerdict.BUSY, reason="grok is running a turn", failure_kind="agent_busy")
    _attach_scripted_worker(tracker, busy)

    assert session.send("hello", "m1") is SendOutcome.OK

    assert _contents(tracker) == ["hello"]
    assert [entry["is_sending"] for entry in tracker.snapshot()] == [False]


def test_a_send_no_worker_settles_answers_ok_with_the_message_still_sending(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent-slow"
    session = _session(agent_state_dir, [])
    session.send_result_wait_seconds = 0.05

    assert session.send("hello", "m1") is SendOutcome.OK

    assert [entry["is_sending"] for entry in get_tracker(agent_state_dir).snapshot()] == [True]
    assert session.is_sending() is True
    assert session.is_tap_available(has_queued=True) is False


def test_ensure_live_hands_the_worker_this_sessions_send_and_close_takes_it_back(tmp_path: Path) -> None:
    typed: list[str] = []
    agent_state_dir = tmp_path / "agent-bound"
    session = _session(agent_state_dir, typed)
    tracker = get_tracker(agent_state_dir)
    assert tracker.get_session_send() is None

    session.ensure_live()
    session_send = tracker.get_session_send()
    assert session_send is not None
    assert session_send("typed by the worker") is True
    assert typed == ["typed by the worker"]

    session.close()
    assert tracker.get_session_send() is None


def test_held_messages_are_not_also_returned_as_in_flight(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent-double"
    session = _session(agent_state_dir, [])
    write_root_turn(agent_state_dir, _SESSION, "prompt-1")
    session.send("held", "m1")

    assert session.in_flight_block() == ""
    assert get_tracker(agent_state_dir).concatenated_block() == "held"


def test_a_send_after_grok_relaunched_adopts_the_new_session_before_holding_the_message(tmp_path: Path) -> None:
    """Measured live: the worker adopted the relaunch first and dropped the message sent to revive grok."""
    agent_state_dir = tmp_path / "agent-relaunched"
    session = _session(agent_state_dir, [])
    tracker = get_tracker(agent_state_dir)
    (agent_state_dir / PROCESS_STARTED_MARKER_FILENAME).write_text("")
    write_root_turn(agent_state_dir, _SESSION, "prompt-1")

    assert session.send("sent to the new grok", "m1") is SendOutcome.OK

    assert tracker.set_session(session_token(agent_state_dir)) is False
    assert _contents(tracker) == ["sent to the new grok"]
