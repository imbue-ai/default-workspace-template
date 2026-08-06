"""Tests for :mod:`codex_session_parser` -- mapping raw codex rollout lines to the
web-UI event schema. Focused on the load-bearing invariants: stable, position-
independent event ids (so codex's re-serialised / re-read duplicates dedup), the
user-bubble / turn-abort sourcing, and the self-contained web-search expansion.
"""

from __future__ import annotations

from typing import Any

from imbue.system_interface.harnesses.codex.session_parser import parse_lines


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
        "payload": {"type": "message", "role": "assistant", "id": "msg_abc", "content": [{"type": "output_text", "text": "hi"}]},
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


def test_non_conversation_lines_are_dropped() -> None:
    assert parse_lines({"timestamp": "t", "type": "session_meta", "payload": {"type": "x"}}, 0, {}) == []
    assert parse_lines({"timestamp": "t", "type": "turn_context", "payload": {}}, 0, {}) == []
    non_dict_payload: dict[str, Any] = {"type": "event_msg", "payload": "not-a-dict"}
    assert parse_lines(non_dict_payload, 0, {}) == []
