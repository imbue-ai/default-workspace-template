"""Parse raw Claude session JSONL files into common transcript events.

Reimplements the conversion logic from mngr_claude's common_transcript.sh
in pure Python. Handles user messages, assistant messages with tool calls,
and tool result events.
"""

from __future__ import annotations

import json
import re
from enum import auto
from typing import Any

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.harnesses.auth_errors import is_auth_error_text
from imbue.system_interface.harnesses.error_patterns import classify_api_error
from imbue.system_interface.harnesses.error_patterns import is_provider_fault
from imbue.system_interface.harnesses.claude.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.claude.tool_labels import shell_command
from imbue.system_interface.harnesses.claude.tool_labels import tool_labels
from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.message_display import stamp_user_message_display
from imbue.system_interface.harnesses.tool_output import classify_tool_call_display
from imbue.system_interface.harnesses.tool_output import find_permission_request
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import truncate_tool_output

logger = _loguru_logger

_SOURCE = "claude/common_transcript"

_AGENT_ID_PATTERN = re.compile(r"agentId:\s*(\S+)")

# Sentinel text Claude writes to the user channel when the user interrupts a
# turn (e.g. presses Esc mid-tool-use). It is a control marker, not real user
# input -- emitting it as a ``user_message`` event would pin the activity
# indicator on "Thinking..." after every interrupt, since the transcript-tail
# heuristic would treat it as "user just spoke, Claude hasn't replied yet."
INTERRUPT_SENTINEL_TEXT = "[Request interrupted by user]"

# The interrupt sentinel has TWO shapes on disk: the plain streaming-abort form above,
# and this mid-tool form Claude writes when the interrupt lands while a tool is running
# (the dominant stop scenario). The plain constant is NOT a substring of it, so both are
# recognized by anchoring on the shared opening rather than the plain string alone.
MID_TOOL_INTERRUPT_SENTINEL_TEXT = "[Request interrupted by user for tool use]"
_INTERRUPT_SENTINEL_PREFIX = "[Request interrupted by user"


def is_interrupt_sentinel_text(text: str) -> bool:
    """True iff ``text`` is one of Claude's interrupt sentinels (either shape).

    Prefix-anchored on the shared opening so both ``[Request interrupted by user]`` and the
    mid-tool ``[Request interrupted by user for tool use]`` variant are matched. Callers pass
    the PARSED text of a user record (not a raw line), so a ``tool_result`` merely quoting the
    sentinel -- whose text is not extracted as user text -- can never be mistaken for one.
    """
    return text.strip().startswith(_INTERRUPT_SENTINEL_PREFIX)


# Claude Code's resume bookkeeping. Whenever ``claude --resume`` reloads a
# session whose previous turn did not finish cleanly (the turn was interrupted,
# or the process was stopped or crashed mid-turn), the framework injects a
# synthetic turn-pair to close the dangling turn: an ``isMeta`` user message
# ("Continue from where you left off."), answered by a synthetic-model assistant
# message (see ``_SYNTHETIC_MODEL``). This pair is inert -- Claude Code's own UI
# hides both, and the agent never acts on it. The user half is now hidden by the
# general ``is_meta`` path (it is an ``isMeta`` message like any other framework
# injection), so it needs no dedicated matcher here; only the assistant half
# (below) still does, because it is not marked ``isMeta``.

# Model value Claude Code stamps on assistant messages the framework generates
# itself, as opposed to real model output. Note this model is NOT unique to the
# resume turn-pair's reply: Claude Code also stamps it on API-error and auth
# (e.g. "API Error: 529 Overloaded", "Please run /login") notices, which the
# user does need to see. So the synthetic model alone is not enough to hide a
# message -- the text must also match (see ``_is_resume_no_response_reply``).
_SYNTHETIC_MODEL = "<synthetic>"

# Exact text of the synthetic assistant message that answers the resume
# continuation marker. The resume turn-pair is "Continue from where you left
# off." -> "No response requested."; this is the reply half.
_NO_RESPONSE_REQUESTED_TEXT = "No response requested."

# Claude Code records a message the user typed while the agent was busy (a
# "queued" message) not as a normal ``user`` line but as an ``attachment`` event
# of this type. Its ``commandMode`` distinguishes the verbatim user prompt
# (``prompt``) from background-task completion notices (``task-notification``),
# which are framework-generated and not user turns. Without parsing the
# ``prompt`` form, a queued user message yields no ``user_message`` event at all:
# it never appears as a user bubble, and the frontend's optimistic "Queued"
# bubble never reconciles -- so it stays up even after the agent has received and
# answered the message. (Empirically a queued message is recorded EITHER as this
# attachment OR, on older Claude Code versions, as a plain ``user`` line, never
# both, so parsing it here does not double-render.)
_QUEUED_COMMAND_ATTACHMENT_TYPE = "queued_command"
_QUEUED_COMMAND_PROMPT_MODE = "prompt"

# A slash command the user types (``/foo bar``) is not recorded verbatim: Claude
# Code expands it into an XML-ish block carrying the command name, a display
# message, and the trailing arguments, e.g.
#     <command-message>foo</command-message>
#     <command-name>/foo</command-name>
#     <command-args>bar</command-args>
# The three tags appear in varying order (built-ins lead with <command-name>,
# custom commands with <command-message>), so they are matched individually
# rather than positionally. We rebuild the original ``/foo bar`` text so the
# rendered user bubble shows what the user actually typed instead of the raw
# expansion. (The frontend's optimistic "Sending…" bubble is removed positionally,
# oldest-first, as the real user turn appears -- it does not depend on this text;
# see OutgoingMessages.ts.)
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _normalize_slash_command(text: str) -> str:
    """Rebuild ``/name args`` from a Claude Code slash-command expansion.

    Returns ``text`` unchanged when it is not a command expansion (no
    ``<command-name>`` tag, or an empty command name).
    """
    name_match = _COMMAND_NAME_PATTERN.search(text)
    if name_match is None:
        return text
    command = name_match.group(1).strip()
    if not command:
        return text
    args_match = _COMMAND_ARGS_PATTERN.search(text)
    args = args_match.group(1).strip() if args_match is not None else ""
    return f"{command} {args}".strip()


def extract_text_content(content: str | list[dict[str, Any]] | Any) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: str | list[Any] | Any) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type not in ("tool_result",):
                return False
        elif isinstance(block, str):
            return False
    return True


def _extract_subagent_id(structured_agent_id: str | None, result_content: str) -> str | None:
    """Resolve the subagent id for an Agent tool_result.

    Prefers the structured toolUseResult.agentId field, falling back to the
    `agentId: <id>` text trailer in the tool result content. Newer Claude Code
    versions may emit only the structured field; older versions or nested
    subagents may emit only the trailer.
    """
    if structured_agent_id:
        return structured_agent_id
    if not result_content:
        return None
    agent_id_match = _AGENT_ID_PATTERN.search(result_content)
    if agent_id_match:
        return agent_id_match.group(1)
    return None


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _is_resume_no_response_reply(message: dict[str, Any]) -> bool:
    """True if ``message`` is the synthetic reply half of the resume turn-pair.

    The reply is an assistant message that is BOTH stamped with the synthetic
    model AND has exactly the no-response text. Both conditions are required:
    the synthetic model alone also covers API-error and auth notices the user
    must see, and the text alone could be a real agent turn that happens to say
    those words. Only their conjunction is the inert bookkeeping reply, which
    the chat transcript view hides to match Claude Code's own UI.
    """
    if message.get("model") != _SYNTHETIC_MODEL:
        return False
    return extract_text_content(message.get("content")).strip() == _NO_RESPONSE_REQUESTED_TEXT


def parse_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    tool_name_by_call_id: dict[str, str] | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse raw Claude session JSONL lines into common transcript events.

    Args:
        lines: Raw JSONL lines from a Claude session file.
        existing_event_ids: Set of event IDs already emitted, for deduplication.
            If None, no deduplication is performed.
        tool_name_by_call_id: Mutable mapping from tool_use_id to tool_name,
            carried across calls for cross-message tool name resolution.
            If None, a fresh dict is used.
        session_id: Identifier for the session file these lines came from.
            If provided, each event will include a "session_id" field.

    Returns:
        List of common transcript event dicts, sorted by timestamp.
    """
    if existing_event_ids is None:
        existing_event_ids = set()
    if tool_name_by_call_id is None:
        tool_name_by_call_id = {}

    new_events: list[tuple[str, dict[str, Any]]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            logger.debug("Skipping malformed JSONL line: {}", e)
            continue

        event_type: str = raw.get("type", "")
        uuid: str = raw.get("uuid", "")
        timestamp: str = raw.get("timestamp", "")

        if not uuid or not timestamp:
            continue

        if event_type == "assistant":
            _parse_assistant_message(
                raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, session_id
            )
        elif event_type == "user":
            _parse_user_message(raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, session_id)
        elif event_type == "attachment":
            _parse_queued_command_attachment(raw, uuid, timestamp, existing_event_ids, new_events, session_id)
        # Skip: progress, file-history-snapshot, system, result, etc.

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]


def _parse_assistant_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    event_id = _make_event_id(uuid, "assistant")
    if event_id in existing_event_ids:
        return

    # ``raw.get("message", {})`` returns ``None`` for a present-but-null key (the
    # default only applies to a *missing* key), and Claude Code does write lines
    # with ``"message": null``. Without this guard the ``.get`` calls below raise
    # AttributeError, which kills the watcher thread and wedges the read path (the
    # byte offset never advances, so every poll re-reads and re-crashes the same
    # line forever). A null message carries no usable content -> drop the line.
    message = raw.get("message")
    if not isinstance(message, dict):
        return

    # Drop Claude Code's resume bookkeeping -- its own UI hides it, so do we.
    if _is_resume_no_response_reply(message):
        return

    content_blocks: list[Any] = message.get("content", [])
    model: str = message.get("model", "unknown")
    stop_reason: str | None = message.get("stop_reason")
    usage_raw: dict[str, Any] = message.get("usage", {})

    text_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id: str = block.get("id", "")
            tool_name: str = block.get("name", "")
            tool_input = block.get("input", {})
            raw_input = json.dumps(tool_input, separators=(",", ":"))
            input_preview = raw_input
            if len(input_preview) > MAX_TOOL_INPUT_PREVIEW_LENGTH and not keeps_full_tool_input(tool_name, raw_input):
                input_preview = input_preview[:MAX_TOOL_INPUT_PREVIEW_LENGTH] + "..."
            command = shell_command(tool_name, raw_input)
            is_hidden_tk = command is not None and is_pure_tk_lifecycle_command(command)

            if call_id and tool_name:
                tool_name_by_call_id[call_id] = tool_name

            # Labelled here, where the harness is known, so the frontend renders a
            # string rather than deciding what a claude tool call should read as.
            header_label, caption_label = tool_labels(tool_name, input_preview)
            tool_call: dict[str, str] = {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": input_preview,
                "header_label": header_label,
                "caption_label": caption_label,
            }
            # The render decision ships with the call (a hidden tk marker, or the
            # permission card), recognised from the UNTRUNCATED input backend-side; the
            # frontend never re-derives it from the command text.
            display = classify_tool_call_display(is_pure_tk=is_hidden_tk, raw_input=raw_input)
            if display is not None:
                tool_call["display"] = display.value
            # For Agent tool calls, surface the description and subagent_type from the
            # tool input directly. These let the frontend render the rich subagent card
            # (label + agent-type badge) the instant the call appears, before the subagent
            # is linked to its session.
            if tool_name == "Agent" and isinstance(tool_input, dict):
                description = tool_input.get("description")
                subagent_type = tool_input.get("subagent_type")
                if isinstance(description, str) and description:
                    tool_call["description"] = description
                if isinstance(subagent_type, str) and subagent_type:
                    tool_call["subagent_type"] = subagent_type

            tool_calls.append(tool_call)

    usage: dict[str, Any] | None = None
    if usage_raw:
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
        }

    joined_text = "\n".join(text_parts)
    # A model API error surfaces as a synthetic assistant message (e.g. "API Error:
    # 529 Overloaded"). Classify it so the frontend can style it as an error and, for a
    # provider-side failure (5xx / overloaded), add a "not Minds' fault" note. Gated on
    # the synthetic model: only Claude Code's own framework-generated notices carry these
    # forms, so a REAL assistant message that merely quotes "API Error: 500" or an error
    # JSON (routine in a coding chat) is not mistaken for an outage. Auth failures are
    # flagged separately (is_auth_error) and are not reclassified here.
    api_error_kind = classify_api_error(joined_text) if model == _SYNTHETIC_MODEL else None
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": model,
        "text": joined_text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": usage,
        "message_uuid": uuid,
        "is_auth_error": is_auth_error_text(joined_text),
        "is_api_error": api_error_kind is not None,
        "api_error_kind": api_error_kind,
        "is_provider_fault": is_provider_fault(api_error_kind),
    }
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))


def _parse_user_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    # See ``_parse_assistant_message``: a present-but-null ``message`` must be
    # dropped, not crashed on (AttributeError here kills the watcher thread and
    # wedges the read path).
    message = raw.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")

    tool_use_result = raw.get("toolUseResult")
    structured_agent_id: str | None = None
    if isinstance(tool_use_result, dict):
        agent_id_value = tool_use_result.get("agentId")
        if isinstance(agent_id_value, str) and agent_id_value:
            structured_agent_id = agent_id_value

    # Emit user text message if there is actual user text
    if not _has_tool_results_only(content):
        event_id = _make_event_id(uuid, "user")
        if event_id not in existing_event_ids:
            text = _normalize_slash_command(extract_text_content(content))
            if text and not is_interrupt_sentinel_text(text):
                event: dict[str, Any] = {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "user",
                    "content": text,
                    "message_uuid": uuid,
                }
                # Claude Code's own markers (``isMeta`` for framework-injected,
                # model-only messages; ``isCompactSummary`` for the post-compaction
                # summary record) are read HERE and become the shared render decision
                # -- the raw flags never cross the wire. Explicit detectors win over
                # isMeta (Stop-hook feedback deliberately surfaces as a chip). (The
                # interrupt sentinel above is NOT isMeta, so it keeps its own guard.)
                stamp_user_message_display(
                    event,
                    text,
                    is_meta=bool(raw.get("isMeta")),
                    is_compact_summary=bool(raw.get("isCompactSummary")),
                )
                if session_id is not None:
                    event["session_id"] = session_id
                existing_event_ids.add(event_id)
                new_events.append((timestamp, event))

    # Emit tool result events for any tool_result blocks
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_call_id: str = block.get("tool_use_id", "")
            if not tool_call_id:
                continue

            event_id = _make_event_id(uuid, f"tool_result-{tool_call_id}")
            if event_id in existing_event_ids:
                continue

            # Extract output text
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts: list[str] = []
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                result_content = "\n".join(parts)
            elif not isinstance(result_content, str):
                result_content = str(result_content)

            tool_name = tool_name_by_call_id.get(tool_call_id, "unknown")

            # Extract subagent ID BEFORE truncation (the trailer may be at the end).
            extracted_subagent_id: str | None = None
            if tool_name == "Agent":
                extracted_subagent_id = _extract_subagent_id(structured_agent_id, result_content)

            # Likewise BEFORE truncation: a permission-request response is
            # routinely longer than the output limit, so the object is located
            # and parsed while it is still intact. The card then reads the
            # request off the event instead of re-parsing a string that
            # truncation may have cut mid-object.
            permission_request = find_permission_request(result_content)

            result_content = truncate_tool_output(result_content, permission_request)

            event = {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": _SOURCE,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "output": result_content,
                "is_error": bool(block.get("is_error", False)),
                "message_uuid": uuid,
            }
            if session_id is not None:
                event["session_id"] = session_id

            if extracted_subagent_id:
                event["subagent_id"] = extracted_subagent_id
            if permission_request is not None:
                event["permission_request"] = permission_request.details

            existing_event_ids.add(event_id)
            new_events.append((timestamp, event))


def _parse_queued_command_attachment(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    """Emit a ``user_message`` event for a message the user queued while busy.

    Claude Code writes such a message as a ``queued_command`` attachment (see
    ``_QUEUED_COMMAND_ATTACHMENT_TYPE``) rather than a normal ``user`` line.
    Only the ``prompt`` command mode carries verbatim user text; the
    ``task-notification`` mode is a framework-generated background-task notice
    and is left unparsed (it is not a user turn).
    """
    attachment = raw.get("attachment")
    if not isinstance(attachment, dict):
        return
    if attachment.get("type") != _QUEUED_COMMAND_ATTACHMENT_TYPE:
        return
    if attachment.get("commandMode") != _QUEUED_COMMAND_PROMPT_MODE:
        return
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    # A queued message can itself be a slash command; normalize it the same way
    # a non-queued one is handled in ``_parse_user_message`` so it renders as the
    # typed text and reconciles against its optimistic bubble.
    prompt = _normalize_slash_command(prompt)

    event_id = _make_event_id(uuid, "queued")
    if event_id in existing_event_ids:
        return

    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "user",
        "content": prompt,
        "message_uuid": uuid,
    }
    # The queued path emits user messages too, so it stamps the same render decision as
    # the normal path -- a /model parked mid-turn must stay hidden (and non-turn), a fleet
    # nudge must chip, and a latchkey verdict must resolve its card, exactly as if typed.
    stamp_user_message_display(event, prompt)
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))


# Queued-message ledger parsing (conservation-law model). Claude Code records the
# live queue as out-of-band ``queue-operation`` records that carry no ``uuid`` and
# so are dropped by ``parse_lines`` at the DAG guard. They obey a conservation
# law: ``enqueue = dequeue + remove + popAll`` -- every parked message leaves the
# queue through exactly one dequeue/remove/popAll record. In the real Minds flow
# EVERY message is delivered via mngr (typed into the TUI), so a mid-turn message
# commits as a ``dequeue`` whose ``promptSource`` is "typed" (NOT "queued"), and
# slash commands / task-notifications also leave via dequeue/remove -- none of
# which the user record or the ``queued_command`` attachment reliably marks. So
# resolution keys off the ledger's LEAVE ops ONLY (one record = one leave), never
# ``promptSource`` or the attachment. ``parse_queue_signals`` therefore recognizes
# ONLY the four ``queue-operation`` ops. The ``queued_command`` attachment and the
# ``promptSource:"queued"`` user record are still parsed by ``parse_lines`` for the
# TRANSCRIPT render -- that is unrelated and unchanged.

_QUEUE_OPERATION_TYPE = "queue-operation"
_ENQUEUE_OPERATION = "enqueue"
# The three ops through which a parked message leaves the queue. Each such record
# is one leave -- popAll emits one record per flushed message, so a per-record pop
# is correct and uniform (no special "clear all").
_LEAVE_OPERATIONS = frozenset({"dequeue", "remove", "popAll"})
# Background-task completion notices ride the same queue as user messages, but
# they are framework-generated and must never surface. An enqueue whose content
# starts with this marker (or is blank) is added as a PHANTOM slot: it occupies a
# FIFO position so leaves stay aligned, but it is filtered from the snapshot.
TASK_NOTIFICATION_CONTENT_PREFIX = "<task-notification>"


class QueueSignalKind(UpperCaseStrEnum):
    """The queue-ledger transitions the tracker acts on.

    Exactly the two the conservation law needs: a message entering the queue, and
    a message leaving it (via any of dequeue / remove / popAll).
    """

    # A message was parked in the queue (``queue-operation/enqueue``).
    ENQUEUE = auto()
    # A parked message left the queue (``queue-operation`` dequeue / remove / popAll).
    LEAVE = auto()


class QueueSignal(FrozenModel):
    """One recognized queue-ledger transition from a raw session line."""

    kind: QueueSignalKind = Field(description="Whether this line parked a message or drained one")
    session_id: str = Field(description="The record's session id; a change means a new session file")
    # Carried only for ENQUEUE (the enqueue timestamp/content, used to mint the id
    # and decide phantom-ness). Empty for LEAVE.
    timestamp: str = Field(default="", description="Enqueue timestamp; empty for a LEAVE")
    content: str = Field(default="", description="Enqueued message text; empty for a LEAVE")


def _record_session_id(raw: dict[str, Any]) -> str:
    """The session id on a raw record (``sessionId`` preferred, ``session_id`` fallback)."""
    session_id = raw.get("sessionId") or raw.get("session_id")
    return session_id if isinstance(session_id, str) else ""


def parse_queue_signals(line: str) -> QueueSignal | None:
    """Recognize a single raw session line as a queue-ledger transition, or None.

    Returns an ENQUEUE for a ``queue-operation/enqueue``, a LEAVE for a
    ``queue-operation`` dequeue / remove / popAll, and ``None`` for every other
    line (including all ``user`` records and ``attachment`` records -- those are
    handled by the transcript parse, not the queue tracker).
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        # A corrupt line in the session JSONL stream: fall back to "no signal" but
        # surface it (a genuinely malformed complete line means on-disk corruption
        # worth noticing, not routine input).
        logger.warning("Skipping non-JSON line for queue signals: {}", e)
        return None
    if not isinstance(raw, dict):
        return None

    if raw.get("type") != _QUEUE_OPERATION_TYPE:
        return None
    operation = raw.get("operation")
    if operation == _ENQUEUE_OPERATION:
        content = raw.get("content", "")
        return QueueSignal(
            kind=QueueSignalKind.ENQUEUE,
            session_id=_record_session_id(raw),
            timestamp=raw.get("timestamp", ""),
            content=content if isinstance(content, str) else "",
        )
    if operation in _LEAVE_OPERATIONS:
        return QueueSignal(kind=QueueSignalKind.LEAVE, session_id=_record_session_id(raw))
    return None
