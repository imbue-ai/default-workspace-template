"""Tests for :mod:`pi_coding.session_parser` -- mapping pi's native session records to
the web-UI event schema. Focused on the load-bearing invariants: stable event ids from
pi's own record id, thinking blocks dropped, the tool-call / result correlation, and the
tk input-truncation exemption.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.pi_coding.session_parser import parse_record

_TESTDATA = Path(__file__).parent / "testdata"


def _message_record(record_id: str, message: dict[str, Any], timestamp: str = "2026-08-07T11:50:00.000Z") -> dict:
    return {"type": "message", "id": record_id, "parentId": None, "timestamp": timestamp, "message": message}


def test_user_record_becomes_user_message() -> None:
    events = parse_record(_message_record("abc123", {"role": "user", "content": [{"type": "text", "text": "hey"}]}))
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user_message"
    assert event["event_id"] == "pi-abc123"
    assert event["content"] == "hey"


def test_assistant_record_drops_thinking_and_keeps_text_and_tool_calls() -> None:
    message = {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [
            {"type": "thinking", "thinking": "secret reasoning", "thinkingSignature": "sig"},
            {"type": "text", "text": "Doing it."},
            {"type": "toolCall", "id": "toolu_1", "name": "bash", "arguments": {"command": "ls"}},
        ],
    }
    events = parse_record(_message_record("asst1", message))
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "assistant_message"
    assert event["event_id"] == "pi-asst1"
    # Thinking is never rendered: its text must not leak into the assistant text.
    assert event["text"] == "Doing it."
    assert "secret reasoning" not in event["text"]
    assert len(event["tool_calls"]) == 1
    call = event["tool_calls"][0]
    assert call["tool_call_id"] == "toolu_1"
    assert call["tool_name"] == "bash"
    assert call["header_label"] == "Tool: Bash"
    assert call["caption_label"] == "Running ls"


def test_parallel_tool_calls_all_captured() -> None:
    message = {
        "role": "assistant",
        "content": [
            {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "echo a"}},
            {"type": "toolCall", "id": "t2", "name": "read", "arguments": {"path": "/x/y.py"}},
        ],
    }
    call_ids = [c["tool_call_id"] for c in parse_record(_message_record("m", message))[0]["tool_calls"]]
    assert call_ids == ["t1", "t2"]


def test_tool_result_correlates_by_toolcall_id() -> None:
    message = {
        "role": "toolResult",
        "toolCallId": "toolu_1",
        "toolName": "bash",
        "content": [{"type": "text", "text": "output here"}],
        "isError": False,
    }
    event = parse_record(_message_record("res1", message))[0]
    assert event["type"] == "tool_result"
    assert event["tool_call_id"] == "toolu_1"
    assert event["tool_name"] == "bash"
    assert event["output"] == "output here"
    assert event["is_error"] is False


def test_tool_result_error_flag() -> None:
    message = {"role": "toolResult", "toolCallId": "t", "toolName": "bash", "content": "boom", "isError": True}
    assert parse_record(_message_record("r", message))[0]["is_error"] is True


def test_meta_records_are_skipped() -> None:
    assert parse_record({"type": "session", "id": "s", "timestamp": "t", "cwd": "/x"}) == []
    assert parse_record({"type": "model_change", "id": "m", "provider": "anthropic", "modelId": "x"}) == []
    assert parse_record({"type": "thinking_level_change", "id": "t", "thinkingLevel": "medium"}) == []


def test_record_without_id_is_skipped() -> None:
    assert parse_record({"type": "message", "timestamp": "t", "message": {"role": "user", "content": "x"}}) == []


def test_tk_lifecycle_input_is_not_truncated() -> None:
    long_summary = "x" * 400
    command = f'tk close wor-step-abcd "{long_summary}"'
    message = {
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "t", "name": "bash", "arguments": {"command": command}}],
    }
    preview = parse_record(_message_record("m", message))[0]["tool_calls"][0]["input_preview"]
    # The whole summary survives (no "..." cut mid-summary).
    assert long_summary in preview


def test_plain_bash_input_is_truncated() -> None:
    command = "echo " + "a" * 400
    message = {
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "t", "name": "bash", "arguments": {"command": command}}],
    }
    preview = parse_record(_message_record("m", message))[0]["tool_calls"][0]["input_preview"]
    assert preview.endswith("...")
    assert len(preview) == MAX_TOOL_INPUT_PREVIEW_LENGTH + len("...")


def test_parses_captured_live_session_with_tools() -> None:
    records = [json.loads(line) for line in (_TESTDATA / "session_with_tools.jsonl").read_text().splitlines() if line]
    events = [event for record in records for event in parse_record(record)]
    kinds = {event["type"] for event in events}
    assert kinds == {"user_message", "assistant_message", "tool_result"}
    # Every tool_result's call id is produced by some assistant tool_call (correlation holds).
    call_ids = {c["tool_call_id"] for e in events if e["type"] == "assistant_message" for c in e["tool_calls"]}
    result_ids = {e["tool_call_id"] for e in events if e["type"] == "tool_result"}
    assert result_ids <= call_ids


_ANTHROPIC_401 = '401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
_ANTHROPIC_529 = '529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'


def test_failed_turn_shows_its_error_instead_of_a_blank_bubble() -> None:
    """pi puts nothing in `content` when a turn fails and everything in `errorMessage`.

    Reading text from `content` alone emitted `text: ""`, so the transcript painted an empty
    bubble -- an agent stuck on a rejected key looked like one that had simply stopped.
    """
    message = {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": _ANTHROPIC_401}
    event = parse_record(_message_record("m", message))[0]
    assert event["text"] == _ANTHROPIC_401
    assert event["is_auth_error"] is True
    # Auth and API are exclusive: two subtexts would offer two contradictory next steps.
    assert event["is_api_error"] is False


def test_failed_turn_classifies_a_provider_fault() -> None:
    message = {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": _ANTHROPIC_529}
    event = parse_record(_message_record("m", message))[0]
    assert event["is_api_error"] is True
    assert event["api_error_kind"] == "overloaded"
    assert event["is_provider_fault"] is True
    assert event["is_auth_error"] is False


def test_a_reply_that_merely_quotes_an_error_is_not_one() -> None:
    """The gate is `stopReason`, not the presence of `errorMessage`.

    Asking the agent about a 401 gets a reply whose text contains one; styling that as a
    failure would put a sign-in button under an ordinary answer.
    """
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": f"You are seeing {_ANTHROPIC_401} because the key expired."}],
        "stopReason": "end_turn",
        "errorMessage": _ANTHROPIC_401,
    }
    event = parse_record(_message_record("m", message))[0]
    assert event["is_auth_error"] is False
    assert event["is_api_error"] is False
    assert "You are seeing" in event["text"]
