"""Unit tests for the antigravity session parser (DecodedStep -> UI events)."""

from __future__ import annotations

import json

from imbue.system_interface.harnesses.antigravity.agy_transcript import DecodedStep
from imbue.system_interface.harnesses.antigravity.agy_transcript import DecodedToolCall
from imbue.system_interface.harnesses.antigravity.agy_transcript import decode_step
from imbue.system_interface.harnesses.antigravity.session_parser import parse_step
from imbue.system_interface.harnesses.antigravity.testing import load_captured_step

_BASE_STEP = DecodedStep(
    conv_id="c1",
    idx=0,
    step_type_name="USER_INPUT",
    status_name="DONE",
    source_name="USER_EXPLICIT",
    created_at="2026-08-07T00:00:00Z",
    is_terminal=True,
)


def _step(**kwargs: object) -> DecodedStep:
    return _BASE_STEP.model_copy_update(*kwargs.items())


def test_user_input_strips_the_request_wrapper() -> None:
    step = _step(
        user_text="<USER_REQUEST>\nhey there\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>",
    )
    events = parse_step(step)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "hey there"
    assert events[0]["event_id"] == "c1:0:user"


def test_user_input_without_wrapper_uses_raw_text() -> None:
    events = parse_step(_step(user_text="plain prompt"))
    assert events[0]["content"] == "plain prompt"


def test_implicit_user_input_is_skipped() -> None:
    assert parse_step(_step(source_name="USER_IMPLICIT", user_text="injected context")) == []


def test_planner_response_becomes_assistant_message_with_thinking() -> None:
    step = _step(
        idx=5,
        step_type_name="PLANNER_RESPONSE",
        source_name="MODEL",
        assistant_text="Here is the answer.",
        thinking="reasoning",
    )
    events = parse_step(step)
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Here is the answer."
    assert events[0]["has_thinking"] is True
    assert "thinking" not in events[0]
    assert events[0]["tool_calls"] == []
    assert events[0]["event_id"] == "c1:5:assistant"


def test_empty_planner_response_is_skipped() -> None:
    assert parse_step(_step(step_type_name="PLANNER_RESPONSE", source_name="MODEL", assistant_text="")) == []


def _tool_step(*, terminal: bool, status: str, result: str | None) -> DecodedStep:
    return _step(
        idx=16,
        step_type_name="RUN_COMMAND",
        status_name=status,
        source_name="MODEL",
        is_terminal=terminal,
        tool_call=DecodedToolCall(
            call_id="X1",
            name="run_command",
            args='{"CommandLine":"python3 showcase.py"}',
            tool_summary="Script execution",
            tool_action="Running python3 showcase.py",
        ),
        tool_result_text=result,
        is_error_result=(status == "ERROR"),
    )


def test_terminal_tool_step_emits_matched_call_and_result() -> None:
    events = parse_step(_tool_step(terminal=True, status="DONE", result="hello output"))
    assert [e["type"] for e in events] == ["assistant_message", "tool_result"]
    call = events[0]["tool_calls"][0]
    assert call["tool_name"] == "run_command"
    assert call["header_label"] == "Tool: Bash"
    assert call["caption_label"] == "Running python3 showcase.py"
    # call and result share the tool_call_id so the frontend pairs them
    assert events[1]["tool_call_id"] == call["tool_call_id"] == "c1:16:toolcall"
    assert events[1]["output_chars"] == len("hello output")
    assert "output" not in events[1]
    assert events[1]["is_error"] is False


def test_running_tool_step_emits_only_the_call() -> None:
    events = parse_step(_tool_step(terminal=False, status="RUNNING", result=None))
    assert [e["type"] for e in events] == ["assistant_message"]
    assert events[0]["tool_calls"][0]["caption_label"] == "Running python3 showcase.py"


def test_error_tool_result_is_flagged() -> None:
    events = parse_step(_tool_step(terminal=True, status="ERROR", result="boom"))
    assert events[1]["is_error"] is True


def test_error_message_step_becomes_flagged_assistant_message() -> None:
    step = _step(idx=9, step_type_name="ERROR_MESSAGE", status_name="ERROR", error_text="quota exceeded")
    events = parse_step(step)
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "quota exceeded"
    assert events[0]["is_api_error"] is True


def test_conversation_history_step_is_skipped() -> None:
    assert parse_step(_step(step_type_name="CONVERSATION_HISTORY", source_name="SYSTEM")) == []


# --- shared tool_output behaviour (parity with claude / codex / pi) ------------------------


def _named_tool_step(*, name: str, args: str, result: str | None = None, **kwargs: object) -> DecodedStep:
    return _step(
        idx=2,
        step_type_name="RUN_COMMAND",
        tool_call=DecodedToolCall(call_id="t1", name=name, args=args, tool_summary="", tool_action=""),
        tool_result_text=result,
        **kwargs,
    )


def _make_permission_request_output(rationale: str) -> str:
    """The stdout of a latchkey permission-request POST: curl's progress meter, then the
    created request pretty-printed the way the gateway writes it."""
    body = json.dumps(
        {
            "request_id": "885711ec07bf47239d71294e1534330b",
            "agent_id": "agent-28dc23edadd34caeaba58441ac8e7218",
            "rationale": rationale,
            "request_type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
        indent=2,
    )
    meter = "  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current\n" * 3
    return meter + body + "\n"


def test_pure_tk_lifecycle_call_is_hidden() -> None:
    """A run_command that is nothing but a tk lifecycle invocation is a structural marker,
    not work, so it renders hidden -- the same decision claude/codex/pi make."""
    step = _named_tool_step(name="run_command", args=json.dumps({"CommandLine": 'tk create --step "do a thing"'}))
    call = parse_step(step)[0]["tool_calls"][0]
    assert call["display"] == "hidden"


def test_an_ordinary_command_gets_no_display_override() -> None:
    """The hide rule is strict: a command that merely MENTIONS tk still renders normally."""
    step = _named_tool_step(name="run_command", args=json.dumps({"CommandLine": "grep -r tk ."}))
    assert "display" not in parse_step(step)[0]["tool_calls"][0]


def test_permission_request_call_renders_as_the_card() -> None:
    """Recognised from the tool INPUT, so the card appears while the request is still
    pending and has produced no result yet."""
    args = json.dumps({"CommandLine": "curl -X POST https://latchkey-self.invalid/permission-requests -d @-"})
    call = parse_step(_named_tool_step(name="run_command", args=args))[0]["tool_calls"][0]
    assert call["display"] == "permission_request"


def test_permission_request_rides_the_event_however_long_the_output() -> None:
    """The permission-request object is lifted whole off the full output; the output itself
    stays off the event (the payload-free wire contract)."""
    output = _make_permission_request_output("I need to read the eng-releases channel. " * 60)
    events = parse_step(_named_tool_step(name="run_command", args="{}", result=output))
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["permission_request"]["request_id"] == "885711ec07bf47239d71294e1534330b"
    assert "output" not in result
    assert result["output_chars"] == len(output)


def test_tk_step_decoration_is_stamped_resident() -> None:
    """tk decoration lines are what the step timeline reads; they are stamped resident so
    the view never needs the raw output."""
    output = ("x" * 5000) + "\nUpdated abc-step-1a2b -> closed"
    events = parse_step(_named_tool_step(name="run_command", args="{}", result=output))
    result = next(event for event in events if event["type"] == "tool_result")
    assert "Updated abc-step-1a2b -> closed" in result["tk_stamp"]


def test_a_terminal_tool_step_always_emits_a_result_even_with_no_output() -> None:
    """The invariant that keeps the activity indicator honest.

    ``session_parser`` emits a ``tool_result`` only when ``tool_result_text is not None``, so a
    decoder that returned None for an unrecognised body would leave that call permanently
    unmatched -- the frontend would show a tool that never finishes, for the life of the agent.
    Only ``run_command`` bodies are measured, so the unrecognised path must stay harmless.
    Asserted at the EVENT level, because the decoder returning "" is not by itself proof the
    result reaches the chat.
    """
    events = parse_step(_tool_step(terminal=True, status="DONE", result=""))
    assert any(event["type"] == "tool_result" for event in events), (
        "a settled tool call with no recognisable output must still be matched by a result"
    )


def test_a_real_conversation_yields_the_tk_lines_the_progress_view_reads() -> None:
    """End-to-end guard for the timeline: the decoration lines must survive decoding, hiding
    and the resident tk stamp all the way into the emitted event."""
    step_type, status, payload = load_captured_step("tk_create")
    events = parse_step(decode_step("conv", 3, step_type, status, payload))
    result = next(event for event in events if event["type"] == "tool_result")
    assert "Created a7-step-7dlr: Run sequential test commands" in result["tk_stamp"]
    call = next(event for event in events if event["type"] == "assistant_message")
    assert call["tool_calls"][0]["display"] == "hidden", "a pure tk call is a structural marker"
