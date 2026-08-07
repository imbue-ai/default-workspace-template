"""Unit tests for antigravity's activity-state derivation."""

from __future__ import annotations

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.antigravity.activity import _tail_is_final_answer
from imbue.system_interface.harnesses.antigravity.activity_state import derive


def _derive(
    *,
    is_agent_running: bool = True,
    has_pending_tool_use: bool = False,
    tail_event_type: str | None = "assistant_message",
    tail_is_final_answer: bool = True,
) -> ActivityState:
    return derive(
        is_agent_running=is_agent_running,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
        tail_is_final_answer=tail_is_final_answer,
    )


def test_not_running_is_idle() -> None:
    assert _derive(is_agent_running=False, has_pending_tool_use=True) == ActivityState.IDLE


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
