"""Recognize the progress updates Claude writes for the user between tool calls.

On Claude 5 models the model can write a short note between tool calls about what it found and
what it does next, addressed to the person watching rather than to itself. The API returns it as
a ``thinking`` block of its own, right before the ``tool_use`` it introduces, and Claude Code
shows it in its terminal as an ordinary message. Its text is the readable summary the API sends
under ``thinking.display`` "updates" (an interactive Claude Code session's default) or
"summarized"; under "omitted" it is empty, and an empty one has nothing to show.

Under "updates" only progress updates carry text, so any thinking block with text is one (the
rule Anthropic's docs give). Under "summarized" every reasoning block carries text too; the block's
``signature`` tells them apart. It is a protobuf message whose field 2 holds a message whose field 1
holds a message whose field 8 is the block kind: ``narration`` for a progress update, ``thinking``
for reasoning. Claude Code classifies blocks by reading the same field. The layout is not a
documented API, so only a signature that positively decodes to ``thinking`` hides a block with
text: if the layout changes, reasoning summaries show (a visible failure, and only with summaries
on) rather than progress updates silently disappearing.
"""

import base64
import binascii
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure

_REASONING_KIND: Final[bytes] = b"thinking"

# The field path from the decoded signature down to the block kind.
_BLOCK_KIND_FIELD_PATH: Final[tuple[int, ...]] = (2, 1, 8)

_WIRE_VARINT: Final[int] = 0
_WIRE_FIXED64: Final[int] = 1
_WIRE_LENGTH_DELIMITED: Final[int] = 2
_WIRE_FIXED32: Final[int] = 5


class _MalformedProtobufError(Exception):
    """The bytes are not a protobuf message this reader can walk."""


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise _MalformedProtobufError("truncated or overlong varint")


def _length_delimited_field(message: bytes, field_number: int) -> bytes | None:
    """The first length-delimited value of ``field_number`` in ``message``, or None when absent."""
    offset = 0
    while offset < len(message):
        key, offset = _read_varint(message, offset)
        number, wire_type = key >> 3, key & 0x7
        if wire_type == _WIRE_VARINT:
            _, offset = _read_varint(message, offset)
        elif wire_type == _WIRE_FIXED64:
            offset += 8
        elif wire_type == _WIRE_FIXED32:
            offset += 4
        elif wire_type == _WIRE_LENGTH_DELIMITED:
            length, offset = _read_varint(message, offset)
            if offset + length > len(message):
                raise _MalformedProtobufError("length past the end of the message")
            if number == field_number:
                return message[offset : offset + length]
            offset += length
        else:
            raise _MalformedProtobufError(f"unsupported wire type {wire_type}")
    return None


@pure
def thinking_block_kind(signature: str) -> bytes | None:
    """The block kind a thinking block's ``signature`` carries, or None when it does not decode."""
    try:
        value: bytes | None = base64.b64decode(signature + "=" * (-len(signature) % 4), validate=True)
        for field_number in _BLOCK_KIND_FIELD_PATH:
            if value is None:
                return None
            value = _length_delimited_field(value, field_number)
    except (binascii.Error, ValueError, _MalformedProtobufError):
        return None
    return value


@pure
def progress_update_text(block: dict[str, Any]) -> str:
    """The text to show for a thinking block with text that is not marked as reasoning, else ""."""
    if block.get("type") != "thinking":
        return ""
    text = block.get("thinking")
    if not isinstance(text, str) or not text.strip():
        return ""
    signature = block.get("signature")
    if isinstance(signature, str) and thinking_block_kind(signature) == _REASONING_KIND:
        return ""
    return text.strip()
