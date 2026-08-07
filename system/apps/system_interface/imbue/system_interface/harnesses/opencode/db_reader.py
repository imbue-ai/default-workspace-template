"""Read an opencode agent's own SQLite conversation store (``opencode.db``).

opencode keeps its whole conversation in a WAL-mode SQLite database at
``<agent_state_dir>/plugin/opencode/data/opencode/opencode.db`` -- three tables whose
content is a JSON blob in a ``data`` column:

* ``message(id, session_id, time_created, time_updated, data)`` -- one row per message;
  ``data`` carries ``role`` and (for an assistant) ``providerID``/``modelID``/``finish``/
  ``tokens``.
* ``part(id, message_id, session_id, time_created, time_updated, data)`` -- one row per
  message part; ``data.type`` is ``text`` / ``tool`` / ``reasoning`` / ``step-*`` / ``patch``.
* ``session(id, parent_id, ...)`` -- one row per conversation (root or subagent).

This module is the thin SQLite-read layer the watcher tails, the opencode analogue of the
"read the native session file" step pi inlines (pi's JSONL is trivial; a DB needs a reader).
It is pure of any watcher state: given a db path + the root session id + a ``time_updated``
watermark, it returns the rows that changed. Every access is READ-ONLY (``mode=ro`` URI, so a
live agent's writer is never disturbed) and wrapped so a transient WAL lock, a missing file, or
a malformed ``data`` blob degrades to "nothing this pass" rather than raising -- the same
resilience the antigravity DB watcher and ``opencode_config._db_has_session`` rely on.

We reimplement the path literals here rather than import ``mngr_opencode.opencode_config``:
that lives in a separate package the system interface does not depend on, exactly as the
codex/claude parsers reimplement their plugins' constants.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel

# The agent's native opencode store, relative to its state dir (under the per-agent
# XDG_DATA_HOME). Kept in sync with mngr_opencode.opencode_config (NATIVE_DB_RELATIVE_PATH /
# the _DATA_HOME_RELATIVE_PATH used by harnesses.opencode.model).
OPENCODE_DB_RELPATH: Path = Path("plugin") / "opencode" / "data" / "opencode" / "opencode.db"

# The file recording the agent's ROOT opencode session id (written by opencode_launch.sh).
# Kept in sync with mngr_opencode.opencode_config.ROOT_SESSION_FILENAME.
ROOT_SESSION_RELPATH: Path = Path("opencode_root_session")

# part.data.type values that carry conversation content we surface. Everything else
# (``step-start``/``step-finish`` bookkeeping, ``patch`` post-edit summaries with no callID,
# ``reasoning`` thinking) is dropped by the parser -- see session_parser.
PART_TYPE_TEXT: str = "text"
PART_TYPE_TOOL: str = "tool"

# A tool part's ``state.status`` values that mean the call has produced a result.
TERMINAL_TOOL_STATUSES: frozenset[str] = frozenset({"completed", "error"})


class OpenCodeMessage(FrozenModel):
    """One ``message`` row: the stable ids/timestamps (columns) plus its parsed ``data``."""

    id: str
    session_id: str
    time_created: int
    time_updated: int
    role: str
    provider_id: str | None
    model_id: str | None
    finish: str | None
    # ``data.time.completed`` (ms epoch), set once the message is done generating -- or None
    # while it is still streaming. This is the settle signal (``finish`` is only on the FINAL
    # assistant message of a turn; an intermediate tool-call message has ``finish=None`` but a
    # real ``time.completed``, so keying settle on ``finish`` would keep it hot forever).
    completed: int | None
    # opencode's per-message token accounting (``data.tokens``), or None when absent. Surfaced
    # as the common ``usage`` shape by the parser, mirroring pi (which emits usage too).
    tokens: dict[str, Any] | None


class OpenCodePart(FrozenModel):
    """One ``part`` row: stable ids/timestamps plus the type-specific payload from ``data``."""

    id: str
    message_id: str
    session_id: str
    time_created: int
    time_updated: int
    kind: str
    # text part
    text: str
    synthetic: bool
    # tool part
    tool_name: str
    call_id: str
    state_status: str
    state_input: dict[str, Any]
    state_output: str
    state_error: str


def opencode_db_path(agent_state_dir: Path) -> Path:
    """The agent's ``opencode.db`` path."""
    return agent_state_dir / OPENCODE_DB_RELPATH


def read_root_session_id(agent_state_dir: Path) -> str | None:
    """The agent's root opencode session id from its marker, or None when absent/blank."""
    try:
        session_id = (agent_state_dir / ROOT_SESSION_RELPATH).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return session_id or None


def _as_dict(raw: str) -> dict[str, Any] | None:
    """Parse a ``data`` column value as a JSON object, or None when it is not one."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _message_from_row(row: tuple[Any, ...]) -> OpenCodeMessage | None:
    """Build an :class:`OpenCodeMessage` from a ``(id, session_id, time_created, time_updated,
    data)`` row, or None when the ``data`` blob is unusable."""
    message_id, session_id, time_created, time_updated, raw = row
    data = _as_dict(raw)
    if data is None:
        logger.warning("opencode db: skipping message {} with unparseable data", message_id)
        return None
    role = data.get("role")
    if not isinstance(role, str) or not role:
        return None
    provider_id = data.get("providerID")
    model_id = data.get("modelID")
    finish = data.get("finish")
    tokens = data.get("tokens")
    time_block = data.get("time")
    completed = time_block.get("completed") if isinstance(time_block, dict) else None
    return OpenCodeMessage(
        id=str(message_id),
        session_id=str(session_id),
        time_created=int(time_created),
        time_updated=int(time_updated),
        role=role,
        provider_id=provider_id if isinstance(provider_id, str) and provider_id else None,
        model_id=model_id if isinstance(model_id, str) and model_id else None,
        finish=finish if isinstance(finish, str) and finish else None,
        completed=int(completed) if isinstance(completed, (int, float)) else None,
        tokens=tokens if isinstance(tokens, dict) else None,
    )


def _string(value: Any) -> str:
    """A value coerced to a display string: a str as-is, anything else JSON-encoded, '' for None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _part_from_row(row: tuple[Any, ...]) -> OpenCodePart | None:
    """Build an :class:`OpenCodePart` from a ``(id, message_id, session_id, time_created,
    time_updated, data)`` row, or None when the ``data`` blob is unusable."""
    part_id, message_id, session_id, time_created, time_updated, raw = row
    data = _as_dict(raw)
    if data is None:
        logger.warning("opencode db: skipping part {} with unparseable data", part_id)
        return None
    kind = data.get("type")
    if not isinstance(kind, str) or not kind:
        return None

    text = data.get("text")
    state = data.get("state")
    state = state if isinstance(state, dict) else {}
    state_input = state.get("input")
    # A tool result lives in ``state.output``; an errored one carries ``state.error`` and its
    # status is ``error``. Both are coerced to a display string (opencode sometimes stores a
    # structured value).
    return OpenCodePart(
        id=str(part_id),
        message_id=str(message_id),
        session_id=str(session_id),
        time_created=int(time_created),
        time_updated=int(time_updated),
        kind=kind,
        text=text if isinstance(text, str) else "",
        synthetic=bool(data.get("synthetic", False)),
        tool_name=str(data.get("tool") or ""),
        call_id=str(data.get("callID") or ""),
        state_status=str(state.get("status") or ""),
        state_input=state_input if isinstance(state_input, dict) else {},
        state_output=_string(state.get("output")),
        state_error=_string(state.get("error")),
    )


# Root-session messages touched since the cursor, OR owning a part touched since the cursor.
# A message's events depend on ALL its parts, so any part change re-surfaces the whole message
# -- the ``time_updated >= cursor`` (inclusive) form never skips a same-millisecond update; the
# watcher's content-supersession dedup makes a re-read of an unchanged row a harmless no-op.
_CHANGED_MESSAGES_SQL = (
    "SELECT m.id, m.session_id, m.time_created, m.time_updated, m.data "
    "FROM message m "
    "WHERE m.session_id = ? "
    "AND (m.time_updated >= ? "
    "     OR EXISTS (SELECT 1 FROM part p WHERE p.message_id = m.id AND p.time_updated >= ?)) "
    "ORDER BY m.time_created, m.id"
)

_PARTS_FOR_MESSAGES_SQL_TEMPLATE = (
    "SELECT id, message_id, session_id, time_created, time_updated, data "
    "FROM part WHERE message_id IN ({placeholders}) ORDER BY message_id, id"
)


def read_changed_messages(
    db_path: Path, root_session_id: str, since_updated: int
) -> tuple[list[OpenCodeMessage], dict[str, list[OpenCodePart]]]:
    """Root-session messages whose own or whose child part's ``time_updated`` is >=
    ``since_updated``, each with ALL its current parts.

    Subagents are disabled (opencode runs without them), so this filters on the single root
    ``session_id`` -- there is no ``parent_id`` walk. Read-only and defensive: a missing/locked
    db or a malformed row degrades to ``([], {})`` / a skipped row rather than raising, so a
    transient WAL lock is simply retried on the next poll.
    """
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        # Dominated by the benign "file does not exist yet" case (agent has not run a turn).
        return [], {}
    try:
        message_rows = connection.execute(
            _CHANGED_MESSAGES_SQL, (root_session_id, since_updated, since_updated)
        ).fetchall()
        messages = [message for row in message_rows if (message := _message_from_row(row)) is not None]
        if not messages:
            return [], {}
        message_ids = [message.id for message in messages]
        placeholders = ",".join("?" for _ in message_ids)
        part_rows = connection.execute(
            _PARTS_FOR_MESSAGES_SQL_TEMPLATE.format(placeholders=placeholders), message_ids
        ).fetchall()
    except sqlite3.Error as exc:
        # File exists but is locked mid-checkpoint or malformed: retry next poll.
        logger.debug("opencode db read failed for {} (retried next poll): {}", db_path, exc)
        return [], {}
    finally:
        connection.close()

    parts_by_message: dict[str, list[OpenCodePart]] = {message.id: [] for message in messages}
    for row in part_rows:
        part = _part_from_row(row)
        if part is not None and part.message_id in parts_by_message:
            parts_by_message[part.message_id].append(part)
    return messages, parts_by_message


def is_message_settled(message: OpenCodeMessage, parts: list[OpenCodePart]) -> bool:
    """Whether ``message`` is done streaming, so the tail cursor may advance past it.

    A user message is always settled. An assistant message is settled once it is done
    generating (``time.completed`` is set) AND every tool part has produced a result (status in
    ``completed``/``error``) -- until then its text/tool parts may still update in place, so it
    stays "hot" and is re-read every poll (the antigravity settle rule, on ``time_updated``).
    ``time.completed`` -- not ``finish`` -- is the generation-done signal: ``finish`` is only on
    the FINAL assistant message of a turn, so an intermediate tool-call message would otherwise
    never settle.
    """
    if message.role == "user":
        return True
    if message.role != "assistant":
        return True
    if message.completed is None:
        return False
    return all(part.state_status in TERMINAL_TOOL_STATUSES for part in parts if part.kind == PART_TYPE_TOOL)
