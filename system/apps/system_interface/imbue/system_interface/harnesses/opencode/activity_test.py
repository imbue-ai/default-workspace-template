"""Unit tests for opencode's marker-driven activity derivation."""

from __future__ import annotations

from typing import Any

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.opencode.activity import OpenCodeActivityTracker


def _assistant_with_tool(call_id: str) -> dict[str, Any]:
    return {"type": "assistant_message", "timestamp": "2026-01-01T00:00:00.000Z", "tool_calls": [{"tool_call_id": call_id}]}


def _tool_result(call_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "timestamp": "2026-01-01T00:00:01.000Z", "tool_call_id": call_id}


def test_running_with_no_tool_is_thinking_even_when_tail_is_assistant() -> None:
    # opencode pre-creates the (empty) assistant row at turn start, so the tail is
    # assistant_message immediately -- the marker-driven latch must still read THINKING.
    tracker = OpenCodeActivityTracker.build()
    tracker.observe([{"type": "user_message", "timestamp": "2026-01-01T00:00:00.000Z"}, _empty_assistant()])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING


def test_running_with_unmatched_tool_is_tool_running() -> None:
    tracker = OpenCodeActivityTracker.build()
    tracker.observe([_assistant_with_tool("c1")])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.TOOL_RUNNING


def test_matched_tool_returns_to_thinking_while_running() -> None:
    tracker = OpenCodeActivityTracker.build()
    tracker.observe([_assistant_with_tool("c1"), _tool_result("c1")])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING


def test_not_running_is_idle() -> None:
    # An unmatched tool, but the agent is not running -> IDLE (the marker gate wins).
    tracker = OpenCodeActivityTracker.build()
    tracker.observe([_assistant_with_tool("c1")])
    assert tracker.derive(is_agent_running=False, process_started_at=None) == ActivityState.IDLE


def test_empty_transcript_running_is_thinking() -> None:
    tracker = OpenCodeActivityTracker.build()
    tracker.observe([])
    assert tracker.derive(is_agent_running=True, process_started_at=None) == ActivityState.THINKING


def test_observe_reports_change_only_when_signals_move() -> None:
    tracker = OpenCodeActivityTracker.build()
    assert tracker.observe([_assistant_with_tool("c1")]) is True
    assert tracker.observe([_assistant_with_tool("c1")]) is False


def _empty_assistant() -> dict[str, Any]:
    return {"type": "assistant_message", "timestamp": "2026-01-01T00:00:00.500Z", "tool_calls": []}
