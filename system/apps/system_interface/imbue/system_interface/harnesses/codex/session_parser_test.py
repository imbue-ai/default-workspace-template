"""Tests for :mod:`codex_session_parser` -- mapping raw codex rollout lines to the
web-UI event schema. Focused on the load-bearing invariants: stable, position-
independent event ids (so codex's re-serialised / re-read duplicates dedup), the
user-bubble / turn-abort sourcing, and the self-contained web-search expansion.
"""

from __future__ import annotations

from typing import Any

from imbue.system_interface.harnesses.codex.session_parser import _labelled_tool_call
from imbue.system_interface.harnesses.codex.session_parser import THINKING_SOURCE_MARKER_TYPE
from imbue.system_interface.harnesses.codex.session_parser import parse_line_detail
from imbue.system_interface.harnesses.codex.session_parser import parse_lines
from imbue.system_interface.harnesses.codex.session_parser import parse_reasoning_detail
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.codex.tool_labels import CODE_MODE_TOOL_NAME


def _user_line(text: str, timestamp: str = "2026-07-19T10:00:00.123Z") -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def _item_user_line(text: str, timestamp: str = "2026-07-19T10:00:00.123Z") -> dict:
    """A new-schema user turn: ``event_msg`` / ``item_completed`` with a ``UserMessage``
    item. Shape taken from a real codex rollout after the item-model upgrade."""
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": text}]},
        },
    }


def _item_line(item_type: str) -> dict:
    """A non-user item_completed line (a display duplicate of a response_item)."""
    return {
        "timestamp": "2026-07-19T10:00:00.123Z",
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": {"type": item_type, "content": [{"type": "Text", "text": "x"}]}},
    }


def test_user_bubble_id_is_stable_across_line_index() -> None:
    """The same user message re-read at a different physical line (e.g. a rollout
    compressed then re-materialised, repointing the marker and forcing a re-read from
    byte 0) must yield the SAME event id so the watcher dedups it -- not a duplicate
    bubble. This is why the id is content-derived, not line-index-derived."""
    line = _user_line("hello codex")
    first = parse_lines(line, 5, {})
    reread = parse_lines(line, 999, {})
    assert first == reread
    assert first[0]["event_id"] == reread[0]["event_id"]
    assert first[0]["type"] == "user_message"
    assert first[0]["content"] == "hello codex"


def test_user_bubble_id_differs_for_distinct_sends() -> None:
    """Distinct sends (different text, or same text at a different time) must NOT
    collide, or a genuine repeat would be swallowed as a duplicate."""
    a = parse_lines(_user_line("yes"), 1, {})[0]["event_id"]
    b = parse_lines(_user_line("no"), 2, {})[0]["event_id"]
    c = parse_lines(_user_line("yes", timestamp="2026-07-19T10:00:05.000Z"), 3, {})[0]["event_id"]
    # different text
    assert a != b
    # same text, different timestamp
    assert a != c


def test_empty_user_message_is_skipped() -> None:
    assert parse_lines(_user_line(""), 0, {}) == []


def test_new_schema_item_completed_user_message_renders() -> None:
    """After the codex item-model upgrade the human turn arrives as
    ``item_completed`` / ``UserMessage``; it must render as the same user bubble the
    old ``user_message`` form produced."""
    events = parse_lines(_item_user_line("how we doin'"), 5, {})
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "how we doin'"


def test_old_and_new_user_forms_share_one_event_id() -> None:
    """Both user-turn shapes derive the same content-based id, so a rollout that
    somehow carried both dedups to a single bubble rather than showing it twice."""
    old = parse_lines(_user_line("how we doin'"), 5, {})[0]["event_id"]
    new = parse_lines(_item_user_line("how we doin'"), 9, {})[0]["event_id"]
    assert old == new


def test_empty_item_user_message_is_skipped() -> None:
    assert parse_lines(_item_user_line(""), 0, {}) == []


def test_non_user_item_completed_is_dropped() -> None:
    """Agent/command/reasoning items are display duplicates of response_item lines we
    already parse, so they must not double-render."""
    assert parse_lines(_item_line("AgentMessage"), 1, {}) == []
    assert parse_lines(_item_line("CommandExecution"), 2, {}) == []
    assert parse_lines(_item_line("Reasoning"), 3, {}) == []


def test_turn_aborted_emits_marker() -> None:
    """A user interrupt is surfaced as a lightweight turn_aborted marker (used to
    clear a stuck 'Running' dot), not dropped."""
    line = {"timestamp": "2026-07-19T10:00:01Z", "type": "event_msg", "payload": {"type": "turn_aborted"}}
    events = parse_lines(line, 7, {})
    assert len(events) == 1
    assert events[0]["type"] == "special"
    assert events[0]["kind"] == "turn_aborted"


def test_task_markers_become_turn_lifecycle_events() -> None:
    """task_started/task_complete are surfaced as turn_started/turn_completed markers
    (the codex activity latch), not dropped."""
    started = parse_lines(
        {"timestamp": "2026-07-19T10:00:00Z", "type": "event_msg", "payload": {"type": "task_started"}}, 1, {}
    )
    assert len(started) == 1
    assert started[0]["type"] == "special"
    assert started[0]["kind"] == "turn_started"
    completed = parse_lines(
        {"timestamp": "2026-07-19T10:00:02Z", "type": "event_msg", "payload": {"type": "task_complete"}}, 2, {}
    )
    assert len(completed) == 1
    assert completed[0]["type"] == "special"
    assert completed[0]["kind"] == "turn_completed"


def test_assistant_message_id_dedups_reserialised_copies() -> None:
    """Codex re-serialises history; each copy keeps the message id, so the event id
    keys on it (stable across the physical line it is re-read at)."""
    line = {
        "timestamp": "2026-07-19T10:00:02Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": "msg_abc",
            "content": [{"type": "output_text", "text": "hi"}],
        },
    }
    first = parse_lines(line, 3, {})
    reread = parse_lines(line, 400, {})
    assert first[0]["event_id"] == reread[0]["event_id"] == "codex-msg_abc"
    assert first[0]["text"] == "hi"


def test_response_item_user_role_is_dropped() -> None:
    """The model-facing role=user item carries injected AGENTS.md / environment
    context; user bubbles come from event_msg, so this is skipped."""
    line = {
        "timestamp": "2026-07-19T10:00:03Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "prompt+injected"}]},
    }
    assert parse_lines(line, 1, {}) == []


def test_function_call_and_output_link_by_call_id() -> None:
    """A function_call registers its name; the later output recovers it by call_id."""
    name_map: dict[str, str] = {}
    call_line = {
        "timestamp": "2026-07-19T10:00:05Z",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd":"ls"}'},
    }
    out_line = {
        "timestamp": "2026-07-19T10:00:06Z",
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "file.txt"},
    }
    call = parse_lines(call_line, 1, name_map)[0]
    out = parse_lines(out_line, 2, name_map)[0]
    assert call["tool_calls"][0]["tool_call_id"] == "c1"
    assert out["tool_call_id"] == "c1"
    # recovered from the cross-line map
    assert out["tool_name"] == "shell"


def test_turn_markers_key_on_turn_id() -> None:
    """Turn-lifecycle marker ids derive from codex's stable turn_id, not the line index,
    so they survive a rollout re-materialisation (started/complete share the turn_id, kept
    distinct by the payload-type suffix)."""
    started = {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "tid1"}}
    complete = {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "tid1"}}
    aborted = {"timestamp": "t", "type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "tid1"}}
    assert parse_lines(started, 5, {})[0]["event_id"] == "codex-turn-tid1-task_started"
    assert parse_lines(complete, 9, {})[0]["event_id"] == "codex-turn-tid1-task_complete"
    assert parse_lines(aborted, 2, {})[0]["event_id"] == "codex-turn-tid1-turn_aborted"
    # Position-independent: the same turn re-read at a different line keeps its id.
    assert parse_lines(started, 999, {})[0]["event_id"] == "codex-turn-tid1-task_started"


def test_turn_markers_expose_turn_id_field() -> None:
    """The turn_id rides in the event_id, but is also surfaced as an explicit field on the
    three turn-lifecycle markers so the atomic shoulder-tap can ABA-gate on the live turn."""
    started = {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "tid1"}}
    complete = {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "tid1"}}
    aborted = {"timestamp": "t", "type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "tid1"}}
    assert parse_lines(started, 5, {})[0]["turn_id"] == "tid1"
    assert parse_lines(complete, 9, {})[0]["turn_id"] == "tid1"
    assert parse_lines(aborted, 2, {})[0]["turn_id"] == "tid1"


def test_marker_without_turn_id_falls_back_to_line_index() -> None:
    started = {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started"}}
    event = parse_lines(started, 7, {})[0]
    assert event["event_id"] == "codex-7-task_started"
    # No codex turn_id on the raw marker -> the explicit field is None (nothing to gate on).
    assert event["turn_id"] is None


def test_failed_script_output_sets_is_error() -> None:
    """A code-mode script failure (output starts with 'Script failed') flags is_error;
    a normal output does not."""
    name_map = {"c1": "exec"}
    failed = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": "c1",
            "output": "Script failed: ReferenceError: x is not defined",
        },
    }
    ok = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "done"},
    }
    assert parse_lines(failed, 1, name_map)[0]["is_error"] is True
    assert parse_lines(ok, 2, name_map)[0]["is_error"] is False


def test_labels_read_the_full_input() -> None:
    """A patch whose apply_patch call sits deep in a long program still labels as the edit
    it is: labels always derive from the FULL input (which itself never rides the event)."""
    prefix = "// " + "x" * 250 + "\n"
    js = f"{prefix}await tools.apply_patch(`*** Begin Patch\n*** Add File: newfile.py\n+print(1)\n*** End Patch`);"
    call = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": js},
    }
    tc = parse_lines(call, 1, {})[0]["tool_calls"][0]
    assert tc["header_label"] == "Tool: Write"
    # Payload-free wire: the input never rides the event; its size drives the expand.
    assert "input_preview" not in tc
    assert tc["input_chars"] == len(js)


def test_tk_command_is_stamped_resident() -> None:
    """A tk lifecycle command is stamped whole so the step timeline reads the whole plan
    without fetching the input."""
    js = 'await tools.exec_command({"cmd":"tk start wor-step-abc"})' + "x" * 300
    call = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": js},
    }
    tc = parse_lines(call, 1, {})[0]["tool_calls"][0]
    assert tc["tk_command"] == "tk start wor-step-abc"


def test_detail_reconstructs_the_full_input_and_output() -> None:
    """The detail parser hands back the whole payloads the resident events omit."""
    js = 'await tools.exec_command({"cmd":"echo ' + "a" * 500 + '"})'
    call = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": js},
    }
    tc = parse_lines(call, 1, {})[0]["tool_calls"][0]
    assert "input_preview" not in tc
    detail = parse_line_detail(call, 1)["codex-call-c1"]
    assert detail["inputs_by_tool_call_id"]["c1"] == js

    result = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "big " * 999},
    }
    detail = parse_line_detail(result, 2)["codex-result-c1"]
    assert detail["output"] == "big " * 999


def test_reasoning_line_marks_the_next_assistant_event_thinking_source() -> None:
    """A reasoning item with readable summaries yields the internal thinking-source marker
    (never a transcript event); one with no readable text yields an unreadable marker."""
    readable = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thought"}], "content": []},
    }
    assert parse_lines(readable, 1, {}) == [{"type": THINKING_SOURCE_MARKER_TYPE, "readable": True}]
    assert parse_reasoning_detail(readable) == "thought"

    unreadable = {
        "timestamp": "t",
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": [], "content": [{"type": "encrypted", "data": "x"}]},
    }
    assert parse_lines(unreadable, 1, {}) == [{"type": THINKING_SOURCE_MARKER_TYPE, "readable": False}]
    assert parse_reasoning_detail(unreadable) is None


def test_non_conversation_lines_are_dropped() -> None:
    assert parse_lines({"timestamp": "t", "type": "session_meta", "payload": {"type": "x"}}, 0, {}) == []
    assert parse_lines({"timestamp": "t", "type": "turn_context", "payload": {}}, 0, {}) == []
    non_dict_payload: dict[str, Any] = {"type": "event_msg", "payload": "not-a-dict"}
    assert parse_lines(non_dict_payload, 0, {}) == []


def _assistant_line(msg_id: str, text: str) -> dict[str, Any]:
    return {
        "timestamp": "t",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": msg_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def test_turn_context_effective_model_stamps_assistant_messages() -> None:
    # §4b: the EFFECTIVE per-turn model is read from turn_context and stamped on the assistant
    # messages that follow it, replacing the "unknown" placeholder.
    turn_state: dict[str, Any] = {}
    before = parse_lines(_assistant_line("m1", "hi"), 0, {}, turn_state)
    assert before[0]["model"] == "unknown"

    context = {"timestamp": "t", "type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "high"}}
    assert parse_lines(context, 1, {}, turn_state) == []
    assert turn_state == {"model": "gpt-5.6-sol", "effort": "high"}

    after = parse_lines(_assistant_line("m2", "yo"), 2, {}, turn_state)
    assert after[0]["model"] == "gpt-5.6-sol"

    # A later turn's fallback model (differing from the first) is reflected on its assistant messages.
    fallback = {"timestamp": "t", "type": "turn_context", "payload": {"model": "gpt-5.2", "effort": "low"}}
    assert parse_lines(fallback, 3, {}, turn_state) == []
    later = parse_lines(_assistant_line("m3", "z"), 4, {}, turn_state)
    assert later[0]["model"] == "gpt-5.2"


# --- code mode batches several delegated calls into ONE tool call -------------------------
# Measured on codex-cli 0.147.0: one `custom_tool_call` holding three `tools.exec_command`
# calls produced three PreToolUse events with three unrelated `tool_use_id`s and no field
# naming the outer call. So "this call is ONLY an X" is unknowable for a batched program, and
# both structural verdicts are derived from the FIRST call in it.


def _display(js: str) -> str | None:
    return _labelled_tool_call("c1", CODE_MODE_TOOL_NAME, js).get("display")


def _exec(cmd: str) -> str:
    return f'const r = await tools.exec_command({{cmd:"{cmd}",workdir:"/w"}});'


def test_a_lone_tk_lifecycle_call_is_still_hidden() -> None:
    assert _display(_exec("tk start s1")) == "hidden"


def test_a_batched_program_containing_tk_is_not_hidden() -> None:
    """THE bug this guards. `tk start` first and real work second classified the whole call as a
    structural marker, so the real work vanished from the chat entirely -- the exact failure the
    standalone policies exist to prevent."""
    assert _display(_exec("tk start s1") + "\n" + _exec("sed -i s/a/b/ prod.py")) is None


def test_a_lone_permission_request_still_renders_its_card() -> None:
    post = "curl -XPOST http://latchkey-self.invalid/permission-requests -d @/tmp/r.json"
    assert _display(_exec(post)) == "permission_request"


def test_two_permission_requests_in_one_program_render_no_card() -> None:
    """The card is built from the first request object echoed in the result, so a second one is
    never shown -- a buttonless or wrong card. Rendering as ordinary work is honest."""
    post = "curl -XPOST http://latchkey-self.invalid/permission-requests -d @/tmp/{}.json"
    assert _display(_exec(post.format("a")) + "\n" + _exec(post.format("b"))) is None


def test_ordinary_work_is_unaffected() -> None:
    assert _display(_exec("pytest -q")) is None
    assert _display(_exec("pytest -q") + "\n" + _exec("ruff check .")) is None


_CODEX_401 = (
    "unexpected status 401 Unauthorized: Incorrect API key provided: sk-bogus000., "
    "auth error: 401, auth error code: invalid_api_key"
)


def _task_complete(error: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "task_complete", "turn_id": "tid1"}
    if error is not None:
        payload["error"] = error
    return {"timestamp": "2026-07-19T10:00:02Z", "type": "event_msg", "payload": payload}


def test_a_failed_turn_surfaces_its_reason_as_a_message() -> None:
    """`task_complete.error` is the ONLY durable copy of why a turn died.

    codex classes its live `EventMsg::Error` non-persistent, so it never reaches the rollout.
    Keeping the reason on the marker alone meant it existed and was never shown, and once the
    turn ended it was unrecoverable.
    """
    events = parse_lines(_task_complete({"message": _CODEX_401}), 2, {})
    # The failure is ordered BEFORE the marker: it happened first.
    assert [event["type"] for event in events] == ["assistant_message", SPECIAL_EVENT_TYPE]
    failure = events[0]
    assert failure["text"] == _CODEX_401
    assert failure["is_auth_error"] is True
    assert failure["is_api_error"] is False
    # Two events out of one record need two ids, or the frontend dedupes one away.
    assert failure["event_id"] != events[1]["event_id"]


def test_a_failed_turn_classifies_off_the_structured_tag() -> None:
    """The tag survives codex rewording its prose; the prose does not."""
    events = parse_lines(
        _task_complete({"message": "the model is busy", "codex_error_info": {"type": "server_error"}}), 2, {}
    )
    failure = events[0]
    assert failure["api_error_kind"] == "api_error"
    assert failure["is_provider_fault"] is True


def test_a_spent_quota_is_an_auth_failure_not_a_provider_fault() -> None:
    """Not an authentication failure in the HTTP sense, but the only way forward is different
    credentials -- which is exactly what the auth subtext offers."""
    events = parse_lines(
        _task_complete({"message": "you have hit your usage_limit_exceeded", "codex_error_info": {"type": "quota"}}),
        2,
        {},
    )
    failure = events[0]
    assert failure["is_auth_error"] is True
    assert failure["is_api_error"] is False


def test_a_clean_turn_still_yields_only_its_marker() -> None:
    events = parse_lines(_task_complete(None), 2, {})
    assert [event["type"] for event in events] == [SPECIAL_EVENT_TYPE]
