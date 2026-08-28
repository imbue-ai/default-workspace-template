"""Unit tests for the agy ``steps`` protobuf decoder.

Fixtures are built with the tiny protobuf encoder in :mod:`testing` so each test states
exactly the wire bytes it exercises -- clearer than a captured hex blob and independent of
any live ``.db``.
"""

from __future__ import annotations

import pytest

from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_ERROR_MESSAGE
from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_PLANNER_RESPONSE
from imbue.system_interface.harnesses.antigravity.agy_transcript import STEP_TYPE_USER_INPUT
from imbue.system_interface.harnesses.antigravity.agy_transcript import TruncatedError
from imbue.system_interface.harnesses.antigravity.agy_transcript import _iter_messages
from imbue.system_interface.harnesses.antigravity.agy_transcript import decode_step
from imbue.system_interface.harnesses.antigravity.testing import build_metadata as _timestamp_metadata
from imbue.system_interface.harnesses.antigravity.testing import build_step_payload as _step
from imbue.system_interface.harnesses.antigravity.testing import build_tool_body as _tool_body
from imbue.system_interface.harnesses.antigravity.testing import build_tool_metadata as _tool_metadata
from imbue.system_interface.harnesses.antigravity.testing import encode_varint as _varint
from imbue.system_interface.harnesses.antigravity.testing import len_field as _lfield
from imbue.system_interface.harnesses.antigravity.testing import load_captured_step
from imbue.system_interface.harnesses.antigravity.testing import str_field as _sfield

# The live tool-step type on agy 1.1.20; every tool call arrives as 132.
_TOOL_CALL = 132


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


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
    )
    payload = _step(
        metadata,
        body=_tool_body(
            result="Hello from the script",
            tool_summary="Script execution",
            tool_action="Running python3 showcase.py",
        ),
    )
    step = decode_step("conv", 16, _TOOL_CALL, 3, payload)
    assert step.tool_call is not None
    assert step.tool_call.name == "run_command"
    assert step.tool_call.call_id == "X1"
    assert step.tool_call.args == '{"CommandLine":"python3 showcase.py"}'
    assert step.tool_call.tool_action == "Running python3 showcase.py"
    assert step.tool_call.tool_summary == "Script execution"
    assert step.tool_result_text == "Hello from the script"


def test_running_tool_step_withholds_result() -> None:
    """A non-terminal (RUNNING=2) tool row exposes its call but not its result yet."""
    metadata = _tool_metadata("run_command", '{"CommandLine":"sleep 9"}')
    payload = _step(metadata, body=_tool_body(result="partial output"))
    step = decode_step("conv", 3, _TOOL_CALL, 2, payload)
    assert step.status_name == "RUNNING"
    assert step.is_terminal is False
    assert step.tool_call is not None
    assert step.tool_result_text is None


def test_error_status_tool_flags_error_result() -> None:
    metadata = _tool_metadata("run_command", "{}")
    step = decode_step("conv", 3, _TOOL_CALL, 7, _step(metadata, body=_tool_body(result="boom")))
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


# --- decoding a REAL payload -------------------------------------------------------------
# Every test above builds its own payload with the helpers in ``testing``. None of those
# shapes reproduced what actually broke the decoder, which is how a bug that corrupted 100%
# of agy tool results passed CI for the life of the harness. These use captured rows.


def test_a_real_tk_create_decodes_to_the_command_output_not_its_arguments() -> None:
    """THE bug. The decoder used to keep the longest printable run it could find anywhere in
    the step, and for agy that is the ARGUMENTS blob -- so the tk lines the progress view is
    built from never reached the chat and no step node ever rendered."""
    step_type, status, payload = load_captured_step("tk_create")
    step = decode_step("conv", 3, step_type, status, payload)
    assert step.tool_result_text is not None
    assert "Created a7-step-7dlr: Run sequential test commands" in step.tool_result_text
    assert '"CommandLine"' not in step.tool_result_text, "returned the arguments, not the output"


def test_a_real_tk_close_keeps_the_title_and_summary_lines() -> None:
    """``tk close`` prints the transition FIRST and the title/summary after, so a decoder that
    truncates loses exactly the decoration the progress view needs."""
    step_type, status, payload = load_captured_step("tk_close")
    step = decode_step("conv", 51, step_type, status, payload)
    assert step.tool_result_text is not None
    assert "Updated a7-step-7dlr -> closed" in step.tool_result_text
    assert "tk-step a7-step-7dlr title:" in step.tool_result_text
    assert "tk-step a7-step-7dlr summary:" in step.tool_result_text


def test_a_real_step_carries_agys_own_captions() -> None:
    """agy writes a noun and a verb phrase per call, in the step BODY. The metadata caption
    fields we used to read (f30/f31) are absent on every captured row."""
    step_type, status, payload = load_captured_step("tk_create")
    step = decode_step("conv", 3, step_type, status, payload)
    assert step.tool_call is not None
    assert step.tool_call.tool_action == "Creating step"
    assert step.tool_call.tool_summary == "Task tracking"


def test_a_real_plain_command_decodes_its_output() -> None:
    """Not a tk-only fix: every agy tool result was the arguments blob."""
    step_type, status, payload = load_captured_step("plain_command")
    step = decode_step("conv", 7, step_type, status, payload)
    assert step.tool_result_text is not None
    assert "Completed test call 1/20" in step.tool_result_text


def test_an_unrecognised_body_yields_empty_text_never_none() -> None:
    """Only ``run_command`` bodies are measured. If a differently-shaped tool body returned
    None, ``session_parser`` would emit no ``tool_result`` at all, leaving the call unmatched
    and the activity indicator pinned at TOOL_RUNNING for the life of the agent -- worse than
    the bug being fixed. ``""`` keeps the "a terminal tool step always has a result" invariant.
    """
    payload = _step(_tool_metadata("view_file", '{"AbsolutePath":"/x"}'), body=b"")
    step = decode_step("conv", 1, _TOOL_CALL, 3, payload)
    assert step.tool_result_text == ""


def test_a_truncated_body_yields_empty_text_rather_than_raising() -> None:
    """The watcher stops scanning a conversation on TruncatedError, which is right for a
    mid-write row and permanent for a corrupt one. Result decoding swallows it."""
    metadata = _tool_metadata("run_command", '{"CommandLine":"ls"}')
    payload = _step(metadata, body=b"") + b"\xf2\x08\xff\xff\xff"
    step = decode_step("conv", 1, _TOOL_CALL, 3, payload)
    assert step.tool_result_text == ""


def test_iter_messages_skips_fixed_width_fields_on_the_same_field_number() -> None:
    """``_iter_fields`` yields ``bytes`` for the 64-bit and 32-bit wire types too, so filtering
    on isinstance alone would hand an 8-byte double on to be read as a nested message. Only
    length-delimited values are real sub-messages."""
    double_on_field_1 = _varint((1 << 3) | 1) + b"\x00" * 8
    message_on_field_1 = _lfield(1, _sfield(1, "real"))
    found = list(_iter_messages(double_on_field_1 + message_on_field_1, 1))
    assert found == [_sfield(1, "real")]
