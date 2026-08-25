"""Unit tests for antigravity's activity-state derivation."""

from __future__ import annotations

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.antigravity.activity import _tail_is_final_answer
from imbue.system_interface.harnesses.antigravity.activity_state import derive


def _derive(
    *,
    is_agent_alive: bool = True,
    # Absent by default: agy removes the marker for the whole of every tool call, so
    # "no marker" is its ordinary mid-turn state and the transcript rungs are what matter.
    # Tests that care about the marker rung pass it explicitly.
    is_active_marker_present: bool = False,
    has_pending_tool_use: bool = False,
    tail_event_type: str | None = "assistant_message",
    tail_is_final_answer: bool = True,
) -> ActivityState:
    # Accepted and deliberately dropped: derive no longer reads the marker at all, and the
    # tests below assert exactly that it cannot change the answer.
    del is_active_marker_present
    return derive(
        is_agent_alive=is_agent_alive,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
        tail_is_final_answer=tail_is_final_answer,
    )


def test_a_dead_process_is_idle() -> None:
    assert _derive(is_agent_alive=False, has_pending_tool_use=True) == ActivityState.IDLE


def test_pending_tool_is_tool_running() -> None:
    assert _derive(has_pending_tool_use=True) == ActivityState.TOOL_RUNNING


def test_user_and_tool_result_tails_are_thinking() -> None:
    assert _derive(tail_event_type="user_message", tail_is_final_answer=False) == ActivityState.THINKING
    assert _derive(tail_event_type="tool_result", tail_is_final_answer=False) == ActivityState.THINKING


def test_final_answer_tail_is_idle() -> None:
    assert _derive(tail_event_type="assistant_message", tail_is_final_answer=True) == ActivityState.IDLE


def test_empty_planner_tail_mid_turn_stays_thinking() -> None:
    # agy's between-tool "thinking" step: an empty assistant tail while still running must
    # NOT flicker IDLE.
    assert _derive(tail_event_type="assistant_message", tail_is_final_answer=False) == ActivityState.THINKING


def test_tail_is_final_answer_detects_substantive_answer() -> None:
    events = [
        {"type": "user_message", "content": "hi"},
        {"type": "assistant_message", "text": "here is my answer", "tool_calls": []},
    ]
    assert _tail_is_final_answer(events) is True


def test_tail_is_final_answer_false_for_empty_planner() -> None:
    events = [
        {"type": "user_message", "content": "hi"},
        {"type": "assistant_message", "text": "", "tool_calls": [], "thinking": "hmm"},
    ]
    assert _tail_is_final_answer(events) is False


def test_tail_is_final_answer_false_after_tool_result() -> None:
    events = [
        {"type": "assistant_message", "text": "", "tool_calls": [{"tool_call_id": "c"}]},
        {"type": "tool_result", "tool_call_id": "c", "output": "done"},
    ]
    assert _tail_is_final_answer(events) is False


# --- the marker is a supporting rung, never an override --------------------------------
#
# agy's statusLine reports only idle/thinking, so the marker is absent for the WHOLE of every
# tool call. These are the cases where reading it as liveness produced a mid-chain IDLE --
# which armed the queue flush and swallowed the block into the running turn.


def test_a_running_tool_survives_the_marker_dropping() -> None:
    assert _derive(is_active_marker_present=False, has_pending_tool_use=True) == ActivityState.TOOL_RUNNING


def test_a_tool_result_tail_survives_the_marker_dropping() -> None:
    assert _derive(is_active_marker_present=False, tail_event_type="tool_result") == ActivityState.THINKING


def test_an_empty_planner_tail_survives_the_marker_dropping() -> None:
    assert (
        _derive(is_active_marker_present=False, tail_event_type="assistant_message", tail_is_final_answer=False)
        == ActivityState.THINKING
    )


def test_the_marker_can_never_hold_the_dot_on() -> None:
    """The regression that latched the dot: a rung reading "marker present -> THINKING" has no
    edge that leaves it, because no watcher notifies on a marker-only change. Every harness
    keeps the marker gating toward IDLE only; the transcript decides the working side."""
    assert (
        _derive(is_active_marker_present=True, tail_event_type="assistant_message", tail_is_final_answer=True)
        == ActivityState.IDLE
    )


def test_a_finished_turn_with_no_marker_is_idle() -> None:
    assert (
        _derive(is_active_marker_present=False, tail_event_type="assistant_message", tail_is_final_answer=True)
        == ActivityState.IDLE
    )
