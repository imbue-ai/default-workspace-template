"""Test utilities for the antigravity harness: a tiny protobuf encoder for building
``steps`` payload fixtures, and a synthetic conversation-``.db`` builder. Used by the
decoder, parser, and watcher tests so no live agy ``.db`` is needed.

The field numbers mirror ``agy_transcript``'s recovered map; if that map changes, these
builders change with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def encode_varint(value: int) -> bytes:
    out = bytearray()
    remaining = value
    has_more = True
    while has_more:
        byte = remaining & 0x7F
        remaining >>= 7
        has_more = remaining != 0
        out.append(byte | (0x80 if has_more else 0))
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


def uint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + encode_varint(value)


def len_field(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + encode_varint(len(value)) + value


def str_field(field: int, text: str) -> bytes:
    return len_field(field, text.encode("utf-8"))


def build_metadata(seconds: int = 1_700_000_000, *, source: int = 4, extra: bytes = b"") -> bytes:
    """A CortexStepMetadata: created_at (f1 = Timestamp{f1 seconds}), source (f3), + extra."""
    created_at = uint_field(1, seconds)
    return len_field(1, created_at) + uint_field(3, source) + extra


def build_tool_metadata(name: str, args: str, *, call_id: str = "abc123", short: str = "", long: str = "") -> bytes:
    """Metadata carrying a ChatToolCall (f4) + optional captions (f30/f31)."""
    call = str_field(1, call_id) + str_field(2, name) + str_field(3, args)
    extra = len_field(4, call)
    if short:
        extra += str_field(30, short)
    if long:
        extra += str_field(31, long)
    return build_metadata(source=2, extra=extra)


def build_step_payload(metadata: bytes, body: bytes = b"") -> bytes:
    """A Step payload: metadata (f5) + a body field. step_type/status come from columns, so
    they are not encoded here."""
    return len_field(5, metadata) + body


def build_steps_db(path: Path, rows: list[tuple[int, int, int, bytes]]) -> None:
    """Create a minimal agy-shaped conversation ``.db`` at ``path`` with the given
    ``(idx, step_type, status, step_payload)`` rows."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, step_payload BLOB)"
        )
        connection.executemany("INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def append_step(path: Path, row: tuple[int, int, int, bytes]) -> None:
    """Append one row to an existing steps ``.db`` (simulating agy writing a new step)."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?, ?, ?, ?)", row)
        connection.commit()
    finally:
        connection.close()


def set_step_status(path: Path, idx: int, status: int, step_payload: bytes) -> None:
    """Update a row's status + payload in place (simulating a RUNNING step settling)."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE steps SET status = ?, step_payload = ? WHERE idx = ?", (status, step_payload, idx))
        connection.commit()
    finally:
        connection.close()
