"""Adapt mngr's codex *common-transcript* records into the web-UI event schema.

mngr_codex's agent-side converter (``common_transcript_convert.py``) already
normalizes codex's native rollout JSONL into mngr's harness-agnostic "common
transcript" schema (``user_message`` / ``assistant_message`` / ``tool_result``),
written append-only to
``<agent_state_dir>/events/codex/common_transcript/events.jsonl``.

This module maps those already-parsed records into the *exact* dict shape the web
UI consumes -- the same shape :mod:`session_parser` emits for claude -- so the
transport (SSE), the frontend (``Response.ts``), and the activity tracker need no
codex-specific branches. It is the codex analogue of :mod:`session_parser`, except
the heavy lifting (native-format parsing) is already done on the agent side, so all
that is left is a field-for-field remap.

The remap is lossy by design for this first cut: codex's common transcript drops
token usage, reasoning, and subagent linkage, so the UI fields those feed
(``usage``, ``subagent_metadata``) are filled with nulls/omitted. ``is_auth_error``
is always ``False`` here -- codex auth is done in the terminal for now, and
surfacing codex auth errors in the UI is a later slice.

Event ids are passed through unchanged: the converter derives them from the
transcript's per-line ordinal (e.g. ``line-5-assistant``), which is stable for the
whole-file reads :class:`CodexSessionWatcher` performs, and unique per record -- so
the same id doubles as the UI ``message_uuid`` and drives the transport's dedup.
"""

from __future__ import annotations

from typing import Any

# Kept identical to the ``source`` mngr_codex stamps on every common-transcript
# record. Nothing in the pipeline branches on this string today (the claude path
# emits ``claude/common_transcript`` the same way); it is preserved verbatim so a
# consumer that later wants to tell the harnesses apart can.
_SOURCE = "codex/common_transcript"

# Codex's common transcript never carries a model slug per message (it is null),
# so we surface the same placeholder session_parser uses for claude when the model
# is absent, keeping the frontend's non-optional ``model`` field populated.
_UNKNOWN_MODEL = "unknown"


def _assistant_text(record: dict[str, Any]) -> str:
    """Return the assistant turn's text, falling back to the ordered text parts.

    The converter always sets ``text`` for an assistant record, but a tool-call-only
    turn has an empty ``text`` and carries the content in ``parts``; joining the
    text parts is a defensive fallback so no visible text is dropped.
    """
    text = record.get("text")
    if isinstance(text, str) and text:
        return text
    parts = record.get("parts")
    if isinstance(parts, list):
        chunks = [
            part.get("content", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and part.get("content")
        ]
        if chunks:
            return "\n".join(chunks)
    return ""


def _tool_calls(record: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize the record's ``tool_calls`` to the UI's ``ToolCall`` shape.

    Reduces each entry to the three fields the frontend ``ToolCall`` interface
    requires (``tool_call_id`` / ``tool_name`` / ``input_preview``); codex has no
    Agent-tool / subagent concept, so the optional subagent fields are never added.
    """
    raw_calls = record.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[dict[str, str]] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        calls.append(
            {
                "tool_call_id": str(raw.get("tool_call_id", "")),
                "tool_name": str(raw.get("tool_name", "")),
                "input_preview": str(raw.get("input_preview", "")),
            }
        )
    return calls


def adapt_common_transcript_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map one codex common-transcript record to a UI event dict, or ``None`` to skip.

    Returns ``None`` for records with no ``event_id`` or an unrecognized ``type``
    (the converter only emits the three handled types, but we stay tolerant of
    future additions rather than crashing the watcher).
    """
    event_id = record.get("event_id")
    timestamp = record.get("timestamp")
    if not isinstance(event_id, str) or not event_id or not isinstance(timestamp, str):
        return None

    record_type = record.get("type")

    if record_type == "user_message":
        return {
            "timestamp": timestamp,
            "type": "user_message",
            "event_id": event_id,
            "source": _SOURCE,
            "role": "user",
            "content": str(record.get("content", "")),
            # The converter's per-line event_id is already unique; reuse it as the
            # message_uuid the frontend keys replay/scroll state on.
            "message_uuid": event_id,
        }

    if record_type == "assistant_message":
        model = record.get("model")
        return {
            "timestamp": timestamp,
            "type": "assistant_message",
            "event_id": event_id,
            "source": _SOURCE,
            "role": "assistant",
            "model": model if isinstance(model, str) and model else _UNKNOWN_MODEL,
            "text": _assistant_text(record),
            "tool_calls": _tool_calls(record),
            # codex names it finish_reason; the UI schema calls it stop_reason.
            "stop_reason": record.get("finish_reason"),
            # Token usage is dropped by the common transcript (lossy v1).
            "usage": None,
            "message_uuid": event_id,
            # Required by the frontend AssistantMessageEvent interface. codex auth
            # is handled in the terminal for now, so never flag an auth error here.
            "is_auth_error": False,
        }

    if record_type == "tool_result":
        return {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": event_id,
            "source": _SOURCE,
            "tool_call_id": str(record.get("tool_call_id", "")),
            "tool_name": str(record.get("tool_name", "")),
            "output": str(record.get("output", "")),
            "is_error": bool(record.get("is_error", False)),
            "message_uuid": event_id,
        }

    return None
