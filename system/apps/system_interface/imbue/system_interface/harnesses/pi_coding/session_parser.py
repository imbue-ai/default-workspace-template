"""Parse a pi agent's native session JSONL into the web-UI event schema.

pi writes its conversation to its OWN session file -- an append-only JSONL under
``<agent_state_dir>/plugin/pi_coding/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl`` --
where each line is a record ``{type, id, parentId, timestamp, ...}`` forming a
parent-linked chain. :class:`PiSessionWatcher` tails that file (the pi analogue of
codex's rollout), following it across a ``/new`` rotation via the ``pi_session_file``
marker. We read pi's own file -- NOT mngr's ``logs/<type>_transcript`` mirror -- the
same way the codex watcher reads codex's live rollout, not its mirror.

This module maps those records into the *exact* dict shape the web UI consumes -- the
same shape ``claude``/``codex`` emit -- so the transport, the frontend, and the activity
tracker need no pi-specific branches.

Record types (verified live): ``session`` / ``model_change`` / ``thinking_level_change``
carry no transcript-visible content and are dropped; ``message`` wraps a pi
``AgentMessage`` (``user`` / ``assistant`` / ``toolResult``). An assistant message
carries interleaved ``text`` / ``thinking`` / ``toolCall`` content blocks (1..N tool
calls per message); **thinking blocks are dropped entirely and never rendered.**

Event ids use pi's own stable record ``id`` (``pi-<id>``), so a re-serialised/resumed
record dedups against what we already emitted (the spine's stable-id rule; the same
reason codex keys on its message id / call_id). A tool call correlates to its result by
pi's ``toolu_...`` id (assistant ``toolCall.id`` == toolResult ``toolCallId``).
"""

from __future__ import annotations

import json
from typing import Any

from imbue.system_interface.harnesses.auth_errors import is_auth_error_text
from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.message_display import stamp_user_message_display
from imbue.system_interface.harnesses.pi_coding.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.pi_coding.tool_labels import shell_command
from imbue.system_interface.harnesses.pi_coding.tool_labels import tool_labels
from imbue.system_interface.harnesses.tool_output import classify_tool_call_display
from imbue.system_interface.harnesses.tool_output import find_permission_request
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import truncate_tool_output

# "common" here means the normalized/common event *form*, not the on-disk common-transcript
# file (which we do NOT read). Nothing in the pipeline branches on this string.
SOURCE = "pi-coding/common_transcript"

# pi records always carry a model on the assistant message; surface the same placeholder
# claude/codex use when it is somehow absent, keeping the non-optional ``model`` populated.
_UNKNOWN_MODEL = "unknown"


def _text_from_content(content: Any) -> str:
    """Join the ``text`` of ``text`` content blocks. Thinking/tool blocks are skipped.

    pi user content is occasionally a bare string (not a block list); handle both.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _input_preview(tool_name: str, raw_input: str) -> str:
    """The stored ``input_preview``: the raw arguments JSON, truncated to the shared cap --
    but left whole for a tk lifecycle command, whose ``--step`` titles / close summaries the
    step timeline reads in full (``keeps_full_tool_input``)."""
    if keeps_full_tool_input(tool_name, raw_input):
        return raw_input
    if len(raw_input) > MAX_TOOL_INPUT_PREVIEW_LENGTH:
        return raw_input[:MAX_TOOL_INPUT_PREVIEW_LENGTH] + "..."
    return raw_input


def _labelled_tool_call(block: dict[str, Any]) -> dict[str, str]:
    """A pi ``toolCall`` content block carrying its own human labels.

    Labels come from the RAW arguments (not the truncated preview), so a tk command's
    verb is read off the whole command. The stored ``input_preview`` is truncated for
    display, except for the tk bodies the timeline needs whole (see :func:`_input_preview`).
    """
    call_id = str(block.get("id", ""))
    tool_name = str(block.get("name", ""))
    raw_input = json.dumps(block.get("arguments", {}))
    header_label, caption_label = tool_labels(tool_name, raw_input)
    tool_call = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "input_preview": _input_preview(tool_name, raw_input),
        "header_label": header_label,
        "caption_label": caption_label,
    }
    # The render decision ships with the call: a PURE tk lifecycle call is a hidden
    # structural marker (the hide rule is stricter than the truncation exemption -- see
    # tool_output.is_pure_tk_lifecycle_command); a latchkey POST renders as the card.
    command = shell_command(tool_name, raw_input)
    is_pure_tk = command is not None and is_pure_tk_lifecycle_command(command)
    display = classify_tool_call_display(is_pure_tk=is_pure_tk, raw_input=raw_input)
    if display is not None:
        tool_call["display"] = display.value
    return tool_call


def _tool_calls_from_content(content: Any) -> list[dict[str, str]]:
    """Every ``toolCall`` block in an assistant message, labelled (in source order)."""
    if not isinstance(content, list):
        return []
    return [
        _labelled_tool_call(block) for block in content if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def _usage(message: dict[str, Any]) -> dict[str, int | None] | None:
    """Map pi's ``usage`` onto the common token shape, or None when absent."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("input"),
        "output_tokens": usage.get("output"),
        "cache_read_tokens": usage.get("cacheRead"),
        "cache_write_tokens": usage.get("cacheWrite"),
    }


def _assistant_event(event_id: str, timestamp: str, message: dict[str, Any]) -> dict[str, Any]:
    model = message.get("model")
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "assistant",
        "model": model if isinstance(model, str) and model else _UNKNOWN_MODEL,
        "text": _text_from_content(message.get("content")),
        "tool_calls": _tool_calls_from_content(message.get("content")),
        "stop_reason": message.get("stopReason"),
        "usage": _usage(message),
        "message_uuid": event_id,
        # Measured: a rejected key ends the assistant message with `stopReason: "error"` and
        # `errorMessage` holding the provider's raw body -- for anthropic,
        # `401 {"type":"error","error":{"type":"authentication_error", ...}}`. pi passes the
        # provider's words through rather than writing its own, so the shared vocabulary is
        # what reads them.
        "is_auth_error": is_auth_error_text(str(message.get("errorMessage") or "")),
        # Required by the shared contract (Response.ts). Still deferred: an auth failure is
        # distinguishable (above), but pi does not say whether any OTHER error was the
        # provider's fault, so guessing a kind would be worse than saying nothing.
        "is_api_error": False,
        "api_error_kind": None,
        "is_provider_fault": False,
    }


def _user_event(event_id: str, timestamp: str, message: dict[str, Any]) -> dict[str, Any]:
    content = _text_from_content(message.get("content"))
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "user",
        "content": content,
        "message_uuid": event_id,
    }
    # The shared render decision -- pi gets the same detector table as claude and codex.
    stamp_user_message_display(event, content)
    return event


def _tool_result_event(event_id: str, timestamp: str, message: dict[str, Any]) -> dict[str, Any]:
    raw_output = _text_from_content(message.get("content"))
    # Lift the permission-request object and preserve tk step decoration BEFORE truncation
    # (shared with the claude/codex parsers -- see ``harnesses/tool_output``).
    permission_request = find_permission_request(raw_output)
    output = truncate_tool_output(raw_output, permission_request)
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "tool_result",
        "event_id": event_id,
        "source": SOURCE,
        "tool_call_id": str(message.get("toolCallId", "")),
        "tool_name": str(message.get("toolName", "")),
        "output": output,
        "is_error": message.get("isError") is True,
        "message_uuid": event_id,
    }
    if permission_request is not None:
        event["permission_request"] = permission_request.details
    return event


def parse_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one pi native session record to zero or one UI event dict (``[]`` to skip).

    Only ``message`` records produce events; ``session`` / ``model_change`` /
    ``thinking_level_change`` are dropped. The event id is pi's own stable record ``id``.
    """
    if record.get("type") != "message":
        return []
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id:
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else ""
    event_id = f"pi-{record_id}"
    role = message.get("role")
    if role == "user":
        return [_user_event(event_id, timestamp, message)]
    if role == "assistant":
        return [_assistant_event(event_id, timestamp, message)]
    if role == "toolResult":
        return [_tool_result_event(event_id, timestamp, message)]
    # bashExecution / custom / branchSummary / compactionSummary etc. -> not rendered.
    return []
