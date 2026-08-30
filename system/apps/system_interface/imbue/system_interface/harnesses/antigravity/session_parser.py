"""Turn decoded agy ``steps`` (:class:`DecodedStep`) into the shared web-UI event schema.

The antigravity analogue of ``claude_session_parser`` / ``codex_session_parser``: it maps
agy's steps into the exact dict shapes the frontend consumes (``user_message`` /
``assistant_message`` / ``tool_result``), so the transport, the frontend, and the activity
tracker need no antigravity-specific branches.

Two-phase tool emission (the key to a live activity caption -- see the harness spec §5):
a tool step's ``assistant_message`` + ``tool_call`` are emitted as soon as the row appears
(even while ``RUNNING``), but its ``tool_result`` only once the step settles. So during
execution the call is unmatched (= the ``TOOL_RUNNING`` signal, captioned with the tool's
own label) and after, matched (back to ``THINKING``). Event ids are keyed on agy's stable
per-conversation ``idx`` so a row re-decoded across polls (RUNNING -> DONE) supersedes
rather than duplicating.
"""

from __future__ import annotations

import re
from typing import Any
from typing import Final

from imbue.system_interface.harnesses.auth_errors import is_auth_error_text
from imbue.system_interface.harnesses.antigravity.agy_transcript import DecodedStep
from imbue.system_interface.harnesses.antigravity.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.antigravity.tool_labels import shell_command
from imbue.system_interface.harnesses.antigravity.tool_labels import tool_labels
from imbue.system_interface.harnesses.events import MAX_TOOL_INPUT_PREVIEW_LENGTH
from imbue.system_interface.harnesses.message_display import stamp_user_message_display
from imbue.system_interface.harnesses.tool_output import classify_tool_call_display
from imbue.system_interface.harnesses.tool_output import find_permission_request
from imbue.system_interface.harnesses.tool_output import is_pure_tk_lifecycle_command
from imbue.system_interface.harnesses.tool_output import truncate_tool_output

# "common" here means the normalized/common event *form*, matching the
# ``<harness>/common_transcript`` label claude/codex stamp -- not an on-disk file.
SOURCE: Final[str] = "antigravity/common_transcript"

# agy steps carry no per-message model slug; surface the same placeholder codex uses so the
# frontend's non-optional ``model`` field stays populated.
_UNKNOWN_MODEL: Final[str] = "unknown"

# Only an explicit human turn becomes a user bubble; USER_IMPLICIT / SYSTEM inputs are
# framework-injected context (history, checkpoints), not a turn.
_HUMAN_SOURCE: Final[str] = "USER_EXPLICIT"

# agy wraps the human prompt as ``<USER_REQUEST>\n...\n</USER_REQUEST>`` followed by
# ``<ADDITIONAL_METADATA>`` / ``<USER_SETTINGS_CHANGE>`` trailers we strip.
_USER_REQUEST_RE: Final[re.Pattern[str]] = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def _clean_user_text(raw: str) -> str:
    match = _USER_REQUEST_RE.search(raw)
    return match.group(1).strip() if match is not None else raw.strip()


def _event_id(step: DecodedStep, suffix: str) -> str:
    return f"{step.conv_id}:{step.idx}:{suffix}"


def _input_preview(tool_name: str, args_json: str) -> str:
    """The stored ``input_preview``: the raw args truncated to the shared cap -- but left
    whole for a file body or a tk command the diff/timeline view needs entire."""
    if keeps_full_tool_input(tool_name, args_json):
        return args_json
    if len(args_json) > MAX_TOOL_INPUT_PREVIEW_LENGTH:
        return args_json[:MAX_TOOL_INPUT_PREVIEW_LENGTH] + "..."
    return args_json


def _user_message(step: DecodedStep) -> list[dict[str, Any]]:
    text = _clean_user_text(step.user_text or "")
    if not text:
        return []
    event_id = _event_id(step, "user")
    event = {
        "timestamp": step.created_at,
        "type": "user_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "user",
        "content": text,
        "message_uuid": event_id,
    }
    # The shared render decision -- agy gets the same detector table as claude, codex and pi.
    # This is also what stamps ``non_turn_tail``, which the activity path reads to avoid
    # pinning the indicator on a message no reply is coming for (a /model-style command).
    stamp_user_message_display(event, text)
    return [event]


def _assistant_message(
    step: DecodedStep, *, text: str, tool_calls: list[dict[str, str]], suffix: str
) -> dict[str, Any]:
    event_id = _event_id(step, suffix)
    event: dict[str, Any] = {
        "timestamp": step.created_at,
        "type": "assistant_message",
        "event_id": event_id,
        "source": SOURCE,
        "role": "assistant",
        "model": _UNKNOWN_MODEL,
        "text": text,
        "tool_calls": tool_calls,
        "stop_reason": None,
        "usage": None,
        "message_uuid": event_id,
        "is_auth_error": False,
        # The API-error trio the cross-harness contract requires (Response.ts types them as
        # required). Defaulted here so EVERY assistant event carries them; the error-step path
        # below flips ``is_api_error``. agy's store does not distinguish an error's kind or
        # whether the provider was at fault, so those stay None/False rather than guessed.
        "is_api_error": False,
        "api_error_kind": None,
        "is_provider_fault": False,
    }
    # agy gives us reasoning for free (claude drops it); pass it through when present.
    if step.thinking:
        event["thinking"] = step.thinking
    return event


def _tool_events(step: DecodedStep) -> list[dict[str, Any]]:
    call = step.tool_call
    assert call is not None
    call_event_id = _event_id(step, "toolcall")
    header_label, caption_label = tool_labels(call.name, call.args, call.tool_action)
    tool_call = {
        "tool_call_id": call_event_id,
        "tool_name": call.name,
        "input_preview": _input_preview(call.name, call.args),
        "header_label": header_label,
        "caption_label": caption_label,
    }
    # The render decision ships with the call, exactly as it does for claude/codex/pi: a PURE
    # tk lifecycle call is a hidden structural marker rather than work, and a latchkey POST
    # renders as the permission card (recognised from the INPUT, so the card appears while the
    # request is still pending and has no result yet).
    command = shell_command(call.name, call.args)
    is_pure_tk = command is not None and is_pure_tk_lifecycle_command(command)
    display = classify_tool_call_display(is_pure_tk=is_pure_tk, raw_input=call.args)
    if display is not None:
        tool_call["display"] = display.value
    events: list[dict[str, Any]] = [_assistant_message(step, text="", tool_calls=[tool_call], suffix="toolcall")]
    # The result only exists once the step settles; withholding it while RUNNING is what
    # keeps the call unmatched (= TOOL_RUNNING) during execution.
    if step.is_terminal and step.tool_result_text is not None:
        result_event_id = _event_id(step, "toolresult")
        # Lift the permission-request object and preserve tk step decoration BEFORE truncation
        # (shared with the claude/codex/pi parsers -- see ``harnesses/tool_output``). Order
        # matters: head-slicing first would destroy an object or decoration past the cut.
        raw_output = step.tool_result_text
        permission_request = find_permission_request(raw_output)
        result_event: dict[str, Any] = {
            "timestamp": step.created_at,
            "type": "tool_result",
            "event_id": result_event_id,
            "source": SOURCE,
            "tool_call_id": call_event_id,
            "tool_name": call.name,
            "output": truncate_tool_output(raw_output, permission_request),
            "is_error": step.is_error_result,
            "message_uuid": result_event_id,
        }
        if permission_request is not None:
            result_event["permission_request"] = permission_request.details
        events.append(result_event)
    return events


def parse_step(step: DecodedStep) -> list[dict[str, Any]]:
    """Map one decoded agy step to zero or more UI event dicts.

    A tool step yields its ``assistant_message`` + ``tool_call`` immediately and its
    ``tool_result`` once terminal; a user/planner/error step yields one event; anything
    else (history, system, conversation summary) yields none. The watcher dedups by
    ``event_id`` across polls, so re-decoding a RUNNING row that later settles adds only the
    result.
    """
    if step.tool_call is not None:
        return _tool_events(step)
    if step.step_type_name == "USER_INPUT":
        if step.source_name != _HUMAN_SOURCE:
            return []
        return _user_message(step)
    if step.step_type_name == "PLANNER_RESPONSE":
        # Assistant text is only shown once the step settles -- never a partial stream.
        if not step.is_terminal:
            return []
        text = step.assistant_text or ""
        if not text and not step.thinking:
            return []
        return [_assistant_message(step, text=text, tool_calls=[], suffix="assistant")]
    if step.step_type_name == "ERROR_MESSAGE":
        if not step.is_terminal or not step.error_text:
            return []
        event = _assistant_message(step, text=step.error_text, tool_calls=[], suffix="error")
        event["is_api_error"] = True
        # Which error it is decides what the user can do about it. An auth failure is the one
        # they can fix, and it is what the dead-account notice keys on; everything else is
        # just a failed turn. agy passes the provider's own words through, so the shared
        # vocabulary reads them.
        event["is_auth_error"] = is_auth_error_text(step.error_text)
        return [event]
    return []
