"""The cross-harness event contract, asserted per harness.

Every parser fills the same three core event types with the same fields (see
``harnesses/events.py``); ``Response.ts`` types them as required. Divergence used to slip
through review because nothing asserted the emitted key sets -- codex and pi shipped
without the API-error fields the contract declares, and their tk decoration silently died
in truncation. This is the ratchet: one minimal transcript per harness, every emitted
event's keys checked against the shared requirement.
"""

import json
from typing import Any

from imbue.system_interface.harnesses.antigravity.agy_transcript import DecodedStep
from imbue.system_interface.harnesses.antigravity.agy_transcript import DecodedToolCall
from imbue.system_interface.harnesses.antigravity.session_parser import parse_step as agy_parse_step
from imbue.system_interface.harnesses.claude.session_parser import parse_lines as claude_parse_lines
from imbue.system_interface.harnesses.codex.session_parser import parse_lines as codex_parse_lines
from imbue.system_interface.harnesses.pi_coding.session_parser import parse_record

_ASSISTANT_REQUIRED = {
    "timestamp",
    "type",
    "event_id",
    "source",
    "role",
    "model",
    "text",
    "tool_calls",
    "stop_reason",
    "usage",
    "message_uuid",
    "is_auth_error",
    "is_api_error",
    "api_error_kind",
    "is_provider_fault",
}
_USER_REQUIRED = {"timestamp", "type", "event_id", "source", "role", "content", "message_uuid"}
_TOOL_RESULT_REQUIRED = {
    "timestamp",
    "type",
    "event_id",
    "source",
    "tool_call_id",
    "tool_name",
    "output_chars",
    "is_error",
    "message_uuid",
}
_TOOL_CALL_REQUIRED = {"tool_call_id", "tool_name", "input_chars", "header_label", "caption_label"}

_REQUIRED_BY_TYPE = {
    "assistant_message": _ASSISTANT_REQUIRED,
    "user_message": _USER_REQUIRED,
    "tool_result": _TOOL_RESULT_REQUIRED,
}


# The payload fields the wire must NEVER carry (the payload-free contract): raw tool
# inputs, raw outputs, and thinking text stay on disk, served only by the detail endpoint.
_FORBIDDEN_PAYLOAD_EVENT_FIELDS = {"output", "thinking"}
_FORBIDDEN_PAYLOAD_TOOL_CALL_FIELDS = {"input_preview", "input"}


def _assert_contract(events: list[dict[str, Any]], harness: str) -> None:
    assert events, f"{harness}: fixture produced no events"
    seen_types: set[str] = set()
    for event in events:
        required = _REQUIRED_BY_TYPE.get(str(event.get("type")))
        if required is None:
            continue
        seen_types.add(str(event["type"]))
        missing = required - event.keys()
        assert not missing, f"{harness} {event['type']} is missing contract fields: {sorted(missing)}"
        forbidden = _FORBIDDEN_PAYLOAD_EVENT_FIELDS & event.keys()
        assert not forbidden, f"{harness} {event['type']} carries payloads on the wire: {sorted(forbidden)}"
        for tool_call in event.get("tool_calls") or ():
            tc_missing = _TOOL_CALL_REQUIRED - tool_call.keys()
            assert not tc_missing, f"{harness} tool_call is missing contract fields: {sorted(tc_missing)}"
            tc_forbidden = _FORBIDDEN_PAYLOAD_TOOL_CALL_FIELDS & tool_call.keys()
            assert not tc_forbidden, f"{harness} tool_call carries payloads on the wire: {sorted(tc_forbidden)}"
    assert seen_types == set(_REQUIRED_BY_TYPE), (
        f"{harness}: fixture must exercise all three core types, got {seen_types}"
    )


def test_claude_events_satisfy_the_contract() -> None:
    lines = [
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "stop_reason": None,
                    "content": [
                        {"type": "text", "text": "on it"},
                        {"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "ls"}},
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "uuid": "r1",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "files"}],
                },
            }
        ),
    ]
    _assert_contract(claude_parse_lines(lines), "claude")


def test_codex_events_satisfy_the_contract() -> None:
    tool_names: dict[str, str] = {}
    lines = [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hi"},
        },
        {
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "on it"}]},
        },
        {
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
        },
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "files"},
        },
    ]
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        events.extend(codex_parse_lines(line, index, tool_names))
    _assert_contract(events, "codex")


def test_pi_events_satisfy_the_contract() -> None:
    records = [
        {
            "id": "e1",
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "message",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "id": "e2",
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "message",
            "message": {
                "role": "assistant",
                "model": "gpt-things",
                "stopReason": "stop",
                "content": [
                    {"type": "text", "text": "on it"},
                    {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                ],
            },
        },
        {
            "id": "e3",
            "timestamp": "2026-01-01T00:00:02Z",
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "files"}],
            },
        },
    ]
    events: list[dict[str, Any]] = []
    for record in records:
        events.extend(parse_record(record))
    _assert_contract(events, "pi")


def test_codex_and_pi_stamp_tk_decoration_and_permission_objects_resident() -> None:
    """The two structured facts the chat reads out of a tool result are stamped resident on
    EVERY harness, however large the raw output -- the output itself never reaches the
    wire, so a fact left unstamped would be gone for the frontend."""
    filler = "x" * 3000
    tk_output = filler + "\nUpdated cod-step-abcd -> closed\ntk-step cod-step-abcd summary: done"
    permission_output = filler + '\n{"request_id": "req-1", "payload": {"kind": "predefined"}, "rationale": "need it"}'

    codex_tk = codex_parse_lines(
        {
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": tk_output},
        },
        0,
        {"c1": "exec"},
    )[0]
    assert "Updated cod-step-abcd -> closed" in codex_tk["tk_stamp"]
    assert "tk-step cod-step-abcd summary: done" in codex_tk["tk_stamp"]

    codex_permission = codex_parse_lines(
        {
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c2", "output": permission_output},
        },
        1,
        {"c2": "exec"},
    )[0]
    assert codex_permission["permission_request"]["request_id"] == "req-1"
    assert "output" not in codex_permission

    pi_tk = parse_record(
        {
            "id": "p1",
            "timestamp": "2026-01-01T00:00:03Z",
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "bash",
                "content": [{"type": "text", "text": tk_output}],
            },
        }
    )[0]
    assert "Updated cod-step-abcd -> closed" in pi_tk["tk_stamp"]

    pi_permission = parse_record(
        {
            "id": "p2",
            "timestamp": "2026-01-01T00:00:04Z",
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "c2",
                "toolName": "bash",
                "content": [{"type": "text", "text": permission_output}],
            },
        }
    )[0]
    assert pi_permission["permission_request"]["request_id"] == "req-1"


def test_codex_error_marker_and_snippet_stamp_from_the_full_output() -> None:
    """`is_error` and the resident snippet both read the FULL output, so a failed script
    whose failure marker sits ahead of a large body still reads as a failure at a glance."""
    filler = "x" * 3000
    output = "Script failed: boom\n" + filler + '\n{"request_id": "req-9", "payload": {"kind": "predefined"}}'
    event = codex_parse_lines(
        {
            "timestamp": "2026-01-01T00:00:05Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c9", "output": output},
        },
        0,
        {"c9": "exec"},
    )[0]
    assert event["permission_request"]["request_id"] == "req-9"
    assert event["is_error"] is True
    assert event["error_snippet"] == "Script failed: boom"


def test_antigravity_events_satisfy_the_contract() -> None:
    """agy decodes from a protobuf ``steps`` row rather than JSONL, so its fixture is built
    from :class:`DecodedStep` directly -- the decoder has its own tests. One step per core
    type; the tool step yields BOTH the call and (once terminal) its result."""
    base = DecodedStep(
        conv_id="c1",
        idx=0,
        step_type_name="USER_INPUT",
        status_name="DONE",
        source_name="USER_EXPLICIT",
        created_at="2026-01-01T00:00:00Z",
        is_terminal=True,
    )
    user_step = base.model_copy_update(("user_text", "hi"))
    assistant_step = base.model_copy_update(
        ("idx", 1), ("step_type_name", "PLANNER_RESPONSE"), ("assistant_text", "hello")
    )
    tool_step = base.model_copy_update(
        ("idx", 2),
        ("step_type_name", "RUN_COMMAND"),
        (
            "tool_call",
            DecodedToolCall(
                call_id="t1", name="run_command", args='{"CommandLine": "ls"}', tool_summary="", tool_action=""
            ),
        ),
        ("tool_result_text", "a.txt"),
    )
    events = [event for step in (user_step, assistant_step, tool_step) for event in agy_parse_step(step)]
    _assert_contract(events, "antigravity")
