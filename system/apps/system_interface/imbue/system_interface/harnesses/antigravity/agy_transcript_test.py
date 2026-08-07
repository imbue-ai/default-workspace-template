"""Unit tests for the agy ``steps`` protobuf decoder.

Fixtures are built with a tiny local protobuf encoder (``_msg`` / ``_varint`` / ``_field``)
so each test states exactly the wire bytes it exercises -- clearer than a captured hex blob
and independent of any live ``.db``. The field numbers mirror ``agy_transcript``'s recovered
map; if that map changes these builders change with it.
"""

from __future__ import annotations

from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_ERROR_MESSAGE
from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_PLANNER_RESPONSE
from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_USER_INPUT
from imbue.system_interface.harnesses.antigravity.agy_transcript import TruncatedError
from imbue.system_interface.harnesses.antigravity.agy_transcript import decode_step

import pytest

# --- a minimal protobuf wire encoder, just for building fixtures -------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _vfield(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _lfield(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def _sfield(field: int, text: str) -> bytes:
    return _lfield(field, text.encode("utf-8"))


def _timestamp_metadata(seconds: int = 1_700_000_000, *, source: int = 4, extra: bytes = b"") -> bytes:
    """A CortexStepMetadata: created_at (f1 = Timestamp{f1 seconds}), source (f3), + extra."""
    created_at = _vfield(1, seconds)
    return _lfield(1, created_at) + _vfield(3, source) + extra


def _tool_metadata(name: str, args: str, *, call_id: str = "abc123", short: str = "", long: str = "") -> bytes:
    """Metadata carrying a ChatToolCall (f4) + optional captions (f30/f31)."""
    call = _sfield(1, call_id) + _sfield(2, name) + _sfield(3, args)
    extra = _lfield(4, call)
    if short:
        extra += _sfield(30, short)
    if long:
        extra += _sfield(31, long)
    return _timestamp_metadata(source=2, extra=extra)


def _step(metadata: bytes, body: bytes = b"") -> bytes:
    """A Step payload: metadata (f5) + a body field. step_type/status come from columns,
    so they are passed to decode_step separately, not encoded here."""
    return _lfield(5, metadata) + body


# --- tests -------------------------------------------------------------------------------


def test_user_input_decodes_query_text() -> None:
    payload = _step(_timestamp_metadata(source=4), body=_lfield(19, _sfield(1, "hey there")))
    step = decode_step("conv", 0, STEP_TYPE_USER_INPUT, 3, payload)
    assert step.step_type_name == "USER_INPUT"
    assert step.source_name == "USER_EXPLICIT"
    assert step.user_text == "hey there"
    assert step.is_terminal is True
    assert step.tool_call is None


def test_planner_response_decodes_text_and_thinking() -> None:
    body = _lfield(20, _sfield(1, "Here is the answer.") + _sfield(3, "let me think"))
    step = decode_step("conv", 5, STEP_TYPE_PLANNER_RESPONSE, 3, _step(_timestamp_metadata(source=2), body))
    assert step.assistant_text == "Here is the answer."
    assert step.thinking == "let me think"


def test_planner_response_without_thinking_leaves_it_none() -> None:
    body = _lfield(20, _sfield(1, "answer"))
    step = decode_step("conv", 5, STEP_TYPE_PLANNER_RESPONSE, 3, _step(_timestamp_metadata(source=2), body))
    assert step.thinking is None


def test_tool_step_detected_by_metadata_and_carries_captions() -> None:
    metadata = _tool_metadata(
        "run_command",
        '{"CommandLine":"python3 showcase.py"}',
        call_id="X1",
        short="Running python3 showcase.py",
        long="Executing showcase script",
    )
    # result body lives in a non-metadata top-level field
    payload = _step(metadata, body=_lfield(28, _sfield(21, "Hello from the script")))
    step = decode_step("conv", 16, 21, 3, payload)
    assert step.tool_call is not None
    assert step.tool_call.name == "run_command"
    assert step.tool_call.call_id == "X1"
    assert step.tool_call.args == '{"CommandLine":"python3 showcase.py"}'
    assert step.tool_call.caption_short == "Running python3 showcase.py"
    assert step.tool_call.caption_long == "Executing showcase script"
    assert step.tool_result_text == "Hello from the script"


def test_running_tool_step_withholds_result() -> None:
    """A non-terminal (RUNNING=2) tool row exposes its call but not its result yet."""
    metadata = _tool_metadata("run_command", '{"CommandLine":"sleep 9"}', short="Running sleep")
    payload = _step(metadata, body=_lfield(28, _sfield(21, "partial output")))
    step = decode_step("conv", 3, 21, 2, payload)
    assert step.status_name == "RUNNING"
    assert step.is_terminal is False
    assert step.tool_call is not None
    assert step.tool_result_text is None


def test_error_status_tool_flags_error_result() -> None:
    metadata = _tool_metadata("run_command", "{}", short="Running")
    step = decode_step("conv", 3, 21, 7, _step(metadata, body=_lfield(28, _sfield(21, "boom"))))
    assert step.status_name == "ERROR"
    assert step.is_error_result is True


def test_error_message_step_prefers_user_message() -> None:
    details = _sfield(1, "user-facing error") + _sfield(2, "short") + _sfield(3, "full")
    body = _lfield(24, _lfield(3, details))
    step = decode_step("conv", 9, STEP_TYPE_ERROR_MESSAGE, 7, _step(_timestamp_metadata(), body))
    assert step.error_text == "user-facing error"


def test_unknown_step_type_falls_back_to_named_placeholder() -> None:
    step = decode_step("conv", 1, 777, 3, _step(_timestamp_metadata()))
    assert step.step_type_name == "STEP_TYPE_777"


def test_unknown_status_falls_back_and_is_not_terminal() -> None:
    step = decode_step("conv", 1, STEP_TYPE_USER_INPUT, 42, _step(_timestamp_metadata()))
    assert step.status_name == "STEP_STATUS_42"
    assert step.is_terminal is False


def test_truncated_payload_raises() -> None:
    # a length-delimited field claiming more bytes than remain
    truncated = _tag(5, 2) + _varint(50) + b"only a few"
    with pytest.raises(TruncatedError):
        decode_step("conv", 0, STEP_TYPE_USER_INPUT, 3, truncated)


def test_created_at_renders_iso() -> None:
    step = decode_step("conv", 0, STEP_TYPE_USER_INPUT, 3, _step(_timestamp_metadata(seconds=1_700_000_000)))
    assert step.created_at == "2023-11-14T22:13:20Z"
