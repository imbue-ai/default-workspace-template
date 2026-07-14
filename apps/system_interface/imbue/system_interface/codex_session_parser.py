"""Parse a codex agent's raw rollout JSONL into the web-UI event schema.

Codex writes its conversation as a "rollout" -- append-only JSONL where each line
is ``{"timestamp", "type", "payload": {"type", ...}}``. mngr_codex mirrors the live
rollout verbatim (no reschematising) to a stable per-agent path
``<agent_state_dir>/logs/codex_transcript/events.jsonl`` (its ``stream_transcript.sh``),
which is what :class:`CodexSessionWatcher` tails.

This module maps those raw rollout lines into the *exact* dict shape the web UI
consumes -- the same shape ``claude_session_parser`` emits for claude -- so the
transport (SSE), the frontend, and the activity tracker need no codex-specific
branches. It is the codex analogue of ``claude_session_parser``.

Sourcing rule (confirmed against codex ``policy.rs`` + real rollouts, see
blueprint/codex-rich-transcript): ``response_item`` lines are the canonical
conversation state; ``event_msg`` lines are a derived live-display stream. We build
the body from ``response_item`` -- **except user bubbles**, which come from
``event_msg`` ``user_message`` (the clean human-typed prompt). ``response_item``
role=user is the *model-facing* user role: the human prompt PLUS injected
``AGENTS.md`` / ``<environment_context>`` / ``<turn_aborted>`` /
``<subagent_notification>`` content, which we do not want as chat bubbles. Everything
else in ``event_msg`` (``agent_message`` display echoes, ``token_count``, ``task_*``)
is skipped in this core cut.

Lossy by design for this first cut -- all deferred to later slices: ``usage``
(``token_count`` -> Phase 2, and coarse), ``is_auth_error`` (lives in codex's
``logs_2.sqlite``, never the transcript), subagent linkage, tk step-progress.
``stop_reason`` is left null.

Event ids are synthesized from the rollout's physical line index. Each rollout line
is at most one UI event, so ``codex-<line>-<kind>`` is unique and stable for the
in-order, never-reparse-a-single-line reads :class:`CodexSessionWatcher` performs
(the trait that lets a line ordinal be a safe id here, unlike a single-line-reparse
watcher).
"""

from __future__ import annotations

from typing import Any

# Kept as ``codex/common_transcript`` to match the ``<harness>/common_transcript``
# label ``claude_session_parser`` stamps -- "common" here means the normalized/common
# event *form*, not the on-disk common-transcript file (which we do NOT read).
# Nothing in the pipeline branches on this string.
_SOURCE = "codex/common_transcript"

# Codex rollout messages never carry a per-message model slug, so surface the same
# placeholder ``claude_session_parser`` uses when the model is absent, keeping the
# frontend's non-optional ``model`` field populated.
_UNKNOWN_MODEL = "unknown"

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000


def _join_content_text(content: Any, want_type: str) -> str:
    """Join the ``text`` of ``content`` blocks whose ``type`` is ``want_type``."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == want_type and block.get("text")
    )


def _stringify_output(output: Any) -> str:
    """A ``*_output.output`` is either a string or a list of content items; flatten
    to a truncated string."""
    if isinstance(output, str):
        text = output
    elif isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("output") or ""))
            elif isinstance(item, str):
                parts.append(item)
        text = "".join(parts)
    else:
        text = "" if output is None else str(output)
    if len(text) > _MAX_OUTPUT_LENGTH:
        return text[:_MAX_OUTPUT_LENGTH] + "..."
    return text


def _tool_call_input_preview(payload: dict[str, Any]) -> str:
    """``function_call`` carries ``arguments`` (a JSON string); ``custom_tool_call``
    carries ``input`` (raw text, e.g. an apply_patch body)."""
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    text = "" if raw is None else str(raw)
    if len(text) > _MAX_INPUT_PREVIEW_LENGTH:
        return text[:_MAX_INPUT_PREVIEW_LENGTH] + "..."
    return text


def _assistant_event(timestamp: str, event_id: str, *, text: str, tool_calls: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": _UNKNOWN_MODEL,
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": None,  # deferred (derive from task_complete later)
        "usage": None,  # deferred (token_count -> Phase 2)
        "message_uuid": event_id,
        "is_auth_error": False,  # deferred (codex auth errors live in logs_2.sqlite)
    }


def parse_codex_rollout_line(
    record: dict[str, Any],
    line_index: int,
    tool_name_by_call_id: dict[str, str],
) -> dict[str, Any] | None:
    """Map one codex rollout line to a UI event dict, or ``None`` to skip.

    ``line_index`` is the stable physical line number (for event-id synthesis).
    ``tool_name_by_call_id`` is a mutable cross-line map so a ``function_call_output``
    can recover its tool name from the earlier ``function_call``.
    """
    outer = record.get("type")
    payload = record.get("payload")
    timestamp = record.get("timestamp", "")
    if not isinstance(payload, dict) or not isinstance(timestamp, str):
        return None
    payload_type = payload.get("type")

    # --- event_msg: only the clean human prompt; the rest is display echoes / overlay ---
    if outer == "event_msg":
        if payload_type == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                event_id = f"codex-{line_index}-user"
                return {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "user",
                    "content": text,
                    "message_uuid": event_id,
                }
        return None

    if outer != "response_item":
        return None  # session_meta, turn_context -> drop

    # --- response_item: assistant messages + tool calls/results ---
    if payload_type == "message":
        if payload.get("role") == "assistant":
            return _assistant_event(
                timestamp,
                f"codex-{line_index}-assistant",
                text=_join_content_text(payload.get("content"), "output_text"),
                tool_calls=[],
            )
        # role=user (and developer/system) -> skip; user bubbles come from event_msg.
        return None

    if payload_type in ("function_call", "custom_tool_call"):
        call_id = str(payload.get("call_id", ""))
        tool_name = str(payload.get("name", ""))
        if call_id and tool_name:
            tool_name_by_call_id[call_id] = tool_name
        return _assistant_event(
            timestamp,
            f"codex-{line_index}-assistant",
            text="",
            tool_calls=[
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "input_preview": _tool_call_input_preview(payload),
                }
            ],
        )

    if payload_type in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-{line_index}-tool_result"
        return {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": event_id,
            "source": _SOURCE,
            "tool_call_id": call_id,
            "tool_name": tool_name_by_call_id.get(call_id, ""),
            "output": _stringify_output(payload.get("output")),
            "is_error": False,
            "message_uuid": event_id,
        }

    return None
