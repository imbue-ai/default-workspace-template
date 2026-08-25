"""Unit tests for agy's turn-open reading (the marker cannot answer this alone)."""

from pathlib import Path

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import is_turn_open_by_tail


def _call(tool_call_id: str) -> dict:
    return {"type": "tool_call", "tool_call_id": tool_call_id}


def _result(tool_call_id: str) -> dict:
    return {"type": "tool_result", "tool_call_id": tool_call_id}


def test_an_unmatched_tool_call_is_an_open_turn() -> None:
    assert is_turn_open_by_tail([{"type": "user_message"}, _call("t1")]) is True


def test_a_matched_tool_call_followed_by_an_answer_is_closed() -> None:
    events = [_call("t1"), _result("t1"), {"type": "assistant_message", "text": "done"}]
    assert is_turn_open_by_tail(events) is False


def test_a_tool_result_tail_is_open() -> None:
    """agy has run the tool and is about to speak again -- still mid-turn."""
    assert is_turn_open_by_tail([_call("t1"), _result("t1")]) is True


def test_an_empty_planner_tail_is_open() -> None:
    """agy's between-tools step. Reading it as a finished answer is what flickered the dot."""
    assert is_turn_open_by_tail([{"type": "user_message"}, {"type": "assistant_message", "text": ""}]) is True


def test_a_real_answer_tail_is_closed() -> None:
    assert is_turn_open_by_tail([{"type": "user_message"}, {"type": "assistant_message", "text": "hi"}]) is False


def test_an_empty_transcript_is_closed() -> None:
    assert is_turn_open_by_tail([]) is False


def test_the_marker_opens_a_turn_the_transcript_has_not_caught_up_to(tmp_path: Path) -> None:
    drop_turn_state("agent-marker")
    state = get_turn_state("agent-marker")
    state.publish(is_open_by_tail=False)
    assert state.is_turn_open(tmp_path) is False
    (tmp_path / ACTIVE_MARKER_FILENAME).write_text("")
    assert state.is_turn_open(tmp_path) is True


def test_the_transcript_keeps_the_turn_open_without_the_marker(tmp_path: Path) -> None:
    """The regression: mid-tool-chain agy removes the marker, and the send path used to read
    that as idle and type straight into the running turn."""
    drop_turn_state("agent-chain")
    state = get_turn_state("agent-chain")
    state.publish(is_open_by_tail=True)
    assert not (tmp_path / ACTIVE_MARKER_FILENAME).exists()
    assert state.is_turn_open(tmp_path) is True


def test_the_same_agent_shares_one_reading() -> None:
    drop_turn_state("agent-shared")
    assert get_turn_state("agent-shared") is get_turn_state("agent-shared")
