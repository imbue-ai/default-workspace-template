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

Sourcing rule (confirmed against codex ``policy.rs`` + real rollouts):
``response_item`` lines are the canonical
conversation state; ``event_msg`` lines are a derived live-display stream. We build
the body from ``response_item`` -- **except** two things taken from ``event_msg``:
(1) user bubbles, the clean human-typed prompt; and (2) the ``turn_aborted`` marker
(a user interrupt), used to clear a stuck activity dot. We take the user bubble from
the display stream rather than the canonical one because ``response_item`` role=user
is the *model-facing* user role: the human prompt PLUS injected ``AGENTS.md`` /
``<environment_context>`` / ``<turn_aborted>`` / ``<subagent_notification>`` content
with no field marking which is which, so it cannot be shown as-is. The display stream
labels the human turn explicitly, which is the clean signal.

Codex has emitted that human turn under two shapes across versions: older codex as
``event_msg`` ``user_message``; newer codex folds every display echo into
``event_msg`` ``item_completed`` with a typed ``item`` (``UserMessage`` for the human
turn, plus ``AgentMessage`` / ``CommandExecution`` / ``Reasoning`` display duplicates
of the canonical ``response_item`` lines). We accept both user-turn shapes and ignore
the rest of ``item_completed`` (already covered by ``response_item``). Everything else
in ``event_msg`` (``agent_message`` echoes, ``token_count``) is skipped in this core cut.

Lossy by design for this first cut -- all deferred to later slices: ``usage``
(``token_count`` -> Phase 2, and coarse), ``is_auth_error`` (lives in codex's
``logs_2.sqlite``, never the transcript), subagent linkage, tk step-progress.
``stop_reason`` is left null.

Event ids prefer codex's own stable identity (the assistant message ``id``, or a
tool call's ``call_id``) so the watcher dedups codex 0.144.3's re-serialised
duplicates (the same message written to the rollout more than once by the
"paginated" / world_state persistence). Where codex gives no id (an ``event_msg``
``user_message``), we synthesise one from its timestamp + text (see
``_stable_user_event_id``) rather than the physical line index -- position-independent,
so if a rollout is compressed and re-materialised (repointing the marker and forcing a
re-read from byte 0) the same user bubble dedups instead of duplicating.
"""

from __future__ import annotations

import hashlib
from typing import Any

from imbue.system_interface.harnesses.codex.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.codex.tool_labels import tool_labels
from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.events import MAX_TOOL_OUTPUT_LENGTH
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind

# Kept as ``codex/common_transcript`` to match the ``<harness>/common_transcript``
# label ``claude_session_parser`` stamps -- "common" here means the normalized/common
# event *form*, not the on-disk common-transcript file (which we do NOT read).
# Nothing in the pipeline branches on this string.
SOURCE = "codex/common_transcript"

# Codex rollout messages never carry a per-message model slug, so surface the same
# placeholder ``claude_session_parser`` uses when the model is absent, keeping the
# frontend's non-optional ``model`` field populated.
_UNKNOWN_MODEL = "unknown"

def _join_output_text(content: Any) -> str:
    """Join the ``text`` of ``content`` blocks whose ``type`` is ``output_text``."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "output_text" and block.get("text")
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
            else:
                # other item shapes carry no text
                continue
        text = "".join(parts)
    else:
        text = "" if output is None else str(output)
    if len(text) > MAX_TOOL_OUTPUT_LENGTH:
        return text[:MAX_TOOL_OUTPUT_LENGTH] + "..."
    return text


def _tool_call_raw_input(payload: dict[str, Any]) -> str:
    """The tool call's raw, untruncated input. ``function_call`` carries ``arguments``
    (a JSON string); ``custom_tool_call`` carries ``input`` (raw text, e.g. an
    apply_patch body)."""
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    return "" if raw is None else str(raw)


def _labelled_tool_call(call_id: str, tool_name: str, raw_input: str) -> dict[str, str]:
    """A tool call carrying its own human labels.

    Labelled here, where the harness is known, so the frontend renders a string
    rather than having to understand that a codex ``exec`` hides its real operation
    in a JavaScript argument.

    Labels come from the RAW input, not the truncated preview: the operation often
    lives past the 200-char cap (an apply_patch that front-loads its body into a
    variable, a long exec_command), so labelling the clipped string would read off
    the wrong part -- see the same problem noted in ``codex/tool_labels``. The stored
    ``input_preview`` is still truncated for display, EXCEPT for the tk and patch
    bodies the timeline/diff view need whole (see :func:`_input_preview`).
    """
    header_label, caption_label = tool_labels(tool_name, raw_input)
    return {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "input_preview": _input_preview(tool_name, raw_input),
        "header_label": header_label,
        "caption_label": caption_label,
    }


def _input_preview(tool_name: str, raw_input: str) -> str:
    """The stored ``input_preview``: the raw input, truncated to the shared cap -- but
    left whole for a tk command or a patch body, which the step timeline and the diff
    view render in full (a mid-body cut would truncate the plan or the diff). Matches
    the claude parser's tk exemption; ``keeps_full_tool_input`` owns the recognition."""
    if keeps_full_tool_input(tool_name, raw_input):
        return raw_input
    if len(raw_input) > MAX_TOOL_INPUT_PREVIEW_LENGTH:
        return raw_input[:MAX_TOOL_INPUT_PREVIEW_LENGTH] + "..."
    return raw_input


def _assistant_event(timestamp: str, event_id: str, *, text: str, tool_calls: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "assistant",
        "model": _UNKNOWN_MODEL,
        "text": text,
        "tool_calls": tool_calls,
        # deferred (derive from task_complete later)
        "stop_reason": None,
        # deferred (token_count -> Phase 2)
        "usage": None,
        "message_uuid": event_id,
        # deferred (codex auth errors live in logs_2.sqlite)
        "is_auth_error": False,
    }


def _stable_user_event_id(timestamp: str, content: str) -> str:
    """A content-derived, position-independent event id for a user bubble.

    An ``event_msg`` ``user_message`` carries no codex id, so we synthesise one from
    its ``timestamp`` + text. Unlike the physical line index, this is stable across a
    re-read of the *same* message from a rotated/materialised rollout (codex compresses
    finished rollouts to ``.zst`` then re-expands them, which can repoint the marker and
    force the watcher to re-read from byte 0 -- a line-index id would change and the
    bubble would duplicate). Two genuinely distinct sends never collide: identical text
    at the same millisecond timestamp is not something a human can produce.
    """
    digest = hashlib.sha1(f"{timestamp}\x00{content}".encode("utf-8", "replace")).hexdigest()[:16]
    return f"codex-user-{digest}"


def _item_content_text(content: Any) -> str | None:
    """Join the text of an ``item_completed`` item's ``content`` blocks, or None.

    The new item schema carries the human prompt as ``content: [{type, text}, ...]``.
    We take any block's ``text`` (a user turn is a single text block in practice).
    """
    if not isinstance(content, list):
        return None
    text = "".join(
        block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    return text or None


def _marker_event_id(payload: dict[str, Any], payload_type: str, line_index: int) -> str:
    """The event id for a turn-lifecycle marker, keyed on codex's own ``turn_id``.

    The spine's rule: an ``event_id`` must be the harness's own STABLE id, never a
    physical counter (a counter changes when a rollout is re-materialised from byte 0,
    duplicating the marker, and makes truncation/supersession inexpressible). Codex's
    ``task_started`` / ``task_complete`` / ``turn_aborted`` carry a ``turn_id`` (the
    started/complete pair share one, so the ``payload_type`` suffix keeps them
    distinct). Falls back to the line index only if a marker ever arrives without one.
    """
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return f"codex-turn-{turn_id}-{payload_type}"
    return f"codex-{line_index}-{payload_type}"


def _user_message_events(timestamp: str, text: str | None) -> list[dict[str, Any]]:
    """The single user-bubble event for a human prompt, or ``[]`` when there is no text.

    Both the old ``event_msg`` ``user_message`` and the new ``item_completed``
    ``UserMessage`` forms route here, sharing one content-derived event id (see
    :func:`_stable_user_event_id`) so a rollout that somehow carried both dedups to one
    bubble.
    """
    if not text:
        return []
    event_id = _stable_user_event_id(timestamp, text)
    return [
        {
            "timestamp": timestamp,
            "type": "user_message",
            "event_id": event_id,
            "source": SOURCE,
            "role": "user",
            "content": text,
            "message_uuid": event_id,
        }
    ]


def parse_lines(
    record: dict[str, Any],
    line_index: int,
    tool_name_by_call_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Map one codex rollout line to zero or more UI event dicts (``[]`` to skip).

    Returns a *list* because one rollout line can expand to more than one event.

    ``line_index`` is the stable physical line number (for event-id synthesis).
    ``tool_name_by_call_id`` is a mutable cross-line map so a ``function_call_output``
    can recover its tool name from the earlier ``function_call``.
    """
    outer = record.get("type")
    payload = record.get("payload")
    timestamp = record.get("timestamp", "")
    if not isinstance(payload, dict) or not isinstance(timestamp, str):
        return []
    payload_type = payload.get("type")

    # --- event_msg: the clean human prompt + the turn-abort marker ---
    if outer == "event_msg":
        # The clean human prompt. Older codex emitted it as ``user_message``; newer
        # codex folds every display echo into ``item_completed`` carrying a typed
        # ``item``, so the human turn is now ``item_completed`` with
        # ``item.type == "UserMessage"``. We handle both forms: they do not co-occur
        # in a given codex version, and both derive the same content-based event id,
        # so a transitional rollout that carried both would still dedup to one bubble.
        # (Only the user bubble ever came from this display stream -- assistant text
        # and tool calls are sourced from the canonical ``response_item`` lines, which
        # this codex version left unchanged, so nothing else here needs to move.)
        if payload_type == "user_message":
            text = payload.get("message")
            return _user_message_events(timestamp, text if isinstance(text, str) else None)
        if payload_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                return _user_message_events(timestamp, _item_content_text(item.get("content")))
            # Other item_completed items (AgentMessage, CommandExecution, Reasoning)
            # are display duplicates of the response_item lines we already parse; skip.
            return []
        # A user interrupt aborts the turn. Codex does NOT persist the synthetic
        # aborted tool output, so an in-flight tool call would otherwise stay
        # unmatched forever and pin the activity dot at "Running". Emit a lightweight
        # turn_aborted marker; the activity layer treats it as resolving every
        # still-open tool call (see ``activity_state.pending_tool_call``).
        if payload_type == "turn_aborted":
            event_id = _marker_event_id(payload, "turn_aborted", line_index)
            return [
                {
                    "timestamp": timestamp,
                    "type": SPECIAL_EVENT_TYPE,
                    "kind": SpecialEventKind.TURN_ABORTED.value,
                    "event_id": event_id,
                    "source": SOURCE,
                    "message_uuid": event_id,
                }
            ]
        # Turn-lifecycle markers (task == turn). Codex writes these to the rollout in
        # real time -- ``task_started`` the instant the turn begins, ``task_complete``
        # when it ends -- so the activity layer can bracket "the agent is working"
        # (see ``codex_activity_state.turn_open``). Verified against real
        # rollouts: ``task_complete`` lands just after the final assistant message, so
        # the dot clears only once the text is already on screen.
        if payload_type in ("task_started", "task_complete"):
            kind = (
                SpecialEventKind.TURN_STARTED
                if payload_type == "task_started"
                else SpecialEventKind.TURN_COMPLETED
            )
            event_id = _marker_event_id(payload, payload_type, line_index)
            return [
                {
                    "timestamp": timestamp,
                    "type": SPECIAL_EVENT_TYPE,
                    "kind": kind.value,
                    "event_id": event_id,
                    "source": SOURCE,
                    "message_uuid": event_id,
                }
            ]
        return []

    if outer != "response_item":
        # session_meta, turn_context -> drop
        return []

    # --- response_item: assistant messages + tool calls/results ---
    if payload_type == "message":
        if payload.get("role") == "assistant":
            # codex re-serialises history; each copy shares the message ``id``, so
            # keying the event id on it dedups the copies (fall back to line index).
            msg_id = payload.get("id")
            event_id = f"codex-{msg_id}" if isinstance(msg_id, str) and msg_id else f"codex-{line_index}-assistant"
            return [
                _assistant_event(
                    timestamp,
                    event_id,
                    text=_join_output_text(payload.get("content")),
                    tool_calls=[],
                )
            ]
        # role=user (and developer/system) -> skip; user bubbles come from event_msg.
        return []

    if payload_type in ("function_call", "custom_tool_call"):
        call_id = str(payload.get("call_id", ""))
        tool_name = str(payload.get("name", ""))
        if call_id and tool_name:
            tool_name_by_call_id[call_id] = tool_name
        # Same dedup rationale: a re-serialised tool call keeps its ``call_id``.
        event_id = f"codex-call-{call_id}" if call_id else f"codex-{line_index}-assistant"
        return [
            _assistant_event(
                timestamp,
                event_id,
                text="",
                tool_calls=[_labelled_tool_call(call_id, tool_name, _tool_call_raw_input(payload))],
            )
        ]

    if payload_type in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-result-{call_id}" if call_id else f"codex-{line_index}-tool_result"
        output = _stringify_output(payload.get("output"))
        return [
            {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": SOURCE,
                "tool_call_id": call_id,
                "tool_name": tool_name_by_call_id.get(call_id, ""),
                "output": output,
                # A failed code-mode script writes output starting with "Script failed";
                # flag it so the UI renders the result as an error, not a clean success.
                "is_error": output.startswith("Script failed"),
                "message_uuid": event_id,
            }
        ]

    return []
