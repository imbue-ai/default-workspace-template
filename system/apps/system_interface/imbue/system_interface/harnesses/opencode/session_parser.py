"""Map opencode ``message``/``part`` rows into the web-UI event schema.

The opencode analogue of :mod:`pi_coding.session_parser`. Where pi maps one native JSONL
record, opencode maps one ``message`` row plus all of its ``part`` rows (read from
``opencode.db`` by :mod:`db_reader`) into the *exact* dict shape the web UI consumes -- the
same ``user_message`` / ``assistant_message`` / ``tool_result`` shape claude/codex/pi emit, so
the transport, the frontend, and the activity tracker need no opencode-specific branches.

This reproduces the plugin's ``buildCommonRecords`` (``mngr_opencode_plugin.ts``) so the
DB-tailed transcript and the plugin's ``mngr transcript`` output agree -- same ``event_id``s
(``<msg>-user`` / ``<msg>-assistant`` / ``<prt>-tool_result``), same fields -- only sourcing
ids from DB columns rather than SDK event properties.

Parity with pi: text parts are joined (skipping ``synthetic``); ``reasoning`` parts (thinking)
are dropped and never rendered, exactly as pi drops thinking blocks; ``usage`` is emitted from
the message's token accounting, exactly as pi emits usage. ``step-start``/``step-finish``
(turn bookkeeping) and ``patch`` (a post-edit summary with no ``callID`` -- the ``edit``/``write``
tool part already carries the action) are skipped.

A tool call's ``tool_result`` is withheld until the tool part is terminal
(``completed``/``error``) -- the two-phase emission that keeps the live activity caption on the
running tool until it produces a result (mirrors codex/antigravity).
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from typing import Any

from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.events import MAX_TOOL_OUTPUT_LENGTH
from imbue.system_interface.harnesses.opencode.db_reader import OpenCodeMessage
from imbue.system_interface.harnesses.opencode.db_reader import OpenCodePart
from imbue.system_interface.harnesses.opencode.db_reader import PART_TYPE_TEXT
from imbue.system_interface.harnesses.opencode.db_reader import PART_TYPE_TOOL
from imbue.system_interface.harnesses.opencode.db_reader import TERMINAL_TOOL_STATUSES
from imbue.system_interface.harnesses.opencode.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.opencode.tool_labels import tool_labels

# "common" is the normalized event *form*, not the plugin's on-disk common transcript (which we
# do NOT read). ``db_transcript`` marks this as the DB-tailed origin; nothing branches on it.
SOURCE = "opencode/db_transcript"


def _iso(created_ms: int) -> str:
    """An opencode ms-epoch integer as an ISO-8601 ``...Z`` timestamp.

    Millisecond precision is kept (unlike the plugin, which strips it) so the activity
    tracker's stale-tail guard -- which parses this against the process-start mtime -- has full
    resolution. ``datetime.fromisoformat`` accepts the trailing ``Z`` on the targeted Python.
    """
    dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created_ms % 1000:03d}Z"


def _joined_text(parts: list[OpenCodePart]) -> str:
    """The message's visible text: every non-synthetic ``text`` part, concatenated (opencode
    streams a message's text as one growing part, so ``""`` join matches the plugin)."""
    return "".join(part.text for part in parts if part.kind == PART_TYPE_TEXT and not part.synthetic and part.text)


def _usage(tokens: dict[str, Any] | None) -> dict[str, int | None] | None:
    """opencode's ``message.data.tokens`` mapped onto the common token shape, or None.

    Mirrors pi's ``_usage`` (opencode is at pi parity here, not stripped down). opencode nests
    cache counts under ``tokens.cache`` (``{read, write}``).
    """
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    return {
        "input_tokens": tokens.get("input"),
        "output_tokens": tokens.get("output"),
        "cache_read_tokens": cache.get("read"),
        "cache_write_tokens": cache.get("write"),
    }


def _input_preview(part: OpenCodePart) -> str:
    """The tool call's ``input_preview``: its ``state.input`` as compact JSON, truncated to the
    shared cap -- but left whole for a tk lifecycle command (the timeline reads its bodies)."""
    raw = json.dumps(part.state_input, separators=(",", ":"))
    if keeps_full_tool_input(part.tool_name, raw):
        return raw
    if len(raw) > MAX_TOOL_INPUT_PREVIEW_LENGTH:
        return raw[:MAX_TOOL_INPUT_PREVIEW_LENGTH] + "..."
    return raw


def _labelled_tool_call(part: OpenCodePart) -> dict[str, str]:
    """A tool ``part`` as a labelled tool_call block (labels off the untruncated arguments)."""
    input_preview = _input_preview(part)
    header_label, caption_label = tool_labels(part.tool_name, input_preview)
    return {
        "tool_call_id": part.call_id,
        "tool_name": part.tool_name,
        "input_preview": input_preview,
        "header_label": header_label,
        "caption_label": caption_label,
    }


def _user_event(message: OpenCodeMessage, text: str) -> dict[str, Any]:
    return {
        "timestamp": _iso(message.time_created),
        "type": "user_message",
        "event_id": f"{message.id}-user",
        "source": SOURCE,
        "role": "user",
        "content": text,
        "message_uuid": message.id,
    }


def _assistant_event(message: OpenCodeMessage, text: str, tool_calls: list[dict[str, str]]) -> dict[str, Any]:
    model = f"{message.provider_id}/{message.model_id}" if message.provider_id and message.model_id else "unknown"
    return {
        "timestamp": _iso(message.time_created),
        "type": "assistant_message",
        "event_id": f"{message.id}-assistant",
        "source": SOURCE,
        "role": "assistant",
        "model": model,
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": message.finish,
        "usage": _usage(message.tokens),
        "message_uuid": message.id,
        # opencode's transcript carries no auth-error concept; keep the field present-but-false.
        "is_auth_error": False,
    }


def _tool_result_event(message: OpenCodeMessage, part: OpenCodePart) -> dict[str, Any]:
    is_error = part.state_status == "error"
    output = part.state_error if is_error else part.state_output
    if len(output) > MAX_TOOL_OUTPUT_LENGTH:
        output = output[:MAX_TOOL_OUTPUT_LENGTH] + "..."
    return {
        "timestamp": _iso(message.time_created),
        "type": "tool_result",
        "event_id": f"{part.id}-tool_result",
        "source": SOURCE,
        "tool_call_id": part.call_id,
        "tool_name": part.tool_name,
        "output": output,
        "is_error": is_error,
        "message_uuid": part.id,
    }


def build_message_events(message: OpenCodeMessage, parts: list[OpenCodePart]) -> list[dict[str, Any]]:
    """Map one opencode message (+ all its parts) to zero or more UI event dicts.

    A user message yields at most one ``user_message`` (dropped when it has no visible text). An
    assistant message yields one ``assistant_message`` followed by a ``tool_result`` for each of
    its terminal tool parts (a still-running tool emits no result yet -- two-phase emission).
    Any other role yields nothing.
    """
    text = _joined_text(parts)
    if message.role == "user":
        return [_user_event(message, text)] if text else []
    if message.role != "assistant":
        return []

    tool_parts = [part for part in parts if part.kind == PART_TYPE_TOOL]
    events: list[dict[str, Any]] = [
        _assistant_event(message, text, [_labelled_tool_call(part) for part in tool_parts])
    ]
    for part in tool_parts:
        if part.state_status in TERMINAL_TOOL_STATUSES:
            events.append(_tool_result_event(message, part))
    return events
