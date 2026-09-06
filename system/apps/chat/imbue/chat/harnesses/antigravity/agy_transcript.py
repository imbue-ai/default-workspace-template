"""Decode agy's SQLite conversation store (the ``steps`` protobuf) into typed records.

NOTE -- best-effort black magic, NOT a style exemplar for the rest of the repo. This
module reverse-engineers an *undocumented* format: agy publishes no ``.proto`` schema, so
the field/enum map below is recovered empirically from the binary's embedded descriptors.
It is a deliberate near-duplicate of ``mngr_antigravity``'s ``decode_agy_transcript.py``
(the source of truth for the recovered map + its release-marked descriptor-diff test); we
keep our own copy so system_interface never has to import mngr internals, and we EXTEND it
to surface what mngr's stream drops -- tool call id/name/args, agy's own short/long
captions (``f30``/``f31``), and the tool result text. If agy's schema drifts, update both
in lockstep (see ``libs/mngr_antigravity/regenerating_protobuf_schema.md``).

Protobuf keys compatibility on field *numbers*; agy's ~weekly releases are normally
additive, which a number-keyed wire-walk tolerates by construction (unknown fields skipped,
unknown enum values fall back to ``<KIND>_<n>``). The decode is deliberately defensive and
lossy: it degrades or skips malformed/truncated input rather than raising, because a
best-effort transcript beats a crashed capture. Do not cargo-cult these trade-offs.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel

# --- gemini_coder.Step field numbers (recovered) -----------------------------------------
_STEP_STATUS: Final[int] = 4
_STEP_METADATA: Final[int] = 5
_STEP_USER_INPUT: Final[int] = 19
_STEP_PLANNER_RESPONSE: Final[int] = 20
_STEP_ERROR_MESSAGE: Final[int] = 24
# CortexStepMetadata. created_at (f1) is a google.protobuf.Timestamp { f1 seconds; f2 nanos }.
# f4 is a ChatToolCall present on tool steps. agy also declares caption fields at f30/f31, but
# they are absent on every row of both live stores measured (0 of 41) -- the captions live in
# the step BODY instead, as ``toolSummary``/``toolAction`` argument pairs.
_METADATA_CREATED_AT: Final[int] = 1
_METADATA_SOURCE: Final[int] = 3
_METADATA_TOOL_CALL: Final[int] = 4

# The step BODY: agy's own record of the call it ran, alongside the metadata. Field numbers
# measured against agy 1.1.20 on two live conversation stores -- see
# docs/design/antigravity-transcript-schema.md.
_STEP_BODY: Final[int] = 140
_BODY_ARG_PAIR: Final[int] = 1
_BODY_RESULT: Final[int] = 2
_ARG_KEY: Final[int] = 1
_ARG_VALUE: Final[int] = 2
_RESULT_TEXT: Final[int] = 1
# The argument keys carrying agy's own model-authored captions.
_ARG_TOOL_SUMMARY: Final[str] = "toolSummary"
_ARG_TOOL_ACTION: Final[str] = "toolAction"
_TIMESTAMP_SECONDS: Final[int] = 1
# CortexStepUserInput: the typed message lands in query (f1) or user_response (f2).
_USER_INPUT_QUERY: Final[int] = 1
_USER_INPUT_RESPONSE: Final[int] = 2
# CortexStepPlannerResponse
_PLANNER_RESPONSE_TEXT: Final[int] = 1
_PLANNER_THINKING: Final[int] = 3
# ChatToolCall (exa.codeium_common_pb): f1 call id, f2 name, f3 args (a JSON string).
_TOOL_CALL_ID: Final[int] = 1
_TOOL_CALL_NAME: Final[int] = 2
_TOOL_CALL_ARGS: Final[int] = 3
# CortexStepErrorMessage.f3 (error) is a CortexErrorDetails sub-message; its f1
# (user_error_message) is the user-facing text, f2/f3 (short/full) are fallbacks.
_ERROR_MESSAGE_DETAILS: Final[int] = 3
_ERROR_DETAILS_USER_MESSAGE: Final[int] = 1
_ERROR_DETAILS_SHORT_ERROR: Final[int] = 2
_ERROR_DETAILS_FULL_ERROR: Final[int] = 3

# --- enum value -> unprefixed name (unknown -> ``<KIND>_<n>``) ----------------------------
STEP_TYPE_USER_INPUT: Final[int] = 14
STEP_TYPE_PLANNER_RESPONSE: Final[int] = 15
STEP_TYPE_ERROR_MESSAGE: Final[int] = 17

_STEP_TYPE_NAMES: Final[dict[int, str]] = {
    5: "CODE_ACTION",
    7: "GREP_SEARCH",
    8: "VIEW_FILE",
    9: "LIST_DIRECTORY",
    14: "USER_INPUT",
    15: "PLANNER_RESPONSE",
    17: "ERROR_MESSAGE",
    21: "RUN_COMMAND",
    # The live tool-step type on agy 1.1.20 -- every tool call, whatever the tool, arrives as
    # 132 (41 of 41 measured); the per-tool types above are the older, now-unused encoding.
    # Dispatch keys off the decoded tool call rather than this name, so the name is only for
    # diagnostics -- but an unmapped type reads as "STEP_TYPE_132" in logs, which misleads.
    132: "TOOL_CALL",
    # Session identity: one per conversation, SYSTEM source, no user-visible content.
    23: "SESSION_IDENTITY",
    91: "GENERATE_IMAGE",
    98: "CONVERSATION_HISTORY",
    101: "SYSTEM_MESSAGE",
}
_STEP_SOURCE_NAMES: Final[dict[int, str]] = {
    2: "MODEL",
    3: "USER_IMPLICIT",
    4: "USER_EXPLICIT",
    5: "SYSTEM",
    6: "SYSTEM_SDK",
}
_STEP_STATUS_NAMES: Final[dict[int, str]] = {
    1: "PENDING",
    2: "RUNNING",
    3: "DONE",
    4: "INVALID",
    5: "CLEARED",
    6: "CANCELED",
    7: "ERROR",
    8: "GENERATING",
    9: "WAITING",
    11: "QUEUED",
    12: "INTERRUPTED",
}
# A step is "settled" -- safe to emit whole -- once it reaches a terminal status. While it
# is still PENDING/RUNNING/GENERATING/WAITING/QUEUED its result is incomplete, so the caller
# holds its ``tool_result`` (but may still show the running tool_call -- see the watcher).
TERMINAL_STATUS_NAMES: Final[frozenset[str]] = frozenset(
    {"DONE", "INVALID", "CLEARED", "CANCELED", "ERROR", "INTERRUPTED"}
)
_ERROR_STATUS_NAMES: Final[frozenset[str]] = frozenset({"CANCELED", "ERROR", "INVALID"})

# A 64-bit varint is at most 10 bytes; this bounds every decode loop.
_MAX_VARINT_BYTES: Final[int] = 10
_WIRE_VARINT: Final[int] = 0
_WIRE_64BIT: Final[int] = 1
_WIRE_LEN: Final[int] = 2
_WIRE_32BIT: Final[int] = 5

# How far a decoded tool result is clipped in the decoder (a second clip happens in the
# parser against the shared event cap; kept modest here so we never hold a huge blob).
_MAX_RESULT_CHARS: Final[int] = 4000


class TruncatedError(Exception):
    """A protobuf blob ended mid-field; the step is skipped and retried next pass."""


class DecodedToolCall(FrozenModel):
    call_id: str
    name: str
    # ``args`` is a JSON string (ChatToolCall.f3).
    args: str
    # agy's own model-authored captions, read from the step BODY's argument pairs -- a noun
    # phrase ("Task tracking") and a verb phrase ("Creating step"), written per call. The
    # metadata caption fields (f30/f31) these replace were absent on every measured row.
    tool_summary: str
    tool_action: str


class DecodedStep(FrozenModel):
    """One ``steps`` row, decoded. The set fields depend on the step's role."""

    conv_id: str
    idx: int
    step_type_name: str
    status_name: str
    source_name: str
    created_at: str
    is_terminal: bool
    # role-specific payload (all optional; which are set depends on step_type)
    user_text: str | None = None
    assistant_text: str | None = None
    thinking: str | None = None
    tool_call: DecodedToolCall | None = None
    tool_result_text: str | None = None
    is_error_result: bool = False
    error_text: str | None = None


def _read_varint(blob: bytes, start: int) -> tuple[int, int]:
    value = 0
    shift = 0
    index = start
    for _ in range(_MAX_VARINT_BYTES):
        if index >= len(blob):
            raise TruncatedError("varint ran past end of blob")
        byte = blob[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise TruncatedError("varint exceeded 10 bytes")


def _iter_fields(blob: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)``; value is an int (varint) or bytes (len)."""
    index = 0
    length = len(blob)
    while index < length:
        tag, index = _read_varint(blob, index)
        field = tag >> 3
        wire = tag & 7
        if wire == _WIRE_VARINT:
            value, index = _read_varint(blob, index)
            yield field, wire, value
        elif wire == _WIRE_LEN:
            size, index = _read_varint(blob, index)
            if index + size > length:
                raise TruncatedError("length-delimited field ran past end of blob")
            yield field, wire, blob[index : index + size]
            index += size
        elif wire == _WIRE_64BIT:
            if index + 8 > length:
                raise TruncatedError("64-bit field ran past end of blob")
            yield field, wire, blob[index : index + 8]
            index += 8
        elif wire == _WIRE_32BIT:
            if index + 4 > length:
                raise TruncatedError("32-bit field ran past end of blob")
            yield field, wire, blob[index : index + 4]
            index += 4
        else:
            # Wire types 3/4 (deprecated groups) / 6/7 (unused) cannot appear in a
            # well-formed blob; an unknown wire type means truncated or corrupt bytes,
            # not schema drift (a renumber keeps the wire valid). Stop, as with truncation.
            raise TruncatedError(f"unknown wire type {wire}")


def _first(blob: bytes, field_number: int) -> Any:
    for field, _wire, value in _iter_fields(blob):
        if field == field_number:
            return value
    return None


def _first_str(blob: bytes, field_number: int) -> str:
    value = _first(blob, field_number)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return ""


def _first_message(blob: bytes, field_number: int) -> bytes | None:
    value = _first(blob, field_number)
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def _first_varint(blob: bytes, field_number: int) -> int | None:
    value = _first(blob, field_number)
    return value if isinstance(value, int) else None


def _iso_timestamp(metadata: bytes | None) -> str:
    """Render ``metadata.created_at`` (a protobuf Timestamp) as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if metadata is None:
        return ""
    created_at = _first_message(metadata, _METADATA_CREATED_AT)
    if created_at is None:
        return ""
    seconds = _first_varint(created_at, _TIMESTAMP_SECONDS)
    if seconds is None:
        return ""
    # created_at is informational; an out-of-range value (corrupt/truncated, not drift)
    # degrades to empty rather than aborting the whole decode.
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        return ""


def _decode_tool_call(metadata: bytes, body_args: dict[str, str]) -> DecodedToolCall | None:
    """A tool step carries a ``ChatToolCall`` at ``metadata.f4`` with a name; robust to new
    agy tool types because they keep this shape rather than a per-tool schema.

    ``body_args`` comes from the step body (:func:`_body_args`) and carries agy's own captions,
    which live there rather than on the metadata.
    """
    call = _first_message(metadata, _METADATA_TOOL_CALL)
    if call is None:
        return None
    name = _first_str(call, _TOOL_CALL_NAME)
    if not name:
        return None
    return DecodedToolCall(
        call_id=_first_str(call, _TOOL_CALL_ID),
        name=name,
        args=_first_str(call, _TOOL_CALL_ARGS),
        tool_summary=body_args.get(_ARG_TOOL_SUMMARY, ""),
        tool_action=body_args.get(_ARG_TOOL_ACTION, ""),
    )


def _iter_messages(blob: bytes, field_number: int) -> Iterator[bytes]:
    """Every length-delimited value on ``field_number`` -- the repeated counterpart to
    :func:`_first_message`, which only ever returns the first.

    The ``_WIRE_LEN`` test is load-bearing: :func:`_iter_fields` also yields ``bytes`` for the
    fixed-width wire types, so an ``isinstance`` check alone would hand an 8-byte double on to
    be read as a nested message.
    """
    for field, wire, value in _iter_fields(blob):
        if field == field_number and wire == _WIRE_LEN and isinstance(value, (bytes, bytearray)):
            yield bytes(value)


def _body_args(payload: bytes) -> dict[str, str]:
    """The call's argument pairs from the step body: the tool's own arguments (``CommandLine``,
    ``Cwd``, ...) plus agy's model-authored ``toolSummary``/``toolAction`` captions."""
    try:
        body = _first_message(payload, _STEP_BODY)
        if body is None:
            return {}
        args: dict[str, str] = {}
        for pair in _iter_messages(body, _BODY_ARG_PAIR):
            key, value = _first_str(pair, _ARG_KEY), _first_str(pair, _ARG_VALUE)
            if key and value:
                args[key] = value
        return args
    except TruncatedError:
        return {}


def _tool_result_text(payload: bytes) -> str:
    """The command's output, read from the step body's result field.

    NOT a search. This used to keep the longest printable run found anywhere in the payload,
    which returned the tool's ARGUMENTS rather than its output for 41 of 41 tool steps measured
    across two live stores -- so no tk line ever reached the chat and the step progress view
    never drew a node. The arguments JSON is simply longer than a typical command's output.

    Returns "" and never None. The caller emits a ``tool_result`` event only when this is not
    None, so returning None for a body shape we do not recognise would leave that call
    permanently unmatched and pin the activity indicator at TOOL_RUNNING for the life of the
    agent -- worse than the bug this replaces. Only ``run_command`` bodies are measured (every
    observed step is one); the other tools are unverified, so the unrecognised path must stay
    harmless. The RUNNING case never reaches here: ``decode_step`` calls this only when the
    step is terminal.
    """
    try:
        body = _first_message(payload, _STEP_BODY)
        result = _first_message(body, _BODY_RESULT) if body is not None else None
    except TruncatedError:
        return ""
    if result is None:
        return ""
    return _first_str(result, _RESULT_TEXT)[:_MAX_RESULT_CHARS]


def decode_step(conv_id: str, idx: int, step_type: int, status: int, payload: bytes) -> DecodedStep:
    """Decode one ``steps`` row into a :class:`DecodedStep`.

    Raises :class:`TruncatedError` if the protobuf is incomplete (mid-write); the caller
    skips the step and retries on the next pass.
    """
    metadata = _first_message(payload, _STEP_METADATA)
    source_value = _first_varint(metadata, _METADATA_SOURCE) if metadata is not None else None
    status_name = _STEP_STATUS_NAMES.get(status, f"STEP_STATUS_{status}")
    step_type_name = _STEP_TYPE_NAMES.get(step_type, f"STEP_TYPE_{step_type}")

    body_args = _body_args(payload)
    tool_call = _decode_tool_call(metadata, body_args) if metadata is not None else None
    is_terminal = status_name in TERMINAL_STATUS_NAMES

    user_text: str | None = None
    assistant_text: str | None = None
    thinking: str | None = None
    tool_result_text: str | None = None
    error_text: str | None = None

    if tool_call is not None:
        # A tool step: the result body is only complete once the step settles.
        if is_terminal:
            tool_result_text = _tool_result_text(payload)
    elif step_type == STEP_TYPE_USER_INPUT:
        user_input = _first_message(payload, _STEP_USER_INPUT)
        if user_input is not None:
            user_text = _first_str(user_input, _USER_INPUT_QUERY) or _first_str(user_input, _USER_INPUT_RESPONSE)
    elif step_type == STEP_TYPE_PLANNER_RESPONSE:
        planner = _first_message(payload, _STEP_PLANNER_RESPONSE)
        if planner is not None:
            assistant_text = _first_str(planner, _PLANNER_RESPONSE_TEXT)
            thinking = _first_str(planner, _PLANNER_THINKING) or None
    elif step_type == STEP_TYPE_ERROR_MESSAGE:
        error = _first_message(payload, _STEP_ERROR_MESSAGE)
        details = _first_message(error, _ERROR_MESSAGE_DETAILS) if error is not None else None
        if details is not None:
            error_text = (
                _first_str(details, _ERROR_DETAILS_USER_MESSAGE)
                or _first_str(details, _ERROR_DETAILS_SHORT_ERROR)
                or _first_str(details, _ERROR_DETAILS_FULL_ERROR)
            )
    else:
        # Other step types (conversation history, system messages) carry no renderable
        # role payload; they decode to a bare record the parser drops.
        pass

    return DecodedStep(
        conv_id=conv_id,
        idx=idx,
        step_type_name=step_type_name,
        status_name=status_name,
        source_name=_STEP_SOURCE_NAMES.get(source_value or 0, f"STEP_SOURCE_{source_value}"),
        created_at=_iso_timestamp(metadata),
        is_terminal=is_terminal,
        user_text=user_text,
        assistant_text=assistant_text,
        thinking=thinking,
        tool_call=tool_call,
        tool_result_text=tool_result_text,
        is_error_result=tool_call is not None and status_name in _ERROR_STATUS_NAMES,
        error_text=error_text,
    )
