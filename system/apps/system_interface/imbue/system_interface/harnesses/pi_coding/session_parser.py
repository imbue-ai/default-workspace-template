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
from imbue.system_interface.harnesses.error_patterns import classify_api_error
from imbue.system_interface.harnesses.error_patterns import is_provider_fault
from imbue.system_interface.harnesses.message_display import stamp_user_message_display
from imbue.system_interface.harnesses.pi_coding.tool_labels import shell_command
from imbue.system_interface.harnesses.pi_coding.tool_labels import tool_labels
from imbue.system_interface.harnesses.tool_output import classify_tool_call_display
from imbue.system_interface.harnesses.tool_output import error_snippet
from imbue.system_interface.harnesses.tool_output import find_permission_request
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import is_tk_lifecycle_anywhere
from imbue.system_interface.harnesses.tool_output import tk_stamp

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


def _labelled_tool_call(block: dict[str, Any]) -> dict[str, Any]:
    """A pi ``toolCall`` content block carrying its own human labels.

    Labels come from the FULL arguments; the input itself stays off the event (the
    payload-free wire contract) and is served whole by the detail endpoint on expand.
    """
    call_id = str(block.get("id", ""))
    tool_name = str(block.get("name", ""))
    raw_input = json.dumps(block.get("arguments", {}))
    header_label, caption_label = tool_labels(tool_name, raw_input)
    tool_call: dict[str, Any] = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "input_chars": len(raw_input),
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
    # The step progress view reads step titles/summaries out of a tk lifecycle command
    # itself, so that one command is stamped resident.
    if command is not None and is_tk_lifecycle_anywhere(command):
        tool_call["tk_command"] = command
    return tool_call


def _thinking_text(content: Any) -> str:
    """The joined text of an assistant message's ``thinking`` blocks ('' = none).

    pi records readable reasoning inline in the message, so this both stamps
    ``has_thinking`` and serves the detail endpoint.
    """
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        block.get("thinking", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("thinking"), str)
    ).strip()


def _tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
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
    # A FAILED turn puts nothing in `content` and the whole failure in `errorMessage`, so
    # reading text from `content` alone painted a blank bubble -- an agent stuck on a billing
    # rejection looked exactly like an agent that had stopped answering.
    #
    # Gated on `stopReason` rather than on `errorMessage` being present: a genuine reply that
    # quotes an error JSON (asking the agent about one, say) must not be styled as a failure.
    failed = message.get("stopReason") == "error"
    error_text = str(message.get("errorMessage") or "") if failed else ""
    text = _text_from_content(message.get("content")) or error_text
    api_error_kind = classify_api_error(error_text)
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "assistant",
        "model": model if isinstance(model, str) and model else _UNKNOWN_MODEL,
        "text": text,
        "tool_calls": _tool_calls_from_content(message.get("content")),
        "stop_reason": message.get("stopReason"),
        "usage": _usage(message),
        "message_uuid": event_id,
        # Measured: a rejected key ends the assistant message with `stopReason: "error"` and
        # `errorMessage` holding the provider's raw body -- for anthropic,
        # `401 {"type":"error","error":{"type":"authentication_error", ...}}`. pi passes the
        # provider's words through rather than writing its own, so the shared vocabulary is
        # what reads them.
        "is_auth_error": is_auth_error_text(error_text),
        # pi passes the provider's own body through, so the shared classifier reads it the same
        # way it reads claude's -- the status and the structured type are the parts that do not
        # change when a provider rewords its prose. `classify_api_error` yields to the auth
        # vocabulary, so these three are all off whenever `is_auth_error` is on.
        "is_api_error": api_error_kind is not None,
        "api_error_kind": api_error_kind,
        "is_provider_fault": is_provider_fault(api_error_kind),
    }
    if _thinking_text(message.get("content")):
        event["has_thinking"] = True
    return event


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
    # The structured facts lifted from the full output, which itself stays off the event
    # (the payload-free wire contract -- see ``harnesses/tool_output``).
    permission_request = find_permission_request(raw_output)
    is_error = message.get("isError") is True
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "tool_result",
        "event_id": event_id,
        "source": SOURCE,
        "tool_call_id": str(message.get("toolCallId", "")),
        "tool_name": str(message.get("toolName", "")),
        "output_chars": len(raw_output),
        "is_error": is_error,
        "message_uuid": event_id,
    }
    if permission_request is not None:
        event["permission_request"] = permission_request.details
    snippet = error_snippet(raw_output) if is_error else ""
    if snippet:
        event["error_snippet"] = snippet
    stamped_tk = tk_stamp(raw_output)
    if stamped_tk:
        event["tk_stamp"] = stamped_tk
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


def parse_record_detail(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Full deferred payloads by event_id for one raw pi session record.

    The read half of the payload-free wire contract: full tool inputs and readable thinking
    for an assistant record, the full output for a toolResult record.
    """
    if record.get("type") != "message":
        return {}
    record_id = record.get("id")
    message = record.get("message")
    if not isinstance(record_id, str) or not record_id or not isinstance(message, dict):
        return {}
    event_id = f"pi-{record_id}"
    role = message.get("role")
    if role == "assistant":
        content = message.get("content")
        inputs_by_tool_call_id: dict[str, str] = {}
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "toolCall" and block.get("id"):
                    inputs_by_tool_call_id[str(block["id"])] = json.dumps(block.get("arguments", {}), indent=2)
        thinking = _thinking_text(content)
        if not inputs_by_tool_call_id and not thinking:
            return {}
        return {
            event_id: {
                "inputs_by_tool_call_id": inputs_by_tool_call_id,
                "output": None,
                "thinking": thinking or None,
            }
        }
    if role == "toolResult":
        return {
            event_id: {
                "inputs_by_tool_call_id": {},
                "output": _text_from_content(message.get("content")),
                "thinking": None,
            }
        }
    return {}
