"""Test utilities for the opencode harness: a synthetic ``opencode.db`` builder and in-place
row mutators, so the reader/parser/watcher tests need no live opencode.

Models exactly how opencode writes: a ``session``/``message``/``part`` schema with the content
as a JSON ``data`` column (the three-table shape verified live), plus helpers that append rows
and update a message/part in place (bumping ``time_updated``), which is how a streaming turn
settles. ``build_opencode_agent_dir`` lays out the per-agent state dir the watcher expects
(the db under ``plugin/opencode/data/opencode/`` and the ``opencode_root_session`` marker).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from imbue.system_interface.harnesses.opencode.db_reader import OPENCODE_DB_RELPATH
from imbue.system_interface.harnesses.opencode.db_reader import ROOT_SESSION_RELPATH

# The three tables the reader touches, matching opencode's live schema (only the columns the
# reader reads are modelled; the real db has more).
_SCHEMA = (
    "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, time_created INTEGER);",
    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
    "time_updated INTEGER, data TEXT);",
    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
    "time_created INTEGER, time_updated INTEGER, data TEXT);",
)


def create_opencode_db(db_path: Path, root_session_id: str) -> None:
    """Create an empty opencode-shaped db with one root ``session`` row."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO session (id, parent_id, time_created) VALUES (?, NULL, ?)", (root_session_id, 0)
        )
        connection.commit()
    finally:
        connection.close()


def insert_message(
    db_path: Path,
    *,
    message_id: str,
    session_id: str,
    role: str,
    time_created: int,
    time_updated: int | None = None,
    data_extra: dict[str, Any] | None = None,
) -> None:
    """Insert a ``message`` row. ``data_extra`` merges into the JSON ``data`` (e.g. a user
    ``{"model": {...}}`` or an assistant ``{"providerID","modelID","finish","time","tokens"}``)."""
    data: dict[str, Any] = {"role": role}
    if data_extra:
        data.update(data_extra)
    _insert(
        db_path,
        "message",
        (
            message_id,
            session_id,
            time_created,
            time_updated if time_updated is not None else time_created,
            json.dumps(data),
        ),
    )


def insert_part(
    db_path: Path,
    *,
    part_id: str,
    message_id: str,
    session_id: str,
    time_created: int,
    time_updated: int | None = None,
    data: dict[str, Any],
) -> None:
    """Insert a ``part`` row with the given ``data`` JSON (a ``text``/``tool``/``reasoning``/... part)."""
    _insert(
        db_path,
        "part",
        (
            part_id,
            message_id,
            session_id,
            time_created,
            time_updated if time_updated is not None else time_created,
            json.dumps(data),
        ),
    )


def update_part(db_path: Path, *, part_id: str, time_updated: int, data: dict[str, Any]) -> None:
    """Rewrite a part's ``data`` + ``time_updated`` in place (a streaming part growing / a tool
    going running -> completed)."""
    _execute(
        db_path, "UPDATE part SET data = ?, time_updated = ? WHERE id = ?", (json.dumps(data), time_updated, part_id)
    )


def update_message(db_path: Path, *, message_id: str, time_updated: int, data_extra: dict[str, Any]) -> None:
    """Merge ``data_extra`` into a message's ``data`` and bump ``time_updated`` in place (an
    assistant message gaining its ``time.completed`` / ``finish`` when it settles)."""
    current = _fetch_one(db_path, "SELECT data FROM message WHERE id = ?", (message_id,))
    data = json.loads(current) if current else {}
    data.update(data_extra)
    _execute(
        db_path,
        "UPDATE message SET data = ?, time_updated = ? WHERE id = ?",
        (json.dumps(data), time_updated, message_id),
    )


def text_part_data(text: str, *, synthetic: bool = False) -> dict[str, Any]:
    """A ``text`` part's ``data``."""
    data: dict[str, Any] = {"type": "text", "text": text}
    if synthetic:
        data["synthetic"] = True
    return data


def tool_part_data(
    *,
    tool: str,
    call_id: str,
    status: str,
    tool_input: dict[str, Any] | None = None,
    output: str = "",
    error: str = "",
) -> dict[str, Any]:
    """A ``tool`` part's ``data`` with its ``state`` (status ``pending``/``running``/``completed``/``error``)."""
    state: dict[str, Any] = {"status": status, "input": tool_input or {}}
    if output:
        state["output"] = output
    if error:
        state["error"] = error
    return {"type": "tool", "tool": tool, "callID": call_id, "state": state}


def assistant_done_data(
    *, provider_id: str, model_id: str, finish: str, completed: int, tokens: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``data_extra`` an assistant message carries once it has settled (done generating)."""
    data: dict[str, Any] = {
        "providerID": provider_id,
        "modelID": model_id,
        "finish": finish,
        "time": {"completed": completed},
    }
    if tokens is not None:
        data["tokens"] = tokens
    return data


def build_opencode_agent_dir(state_dir: Path, root_session_id: str) -> Path:
    """Lay out an agent state dir with an empty opencode db + the root-session marker, and
    return the db path."""
    (state_dir / ROOT_SESSION_RELPATH).write_text(root_session_id, encoding="utf-8")
    db_path = state_dir / OPENCODE_DB_RELPATH
    create_opencode_db(db_path, root_session_id)
    return db_path


def _insert(db_path: Path, table: str, row: tuple[Any, ...]) -> None:
    placeholders = ",".join("?" for _ in row)
    _execute(db_path, f"INSERT INTO {table} VALUES ({placeholders})", row)


def _execute(db_path: Path, sql: str, params: tuple[Any, ...]) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _fetch_one(db_path: Path, sql: str, params: tuple[Any, ...]) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        connection.close()
