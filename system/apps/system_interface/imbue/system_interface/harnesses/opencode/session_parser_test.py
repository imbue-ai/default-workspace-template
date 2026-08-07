"""Unit tests for the opencode row -> common-event mapping."""

from __future__ import annotations

from typing import Any

from imbue.system_interface.harnesses.opencode.db_reader import OpenCodeMessage
from imbue.system_interface.harnesses.opencode.db_reader import OpenCodePart
from imbue.system_interface.harnesses.opencode.session_parser import SOURCE
from imbue.system_interface.harnesses.opencode.session_parser import build_message_events

_TS = 1_786_000_000_000


def _message(
    role: str,
    *,
    provider_id: str | None = None,
    model_id: str | None = None,
    finish: str | None = None,
    completed: int | None = None,
    tokens: dict[str, Any] | None = None,
) -> OpenCodeMessage:
    return OpenCodeMessage(
        id="msg_1",
        session_id="ses_1",
        time_created=_TS,
        time_updated=_TS,
        role=role,
        provider_id=provider_id,
        model_id=model_id,
        finish=finish,
        completed=completed,
        tokens=tokens,
    )


def _part(
    kind: str,
    *,
    text: str = "",
    synthetic: bool = False,
    tool_name: str = "",
    call_id: str = "",
    state_status: str = "",
    state_input: dict[str, Any] | None = None,
    state_output: str = "",
    state_error: str = "",
) -> OpenCodePart:
    return OpenCodePart(
        id="prt_1",
        message_id="msg_1",
        session_id="ses_1",
        time_created=_TS,
        time_updated=_TS,
        kind=kind,
        text=text,
        synthetic=synthetic,
        tool_name=tool_name,
        call_id=call_id,
        state_status=state_status,
        state_input=state_input if state_input is not None else {},
        state_output=state_output,
        state_error=state_error,
    )


def test_user_message_event() -> None:
    events = build_message_events(_message("user"), [_part("text", text="hi")])
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "user_message"
    assert event["event_id"] == "msg_1-user"
    assert event["source"] == SOURCE
    assert event["content"] == "hi"


def test_user_message_with_only_synthetic_text_is_dropped() -> None:
    # opencode inserts a synthetic "The following tool was executed by the user" text part.
    part = _part("text", text="The following tool was executed by the user", synthetic=True)
    assert build_message_events(_message("user"), [part]) == []


def test_reasoning_parts_are_dropped_like_pi_drops_thinking() -> None:
    events = build_message_events(
        _message("assistant", provider_id="opencode", model_id="m", completed=1),
        [_part("reasoning", text="let me think"), _part("text", text="answer")],
    )
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "answer"


def test_assistant_model_and_usage() -> None:
    events = build_message_events(
        _message(
            "assistant",
            provider_id="opencode",
            model_id="deepseek",
            finish="stop",
            completed=1,
            tokens={"input": 10, "output": 3, "cache": {"read": 2, "write": 1}},
        ),
        [_part("text", text="done")],
    )
    event = events[0]
    assert event["model"] == "opencode/deepseek"
    assert event["stop_reason"] == "stop"
    assert event["usage"] == {"input_tokens": 10, "output_tokens": 3, "cache_read_tokens": 2, "cache_write_tokens": 1}


def test_running_tool_emits_no_result_then_settled_tool_does() -> None:
    running = _part("tool", tool_name="bash", call_id="c1", state_status="running", state_input={"command": "ls"})
    events = build_message_events(_message("assistant", provider_id="p", model_id="m"), [running])
    # two-phase: the assistant_message carries the tool_call, but there is NO tool_result yet.
    assert [e["type"] for e in events] == ["assistant_message"]
    assert events[0]["tool_calls"][0]["tool_name"] == "bash"
    assert events[0]["tool_calls"][0]["caption_label"] == "Running ls"

    completed = _part(
        "tool",
        tool_name="bash",
        call_id="c1",
        state_status="completed",
        state_input={"command": "ls"},
        state_output="a\nb\n",
    )
    events = build_message_events(_message("assistant", provider_id="p", model_id="m", completed=1), [completed])
    assert [e["type"] for e in events] == ["assistant_message", "tool_result"]
    result = events[1]
    assert result["event_id"] == "prt_1-tool_result"
    assert result["tool_call_id"] == "c1"
    assert result["output"] == "a\nb\n"
    assert result["is_error"] is False


def test_errored_tool_result_uses_error_text() -> None:
    part = _part("tool", tool_name="bash", call_id="c1", state_status="error", state_error="boom")
    events = build_message_events(_message("assistant", provider_id="p", model_id="m", completed=1), [part])
    result = events[1]
    assert result["is_error"] is True
    assert result["output"] == "boom"


def test_non_user_non_assistant_role_yields_nothing() -> None:
    assert build_message_events(_message("system"), [_part("text", text="x")]) == []
