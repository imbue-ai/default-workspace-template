"""Test utilities for the antigravity harness: a tiny protobuf encoder for building
``steps`` payload fixtures, and a synthetic conversation-``.db`` builder. Used by the
decoder, parser, and watcher tests so no live agy ``.db`` is needed.

The field numbers mirror ``agy_transcript``'s recovered map; if that map changes, these
builders change with it.
"""

from __future__ import annotations

import base64
import json
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


def build_tool_metadata(name: str, args: str, *, call_id: str = "abc123") -> bytes:
    """Metadata carrying a ChatToolCall (f4).

    agy declares caption fields at f30/f31 but never populates them (0 of 41 rows on two live
    stores); the captions live in the step BODY. Use ``build_tool_body`` for those.
    """
    call = str_field(1, call_id) + str_field(2, name) + str_field(3, args)
    return build_metadata(source=2, extra=len_field(4, call))


def build_tool_body(
    *, result: str = "", tool_summary: str = "", tool_action: str = "", args: dict[str, str] | None = None
) -> bytes:
    """A tool step's BODY (f140), in agy's real shape.

    Repeated ``{key, value}`` argument pairs on f1 -- which is where the tool's own arguments
    AND agy's ``toolSummary``/``toolAction`` captions live -- plus a result container on f2
    whose f1 is the command's output. Mirrors what the live stores contain; see
    ``docs/design/antigravity-transcript-schema.md``.
    """
    pairs = dict(args or {})
    if tool_summary:
        pairs["toolSummary"] = tool_summary
    if tool_action:
        pairs["toolAction"] = tool_action
    body = b"".join(len_field(1, str_field(1, k) + str_field(2, v)) for k, v in pairs.items())
    if result:
        body += len_field(2, str_field(1, result))
    return len_field(140, body)


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


def load_captured_step(name: str) -> tuple[int, int, bytes]:
    """A REAL agy step row captured from a live conversation store.

    Returns ``(step_type, status, payload)``. See ``fixtures/README.md`` -- synthetic payloads
    built by the helpers above cannot reproduce the shapes that broke the decoder, so anything
    asserting on decoding behaviour should use one of these instead.
    """
    captured = json.loads((Path(__file__).parent / "fixtures" / "agy_steps.json").read_text())
    row = captured[name]
    return row["step_type"], row["status"], base64.b64decode(row["payload_b64"])
