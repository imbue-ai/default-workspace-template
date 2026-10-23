"""Turn the records of a grok session's ``updates.jsonl`` into the shared web-UI event schema.

Grok Build appends each session's log to ``<GROK_HOME>/sessions/<bucket>/<session id>/updates.jsonl``,
one ``{"timestamp", "method", "params": {"sessionId", "update": {"sessionUpdate": <kind>, ...},
"_meta": {...}}}`` record per line (``docs/design/grok-transcript-schema.md``). Its loader keeps a
:class:`GrokLaneState` per session and hands each record to :func:`parse_record` with its byte
offset; :func:`detail_for` re-reads an event's payloads from the record it came from.

  user_message_chunk     a user turn (chunks sharing a ``promptIndex`` are one message); a wake
                         turn grok started itself is a notice
  agent_message_chunk    a run of assistant text (lines only background reports separate are one run)
  agent_thought_chunk    readable reasoning, carried by the lane's next assistant event
  tool_call              a tool call
  tool_call_update       the call's result, once its status is completed or failed
  turn_completed         the turn's end: end_turn, cancelled or interrupted (open calls read
                         "Interrupted.") or error (its ``agent_result`` becomes an error message)
  subagent_spawned       the child session a spawn_subagent call started
  task_completed,        background work that finished
  subagent_finished
  hook_annotation        a Stop hook sending grok back to work
  anything else          nothing

Event ids are ``grok-<session id>-<byte offset>-<suffix>``: grok's ``eventId`` restarts with each
launch and a ``toolCallId`` can repeat within a session. A result's id carries its call's offset. A
root session's turns emit turn_started and their end; a child session's emit only their end.
"""

import json
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Any
from typing import Final

from pydantic import Field

from imbue.chat.harnesses.auth_errors import is_auth_error_text
from imbue.chat.harnesses.error_patterns import classify_api_error
from imbue.chat.harnesses.error_patterns import is_provider_fault
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.grok.tool_labels import shell_command
from imbue.chat.harnesses.grok.tool_labels import tool_labels
from imbue.chat.harnesses.message_display import classify_user_message
from imbue.chat.harnesses.message_display import stamp_user_message_display
from imbue.chat.harnesses.tool_output import classify_tool_call_display
from imbue.chat.harnesses.tool_output import error_snippet
from imbue.chat.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.chat.harnesses.tool_output import is_tk_lifecycle_anywhere
from imbue.chat.harnesses.tool_output import stamp_echoed_requests
from imbue.chat.harnesses.tool_output import tk_stamp
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

SOURCE: Final[str] = "grok/common_transcript"

# The `type` of the records parse_record returns for the loader alone; none goes on the wire.
# A thought line: the byte span the loader attaches as the thinking source of the lane's next
# event stamped `has_thinking`.
THINKING_SOURCE_MARKER_TYPE: Final[str] = "_grok_thinking_source"
# A child session to tail, with the `call_event_id` of the spawn_subagent call it belongs to
# (None when none matched) and the `subagent_metadata` that call and the session carry.
SUBAGENT_SPAWNED_MARKER_TYPE: Final[str] = "_grok_subagent_spawned"
# Background work (a shell, monitor or subagent) the transcript reports over, by `task_id`, with
# `will_wake`: whether grok starts a turn of the session for it once the session is idle.
TASK_FINISHED_MARKER_TYPE: Final[str] = "_grok_task_finished"
INTERNAL_MARKER_TYPES: Final[frozenset[str]] = frozenset(
    {THINKING_SOURCE_MARKER_TYPE, SUBAGENT_SPAWNED_MARKER_TYPE, TASK_FINISHED_MARKER_TYPE}
)

_UNKNOWN_MODEL: Final[str] = "unknown"
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed"})
_FAILED_STATUS: Final[str] = "failed"
_SPAWN_SUBAGENT_TOOL_NAME: Final[str] = "spawn_subagent"
_INTERRUPTED_SNIPPET: Final[str] = "Interrupted."
_UNSPECIFIED_TURN_ERROR: Final[str] = "The turn ended with an error."
_SEND_NOW_PREFIX: Final[str] = "The user sent a message while you were working:"
_USER_QUERY_OPEN: Final[str] = "<user_query>"
_USER_QUERY_CLOSE: Final[str] = "</user_query>"
_SHELL_OUTPUT_TYPE: Final[str] = "Bash"
# rawOutput fields holding the whole text grok gave the model: a shell's `exit: <code>` header
# and output, a grep's matches (its content block is only a match count). Either may be a string
# or the raw bytes as a list of ints.
_MODEL_TEXT_KEYS: Final[tuple[str, ...]] = ("output_for_prompt", "stdout")
# Where a result variant (a PascalCase rawOutput key, e.g. `EditsApplied`) keeps its text.
_VARIANT_TEXT_KEYS: Final[tuple[str, ...]] = ("tool_output_for_prompt", "content", "output", "message")
# Records grok writes when background work reports in, which can land while a run of assistant
# text streams and split the run's line in two.
_BACKGROUND_REPORT_KINDS: Final[frozenset[str]] = frozenset(
    {"task_completed", "subagent_finished", "background_tasks"}
)


class GrokStopReason(LowerCaseStrEnum):
    """Why grok ended a turn, as ``turn_completed.stop_reason`` spells it."""

    END_TURN = auto()
    CANCELLED = auto()
    ERROR = auto()
    # Written at a resume for the turn a grok that exited or was restarted left open.
    INTERRUPTED = auto()


class GrokOpenCall(FrozenModel):
    """A tool call of the lane's current turn, keyed in the lane by grok's ``toolCallId``."""

    offset: int = Field(description="Byte offset of the call's tool_call record, which its event ids carry")
    tool_name: str = Field(description="Grok's name for the tool")
    description: str | None = Field(description="The call's `description` argument")
    is_resolved: bool = Field(description="Whether the call's result was emitted")
    is_subagent_linked: bool = Field(description="Whether a spawned child session was linked to the call")


class GrokTextRun(FrozenModel):
    """A run of assistant text, which grok can write as several consecutive lines."""

    offset: int = Field(description="Byte offset of the run's first line, which its event id carries")
    timestamp: str = Field(description="Timestamp of the run's first line")
    text: str = Field(description="The run's text so far")
    has_thinking: bool = Field(description="Whether a thought line preceded the run")


class GrokLaneState(MutableModel):
    """What parsing one grok session carries from record to record; its loader keeps one per session."""

    session_id: str = Field(description="The session whose records the lane parses")
    is_root: bool = Field(description="Whether the session is top-level, so its turns are the chat's turns")
    model_id: str | None = Field(default=None, description="The model the latest user turn ran on")
    open_prompt_index: int | None = Field(default=None, description="The `promptIndex` of the open user turn")
    open_user_offset: int | None = Field(default=None, description="Offset of the open user turn's first chunk")
    open_user_timestamp: str = Field(default="", description="Timestamp of the open user turn's first chunk")
    open_user_texts: list[str] = Field(default_factory=list, description="The open user turn's chunk texts")
    is_open_user_wake: bool = Field(default=False, description="Whether grok started the open user turn itself")
    calls_by_tool_call_id: dict[str, GrokOpenCall] = Field(
        default_factory=dict, description="The current turn's calls by grok's `toolCallId`"
    )
    has_pending_thought: bool = Field(
        default=False, description="Whether a thought line waits for the lane's next assistant event"
    )
    open_text_run: GrokTextRun | None = Field(
        default=None, description="The run of assistant text a further text line extends"
    )


@pure
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@pure
def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@pure
def _update_of(record: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_as_dict(record.get("params")).get("update"))


@pure
def record_timestamp(record: dict[str, Any]) -> str:
    """The record's time as ISO 8601 UTC with milliseconds (``2026-10-20T01:14:10.029Z``), or '' when it has none."""
    epoch_ms = _as_dict(_as_dict(record.get("params")).get("_meta")).get("agentTimestampMs")
    if not isinstance(epoch_ms, int) or isinstance(epoch_ms, bool):
        epoch_seconds = record.get("timestamp")
        if not isinstance(epoch_seconds, int) or isinstance(epoch_seconds, bool):
            return ""
        epoch_ms = epoch_seconds * 1000
    whole_seconds = datetime.fromtimestamp(epoch_ms // 1000, tz=timezone.utc)
    return f"{whole_seconds.strftime('%Y-%m-%dT%H:%M:%S')}.{epoch_ms % 1000:03d}Z"


@pure
def unwrap_send_now(text: str) -> str:
    """The user's own words from grok's send-now wrapper, or ``text`` unchanged."""
    stripped = text.strip()
    if not stripped.startswith(_SEND_NOW_PREFIX):
        return text
    rest = stripped[len(_SEND_NOW_PREFIX) :].lstrip()
    end = rest.find(_USER_QUERY_CLOSE)
    if not rest.startswith(_USER_QUERY_OPEN) or end < 0:
        return text
    return rest[len(_USER_QUERY_OPEN) : end].strip("\n")


@pure
def _raw_input_json(update: dict[str, Any]) -> str:
    """The call's ``rawInput`` as JSON: what the labels read and the detail endpoint serves."""
    raw_input = update.get("rawInput")
    return json.dumps(raw_input if raw_input is not None else {}, ensure_ascii=False)


@pure
def _as_output_text(value: Any) -> str | None:
    """A rawOutput text field as a string, decoding one grok wrote as a list of byte values."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(byte, int) and 0 <= byte < 256 for byte in value):
        return bytes(value).decode("utf-8", errors="replace")
    return None


@pure
def tool_result_text(update: dict[str, Any]) -> str:
    """The whole text a finished call gave the model, from its terminal ``tool_call_update``.

    The model-facing rawOutput field where grok keeps one, else the update's content blocks,
    else the text of the rawOutput variant (a listing, an edit's confirmation, an error).
    """
    raw_output = _as_dict(update.get("rawOutput"))
    for key in _MODEL_TEXT_KEYS:
        model_text = _as_output_text(raw_output.get(key))
        if model_text is not None:
            return model_text
    content_texts = [
        _as_dict(block.get("content")).get("text")
        for block in update.get("content") or ()
        if isinstance(block, dict) and block.get("type") == "content"
    ]
    content_text = "\n".join(text for text in content_texts if isinstance(text, str))
    if content_text:
        return content_text
    for key, value in raw_output.items():
        if not key[:1].isupper():
            continue
        if isinstance(value, str):
            return value
        variant = _as_dict(value)
        for text_key in _VARIANT_TEXT_KEYS:
            variant_text = _as_output_text(variant.get(text_key))
            if variant_text:
                return variant_text
    return ""


@pure
def _is_failed_shell_exit(update: dict[str, Any]) -> bool:
    """Whether a shell call that ran to its end exited non-zero, which grok still marks completed."""
    raw_output = _as_dict(update.get("rawOutput"))
    exit_code = raw_output.get("exit_code")
    is_exit_code = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    return raw_output.get("type") == _SHELL_OUTPUT_TYPE and is_exit_code and exit_code != 0


@pure
def _event_id(session_id: str, offset: int, suffix: str) -> str:
    return f"grok-{session_id}-{offset}-{suffix}"


def _base_event(event_type: str, event_id: str, timestamp: str, lane: GrokLaneState) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": event_type,
        "event_id": event_id,
        "source": SOURCE,
        "message_uuid": event_id,
        "session_id": lane.session_id,
    }


def _assistant_event(
    lane: GrokLaneState, event_id: str, timestamp: str, text: str, tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    event = _base_event("assistant_message", event_id, timestamp, lane)
    event.update(
        {
            "role": "assistant",
            "model": lane.model_id or _UNKNOWN_MODEL,
            "text": text,
            "tool_calls": tool_calls,
            "stop_reason": None,
            "usage": None,
            "is_auth_error": False,
            "is_api_error": False,
            "api_error_kind": None,
            "is_provider_fault": False,
        }
    )
    if lane.has_pending_thought:
        event["has_thinking"] = True
        lane.has_pending_thought = False
    return event


def _special_event(
    lane: GrokLaneState, kind: SpecialEventKind, offset: int, timestamp: str, prompt_id: str | None
) -> dict[str, Any]:
    event_id = _event_id(lane.session_id, offset, kind.value.replace("_", "-"))
    event = _base_event(SPECIAL_EVENT_TYPE, event_id, timestamp, lane)
    event["kind"] = kind.value
    event["turn_id"] = prompt_id
    return event


def _on_user_message_chunk(
    update: dict[str, Any], lane: GrokLaneState, offset: int, timestamp: str
) -> list[dict[str, Any]]:
    chunk_meta = _as_dict(update.get("_meta"))
    model_id = _as_text(chunk_meta.get("modelId"))
    if model_id is not None:
        lane.model_id = model_id
    prompt_index = chunk_meta.get("promptIndex")
    events: list[dict[str, Any]] = []
    user_offset = lane.open_user_offset
    if user_offset is None or prompt_index != lane.open_prompt_index:
        user_offset = offset
        lane.open_prompt_index = prompt_index if isinstance(prompt_index, int) else None
        lane.open_user_offset = user_offset
        lane.open_user_timestamp = timestamp
        lane.open_user_texts = []
        is_hidden = chunk_meta.get("hideFromScrollback") is True
        lane.is_open_user_wake = is_hidden or chunk_meta.get("syntheticPrompt") is True
        if lane.is_root:
            events.append(_special_event(lane, SpecialEventKind.TURN_STARTED, offset, timestamp, None))
    text = _as_text(_as_dict(update.get("content")).get("text"))
    if text is None:
        return events
    lane.open_user_texts.append(text)
    content = unwrap_send_now("".join(lane.open_user_texts))
    user_event_id = _event_id(lane.session_id, user_offset, "user")
    event = _base_event("user_message", user_event_id, lane.open_user_timestamp, lane)
    event["role"] = "user"
    event["content"] = content
    stamp_user_message_display(event, content, is_wake=lane.is_open_user_wake)
    events.append(event)
    return events


def _on_agent_message_chunk(
    update: dict[str, Any], lane: GrokLaneState, offset: int, timestamp: str
) -> list[dict[str, Any]]:
    """The run of assistant text the line starts, or the open run grown in place by it."""
    text = _as_text(_as_dict(update.get("content")).get("text"))
    run = lane.open_text_run
    if text is None or (run is None and not text.strip()):
        return []
    if run is None:
        event = _assistant_event(lane, _event_id(lane.session_id, offset, "text"), timestamp, text, [])
        lane.open_text_run = GrokTextRun(
            offset=offset, timestamp=timestamp, text=text, has_thinking=event.get("has_thinking", False)
        )
        return [event]
    run = run.model_copy_update(to_update(run.field_ref().text, run.text + text))
    lane.open_text_run = run
    event = _assistant_event(lane, _event_id(lane.session_id, run.offset, "text"), run.timestamp, run.text, [])
    if run.has_thinking:
        event["has_thinking"] = True
    return [event]


def _on_agent_thought_chunk(update: dict[str, Any], lane: GrokLaneState) -> list[dict[str, Any]]:
    text = _as_text(_as_dict(update.get("content")).get("text"))
    if text is None or not text.strip():
        return []
    lane.has_pending_thought = True
    return [{"type": THINKING_SOURCE_MARKER_TYPE}]


def _on_tool_call(update: dict[str, Any], lane: GrokLaneState, offset: int, timestamp: str) -> list[dict[str, Any]]:
    tool_call_id = _as_text(update.get("toolCallId"))
    if tool_call_id is None or tool_call_id in lane.calls_by_tool_call_id:
        return []
    tool_meta = _as_dict(_as_dict(update.get("_meta")).get("x.ai/tool"))
    tool_name = _as_text(tool_meta.get("name")) or _as_text(update.get("title")) or ""
    raw_input = _raw_input_json(update)
    call_event_id = _event_id(lane.session_id, offset, "call")
    header_label, caption_label = tool_labels(tool_name, raw_input, _as_text(tool_meta.get("label")) or "")
    tool_call: dict[str, Any] = {
        "tool_call_id": call_event_id,
        "tool_name": tool_name,
        "input_chars": len(raw_input),
        "header_label": header_label,
        "caption_label": caption_label,
    }
    command = shell_command(tool_name, raw_input)
    is_pure_tk = command is not None and is_pure_tk_lifecycle_command(command)
    display = classify_tool_call_display(is_pure_tk=is_pure_tk, raw_input=raw_input)
    if display is not None:
        tool_call["display"] = display.value
    if command is not None and is_tk_lifecycle_anywhere(command):
        tool_call["tk_command"] = command
    description = _as_text(_as_dict(update.get("rawInput")).get("description"))
    if tool_name == _SPAWN_SUBAGENT_TOOL_NAME and description is not None:
        tool_call["description"] = description
    lane.calls_by_tool_call_id[tool_call_id] = GrokOpenCall(
        offset=offset, tool_name=tool_name, description=description, is_resolved=False, is_subagent_linked=False
    )
    return [_assistant_event(lane, call_event_id, timestamp, "", [tool_call])]


def _tool_result_event(
    lane: GrokLaneState, call: GrokOpenCall, timestamp: str, output_chars: int, is_error: bool
) -> dict[str, Any]:
    event = _base_event("tool_result", _event_id(lane.session_id, call.offset, "result"), timestamp, lane)
    event.update(
        {
            "tool_call_id": _event_id(lane.session_id, call.offset, "call"),
            "tool_name": call.tool_name,
            "output_chars": output_chars,
            "is_error": is_error,
        }
    )
    return event


def _on_tool_call_update(update: dict[str, Any], lane: GrokLaneState, timestamp: str) -> list[dict[str, Any]]:
    status = update.get("status")
    tool_call_id = _as_text(update.get("toolCallId"))
    call = lane.calls_by_tool_call_id.get(tool_call_id) if tool_call_id is not None else None
    if status not in _TERMINAL_STATUSES or tool_call_id is None or call is None or call.is_resolved:
        return []
    lane.calls_by_tool_call_id[tool_call_id] = call.model_copy_update(to_update(call.field_ref().is_resolved, True))
    text = tool_result_text(update)
    is_error = status == _FAILED_STATUS or _is_failed_shell_exit(update)
    event = _tool_result_event(lane, call, timestamp, len(text), is_error)
    stamp_echoed_requests(event, text)
    snippet = error_snippet(text) if is_error else ""
    if snippet:
        event["error_snippet"] = snippet
    stamped_tk = tk_stamp(text)
    if stamped_tk:
        event["tk_stamp"] = stamped_tk
    return [event]


def _interrupted_results(lane: GrokLaneState, timestamp: str) -> list[dict[str, Any]]:
    """A terminal "Interrupted." result for each call of the turn a cancel or an exit left without one."""
    results: list[dict[str, Any]] = []
    for call in lane.calls_by_tool_call_id.values():
        if call.is_resolved:
            continue
        event = _tool_result_event(lane, call, timestamp, 0, True)
        event["error_snippet"] = _INTERRUPTED_SNIPPET
        results.append(event)
    return results


def _turn_error_event(lane: GrokLaneState, offset: int, timestamp: str, agent_result: Any) -> dict[str, Any]:
    """The failure that ended a turn, as an error message."""
    text = agent_result.strip() if isinstance(agent_result, str) and agent_result.strip() else _UNSPECIFIED_TURN_ERROR
    is_auth = is_auth_error_text(text)
    api_error_kind = None if is_auth else classify_api_error(text)
    event = _assistant_event(lane, _event_id(lane.session_id, offset, "error"), timestamp, text, [])
    event.update(
        {
            "stop_reason": GrokStopReason.ERROR.value,
            "is_auth_error": is_auth,
            "is_api_error": not is_auth,
            "api_error_kind": api_error_kind,
            "is_provider_fault": is_provider_fault(api_error_kind),
        }
    )
    return event


def _system_note(lane: GrokLaneState, event_id: str, timestamp: str, message: Any) -> list[dict[str, Any]]:
    """A note grok shows beside the conversation, as a chip; only one the shared detectors name is shown."""
    note = _as_text(message)
    if note is None or classify_user_message(note) is None:
        return []
    event = _base_event("user_message", event_id, timestamp, lane)
    event["role"] = "system"
    event["content"] = note
    stamp_user_message_display(event, note)
    return [event]


def _on_turn_completed(
    update: dict[str, Any], lane: GrokLaneState, offset: int, timestamp: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    end_kind = SpecialEventKind.TURN_COMPLETED
    match update.get("stop_reason"):
        case GrokStopReason.CANCELLED:
            events.extend(_interrupted_results(lane, timestamp))
            end_kind = SpecialEventKind.TURN_ABORTED
        case GrokStopReason.INTERRUPTED:
            events.extend(_interrupted_results(lane, timestamp))
            interrupted_note_id = _event_id(lane.session_id, offset, "interrupted")
            events.extend(_system_note(lane, interrupted_note_id, timestamp, update.get("agent_result")))
            end_kind = SpecialEventKind.TURN_ABORTED
        case GrokStopReason.ERROR:
            events.append(_turn_error_event(lane, offset, timestamp, update.get("agent_result")))
        case _:
            pass
    events.append(_special_event(lane, end_kind, offset, timestamp, _as_text(update.get("prompt_id"))))
    lane.calls_by_tool_call_id = {}
    lane.open_prompt_index = None
    lane.open_user_offset = None
    lane.open_user_texts = []
    lane.is_open_user_wake = False
    lane.has_pending_thought = False
    return events


def _on_subagent_spawned(update: dict[str, Any], lane: GrokLaneState) -> list[dict[str, Any]]:
    """Link the child session to the open spawn_subagent call with the same description, else the first open one."""
    child_session_id = _as_text(update.get("child_session_id")) or _as_text(update.get("subagent_id"))
    if child_session_id is None:
        return []
    description = _as_text(update.get("description"))
    candidates = [
        (tool_call_id, call)
        for tool_call_id, call in lane.calls_by_tool_call_id.items()
        if call.tool_name == _SPAWN_SUBAGENT_TOOL_NAME and not call.is_subagent_linked and not call.is_resolved
    ]
    matching = [candidate for candidate in candidates if candidate[1].description == description] or candidates
    call_event_id: str | None = None
    if matching:
        tool_call_id, call = matching[0]
        lane.calls_by_tool_call_id[tool_call_id] = call.model_copy_update(
            to_update(call.field_ref().is_subagent_linked, True)
        )
        call_event_id = _event_id(lane.session_id, call.offset, "call")
    return [
        {
            "type": SUBAGENT_SPAWNED_MARKER_TYPE,
            "child_session_id": child_session_id,
            "call_event_id": call_event_id,
            "subagent_metadata": {
                "agent_type": _as_text(update.get("subagent_type")) or "",
                "description": description or "",
                "session_id": child_session_id,
            },
        }
    ]


@pure
def _task_finished(task_id: str | None, will_wake: Any) -> list[dict[str, Any]]:
    if task_id is None:
        return []
    return [{"type": TASK_FINISHED_MARKER_TYPE, "task_id": task_id, "will_wake": will_wake is True}]


def parse_record(record: dict[str, Any], lane: GrokLaneState, *, offset: int) -> list[dict[str, Any]]:
    """The events of the ``updates.jsonl`` record at byte ``offset`` of ``lane``'s session, advancing ``lane``.

    Every event carries ``session_id``. A returned dict whose ``type`` is in
    :data:`INTERNAL_MARKER_TYPES` is for the loader and never goes on the wire.
    """
    update = _update_of(record)
    timestamp = record_timestamp(record)
    kind = update.get("sessionUpdate")
    if kind != "agent_message_chunk" and kind not in _BACKGROUND_REPORT_KINDS:
        lane.open_text_run = None
    match kind:
        case "user_message_chunk":
            return _on_user_message_chunk(update, lane, offset, timestamp)
        case "agent_message_chunk":
            return _on_agent_message_chunk(update, lane, offset, timestamp)
        case "agent_thought_chunk":
            return _on_agent_thought_chunk(update, lane)
        case "tool_call":
            return _on_tool_call(update, lane, offset, timestamp)
        case "tool_call_update":
            return _on_tool_call_update(update, lane, timestamp)
        case "turn_completed":
            return _on_turn_completed(update, lane, offset, timestamp)
        case "subagent_spawned":
            return _on_subagent_spawned(update, lane)
        case "task_completed":
            return _task_finished(
                _as_text(_as_dict(update.get("task_snapshot")).get("task_id")), update.get("will_wake")
            )
        case "subagent_finished":
            subagent_id = _as_text(update.get("subagent_id")) or _as_text(update.get("child_session_id"))
            return _task_finished(subagent_id, update.get("will_wake"))
        case "hook_annotation":
            hook_note_id = _event_id(lane.session_id, offset, "hook-annotation")
            return _system_note(lane, hook_note_id, timestamp, update.get("message"))
        case _:
            return []


@pure
def detail_for(event: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    """``event``'s deferred payloads read from ``record``, or None when the record holds none of them.

    ``record`` is the line the event was parsed from (a call's input, a result's output) or, for
    an event with ``has_thinking``, its thought line. The shape is ``{"inputs_by_tool_call_id",
    "output", "thinking"}``.
    """
    update = _update_of(record)
    event_type = event.get("type")
    match update.get("sessionUpdate"):
        case "tool_call" if event_type == "assistant_message" and event.get("tool_calls"):
            tool_call_id = event["tool_calls"][0].get("tool_call_id")
            return {
                "inputs_by_tool_call_id": {tool_call_id: _raw_input_json(update)},
                "output": None,
                "thinking": None,
            }
        case "tool_call_update" if event_type == "tool_result" and update.get("status") in _TERMINAL_STATUSES:
            return {"inputs_by_tool_call_id": {}, "output": tool_result_text(update), "thinking": None}
        case "agent_thought_chunk":
            thinking = _as_text(_as_dict(update.get("content")).get("text"))
            return None if thinking is None else {"inputs_by_tool_call_id": {}, "output": None, "thinking": thinking}
        case _:
            return None
