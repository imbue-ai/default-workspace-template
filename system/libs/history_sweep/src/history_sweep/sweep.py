"""Find and remove what deleted chats leave on disk, never anything a listed chat can open.

Deleting a chat destroys its agents. mngr keeps a preserved archive of each one under
``<host dir>/preserved/`` (its raw transcript, the common transcript agents search with
``mngr transcript``, and its session-id history), and claude's own session files for the
agent stay behind in the account folder, where nothing can open them any more.

The sweep removes, for an archive whose agent is neither present nor named by a chat
record:

- at once, the agent's session files in the account folders, once the archive holds a
  non-empty raw transcript (the same lines) whose final flush and copy mngr did not report failing;
- after the retention window, the archive itself and any session files still left.

A session id that a present or listed agent's history also names is never touched.
"""

import json
import os
import re
import shutil
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.preservation import (
    PreservationOutcome,
    PreservedAgentArchive,
    TranscriptFlushOutcome,
    discover_preserved_agent_archives,
)
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from pydantic import Field

RETENTION: Final[timedelta] = timedelta(days=30)

SESSION_ID_HISTORY_FILENAME: Final[str] = "claude_session_id_history"
_PRESERVED_RAW_TRANSCRIPT: Final[Path] = Path("logs/claude_transcript/events.jsonl")
# The producer whose final pass at destroy brings the raw transcript up to the session files' last line.
_RAW_TRANSCRIPT_FLUSH_SCRIPT: Final[str] = "stream_transcript.sh"
_CHAT_RECORD_FILENAME: Final[str] = "record.json"
# Session ids are UUIDs; anything else in a history line is never joined into a path.
_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


class SweepRefusedError(Exception):
    """The sweep cannot tell what is still listed, so it removes nothing."""


class RemovalReason(UpperCaseStrEnum):
    ORPHANED_SESSION_COPY = auto()
    EXPIRED_ARCHIVE = auto()


class SweepRoots(FrozenModel):
    host_dir: Path = Field(
        description="mngr's host directory (agents/ and preserved/ live under it)"
    )
    accounts_root: Path = Field(description="Where the provider account folders live")
    chats_root: Path = Field(
        description="The chat app's chat records (data/.apps/chat/chats)"
    )


class Removal(FrozenModel):
    path: Path = Field(description="The file or directory to remove")
    size_bytes: int = Field(
        ge=0, description="Bytes it holds on disk, by apparent size"
    )
    reason: RemovalReason = Field(description="Why it goes")
    agent_id: AgentId = Field(description="The destroyed agent it belonged to")


def plan_sweep(
    roots: SweepRoots, now: datetime, retention: timedelta = RETENTION
) -> tuple[Removal, ...]:
    """Everything the sweep would remove now, without removing anything."""
    present_agent_dirs = _present_agent_dirs(roots.host_dir)
    protected_agent_ids = {
        path.name for path in present_agent_dirs
    } | _listed_agent_ids(roots.chats_root)
    try:
        archives = discover_preserved_agent_archives(roots.host_dir)
    except MngrError as e:
        raise SweepRefusedError(f"cannot read the preserved archives: {e}") from e
    unprotected_archives = [
        archive
        for archive in archives
        if str(archive.agent_id) not in protected_agent_ids
    ]
    protected_history_files = [
        agent_dir / SESSION_ID_HISTORY_FILENAME for agent_dir in present_agent_dirs
    ] + [
        archive.path / SESSION_ID_HISTORY_FILENAME
        for archive in archives
        if str(archive.agent_id) in protected_agent_ids
    ]
    protected_session_ids: set[str] = set()
    for history_file in protected_history_files:
        protected_session_ids.update(read_session_ids(history_file))

    removals: list[Removal] = []
    # A clone carries its source's session history, so two deleted agents can name one session.
    planned_copies: set[Path] = set()
    for archive in unprotected_archives:
        agent_id = archive.agent_id
        session_ids = (
            read_session_ids(archive.path / SESSION_ID_HISTORY_FILENAME)
            - protected_session_ids
        )
        session_copies = [
            path
            for path in _session_copies(roots.accounts_root, session_ids)
            if path not in planned_copies
        ]
        is_expired = now - _preserved_at(archive) >= retention
        if is_expired or _archive_holds_session_lines(archive):
            planned_copies.update(session_copies)
            removals.extend(
                _removal(path, RemovalReason.ORPHANED_SESSION_COPY, agent_id)
                for path in session_copies
            )
        if is_expired:
            removals.append(
                _removal(archive.path, RemovalReason.EXPIRED_ARCHIVE, agent_id)
            )
    return tuple(removals)


def apply_sweep(
    removals: Iterable[Removal],
) -> tuple[tuple[Removal, ...], tuple[tuple[Removal, OSError], ...]]:
    """Remove each planned path; returns what was removed and what could not be.

    An expired archive is kept when one of its agent's session copies could not be removed: its
    session-id history is the only thing naming them, so the next sweep can still find them.
    """
    removed: list[Removal] = []
    failed: list[tuple[Removal, OSError]] = []
    first_error_by_agent: dict[AgentId, OSError] = {}
    for removal in removals:
        earlier_error = first_error_by_agent.get(removal.agent_id)
        if (
            removal.reason == RemovalReason.EXPIRED_ARCHIVE
            and earlier_error is not None
        ):
            failed.append((removal, earlier_error))
            continue
        try:
            if removal.path.is_dir() and not removal.path.is_symlink():
                shutil.rmtree(removal.path)
            else:
                removal.path.unlink(missing_ok=True)
        except OSError as e:
            failed.append((removal, e))
            first_error_by_agent.setdefault(removal.agent_id, e)
        else:
            removed.append(removal)
    return tuple(removed), tuple(failed)


def read_session_ids(history_file: Path) -> set[str]:
    """The session ids a claude_session_id_history names (the first word of each line)."""
    try:
        lines = history_file.read_text().splitlines()
    except FileNotFoundError:
        return set()
    session_ids: set[str] = set()
    for line in lines:
        words = line.split()
        if words and _SESSION_ID_PATTERN.match(words[0]):
            session_ids.add(words[0])
    return session_ids


def _present_agent_dirs(host_dir: Path) -> tuple[Path, ...]:
    agents_root = host_dir / "agents"
    if not agents_root.is_dir():
        return ()
    return tuple(path for path in agents_root.iterdir() if path.is_dir())


def _listed_agent_ids(chats_root: Path) -> set[str]:
    """Every agent a chat record names: its agents, and the successor its handoff is making.

    An unreadable record refuses the sweep.
    """
    if not chats_root.is_dir():
        return set()
    agent_ids: set[str] = set()
    for chat_dir in chats_root.iterdir():
        record_file = chat_dir / _CHAT_RECORD_FILENAME
        if not record_file.is_file():
            continue
        try:
            record = json.loads(record_file.read_text())
            agent_ids.update(str(entry["agent_id"]) for entry in record["agents"])
            handoff = record.get("handoff")
            if handoff is not None:
                agent_ids.add(str(handoff["next_agent_id"]))
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise SweepRefusedError(
                f"cannot read chat record {record_file}: {e}"
            ) from e
    return agent_ids


def _session_copies(accounts_root: Path, session_ids: set[str]) -> tuple[Path, ...]:
    """The session files and per-session folders claude keeps for these sessions, in every account."""
    if not session_ids or not accounts_root.is_dir():
        return ()
    copies: list[Path] = []
    for account_dir in sorted(accounts_root.iterdir()):
        if not account_dir.is_dir():
            continue
        project_dirs = sorted(
            path for path in (account_dir / "projects").glob("*") if path.is_dir()
        )
        for session_id in sorted(session_ids):
            for project_dir in project_dirs:
                copies.extend(
                    path
                    for path in (
                        project_dir / f"{session_id}.jsonl",
                        project_dir / session_id,
                    )
                    if path.exists()
                )
            file_history = account_dir / "file-history" / session_id
            if file_history.exists():
                copies.append(file_history)
    return tuple(copies)


def _preserved_at(archive: PreservedAgentArchive) -> datetime:
    if archive.manifest is not None:
        return archive.manifest.preserved_at
    return datetime.fromtimestamp(archive.path.stat().st_mtime, tz=timezone.utc)


def _archive_holds_session_lines(archive: PreservedAgentArchive) -> bool:
    """Whether the archive's raw transcript holds what the session files do: it is non-empty, and the
    manifest records no failed final flush of it and no failed copy of it."""
    if not _has_preserved_raw_transcript(archive.path):
        return False
    if archive.manifest is None:
        return True
    is_flush_failed = any(
        flush.script_name == _RAW_TRANSCRIPT_FLUSH_SCRIPT
        and flush.outcome == TranscriptFlushOutcome.ERROR
        for flush in archive.manifest.transcript_flush
    )
    is_copy_failed = any(
        item.outcome == PreservationOutcome.ERROR
        and (
            Path(item.rel_path) == _PRESERVED_RAW_TRANSCRIPT
            or Path(item.rel_path) in _PRESERVED_RAW_TRANSCRIPT.parents
        )
        for item in archive.manifest.items
    )
    return not (is_flush_failed or is_copy_failed)


def _has_preserved_raw_transcript(archive_dir: Path) -> bool:
    try:
        return (archive_dir / _PRESERVED_RAW_TRANSCRIPT).stat().st_size > 0
    except FileNotFoundError:
        return False


def _removal(path: Path, reason: RemovalReason, agent_id: AgentId) -> Removal:
    return Removal(
        path=path, size_bytes=_size_bytes(path), reason=reason, agent_id=agent_id
    )


def _size_bytes(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for dir_path, _dir_names, file_names in os.walk(path):
        for file_name in file_names:
            total += (Path(dir_path) / file_name).lstat().st_size
    return total
