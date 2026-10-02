"""Which chat wrote each note, and how many chats have read it, from the chats' own transcripts.

A note file records nothing reliable about who wrote it, but every chat's transcript records each tool call it made.
Claude: every ``Write``/``Edit`` of a file in the notes folder is a write by that session and every ``Read`` a read,
and a session belongs to the agent whose ``claude_session_id_history`` lists it (under the mngr host dir). pi: its
sessions live under each agent's own folder (``agents/<id>/plugin/pi_coding/sessions/``), so the agent is known
directly, and its ``write``/``edit``/``read`` tool calls count the same way. An agent belongs to the chat whose
``agent_ids`` hold it (the chat app's ``GET /api/chats``). A session no agent claims, or an agent no live chat holds
(a deleted chat), is still counted, just without a chat's name.

Transcripts are read when the page asks, decoding and parsing only the lines that name the notes folder (see
``lines_containing``).
"""

import json
import os
import time
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset({"Write", "Edit", "MultiEdit"})
READ_TOOL_NAMES: Final[frozenset[str]] = frozenset({"Read"})
PI_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset({"write", "edit"})
PI_READ_TOOL_NAMES: Final[frozenset[str]] = frozenset({"read"})
PI_SESSIONS_SUBPATH: Final[str] = "plugin/pi_coding/sessions"
# A pi session's key in the session-to-agent mapping: its agent is known from the folder it sits in.
PI_SESSION_PREFIX: Final[str] = "pi:"
SESSION_HISTORY_FILENAME: Final[str] = "claude_session_id_history"
TRANSCRIPT_SUFFIX: Final[str] = ".jsonl"
# A pi session's header line, which says the directory relative paths in its tool calls are relative to.
_PI_LINE_MARKERS: Final[tuple[bytes, ...]] = (b'"type":"session"', b'"type": "session"')
DEFAULT_CHAT_APP_URL: Final[str] = "http://127.0.0.1:8010"
CHAT_LIST_TIMEOUT_SECONDS: Final[float] = 5.0
CHAT_LIST_SLOW_SECONDS: Final[float] = 1.0
# Transcripts are read in chunks this size, so memory stays bounded whatever a transcript's length.
TRANSCRIPT_CHUNK_BYTES: Final[int] = 4 * 1024 * 1024


class NoteToolUse(FrozenModel):
    """One tool call a session made on a note."""

    session_id: str = Field(description="The session that made it: a Claude session id, or ``pi:<agent id>``")
    file_name: str = Field(description="The note's file name")
    is_write: bool = Field(description="A write or edit, as opposed to a read")
    at: datetime | None = Field(description="When, from the transcript line's timestamp")


class AuthorKind(UpperCaseStrEnum):
    """What is known about whoever wrote a note."""

    # A live chat, by its title.
    CHAT = auto()
    # An agent no live chat holds: a chat since deleted, or a background task a chat started.
    NOT_A_CHAT = auto()
    # Not knowable: the chat app could not be asked, or the session belongs to no agent mngr knows.
    UNKNOWN = auto()


class NoteAuthor(FrozenModel):
    """Whoever wrote a note, as far as the transcripts and the chat app can tell."""

    kind: AuthorKind = Field(description="What is known about the writer")
    chat_title: str | None = Field(description="The chat's title, for a writer of kind CHAT")
    at: datetime | None = Field(description="When it last wrote the note")


class NoteAttribution(FrozenModel):
    """Who wrote a note and how many chats read it."""

    authors: tuple[NoteAuthor, ...] = Field(description="Every chat that wrote it, most recent first")
    reader_count: int = Field(description="How many distinct chats (or unclaimed sessions) read it")


class TranscriptSources(FrozenModel):
    """Where the records come from; injectable so tests read fake trees."""

    claude_config_dirs: tuple[Path, ...] = Field(description="Claude config dirs holding ``projects/`` transcripts")
    mngr_agents_dir: Path = Field(description="The mngr host dir's ``agents/`` folder")
    notes_dir: Path = Field(description="The notes folder, absolute")


def default_transcript_sources(notes_dir: Path) -> TranscriptSources:
    home = Path.home()
    accounts_dir = home / ".minds" / "accounts"
    account_dirs = sorted(path for path in accounts_dir.iterdir() if path.is_dir()) if accounts_dir.is_dir() else []
    host_dir = Path(os.environ.get("MNGR_HOST_DIR") or home / ".mngr")
    return TranscriptSources(
        claude_config_dirs=(*account_dirs, home / ".claude"),
        mngr_agents_dir=host_dir / "agents",
        notes_dir=notes_dir.absolute(),
    )


@pure
def matching_lines(block: bytes, markers: Sequence[bytes]) -> list[str]:
    """The lines of ``block`` that contain any of ``markers``, in order, each once, decoded as UTF-8."""
    spans: set[tuple[int, int]] = set()
    for marker in markers:
        found = block.find(marker)
        while found != -1:
            start = block.rfind(b"\n", 0, found) + 1
            newline = block.find(b"\n", found)
            end = len(block) if newline == -1 else newline
            spans.add((start, end))
            found = block.find(marker, end)
    return [block[start:end].decode("utf-8", errors="replace") for start, end in sorted(spans)]


def lines_containing(path: Path, markers: Sequence[bytes], chunk_bytes: int) -> list[str]:
    """The lines of the file at ``path`` that contain any of ``markers``.

    Almost no transcript line names the notes folder, and decoding every line to text costs far more than searching
    bytes, so only matching lines are decoded. The file is read a chunk at a time; a line longer than a chunk (a
    transcript can hold multi-MB lines, such as images) is collected in pieces and joined once, when it ends.
    """
    lines: list[str] = []
    pieces: list[bytes] = []
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            cut = chunk.rfind(b"\n") + 1
            if cut == 0:
                pieces.append(chunk)
                continue
            lines.extend(matching_lines(b"".join([*pieces, chunk[:cut]]), markers))
            pieces = [chunk[cut:]]
    lines.extend(matching_lines(b"".join(pieces), markers))
    return lines


@pure
def note_tool_uses(line: str, session_id: str, notes_dir: Path) -> list[NoteToolUse]:
    """The note tool calls in one transcript line (none for most lines)."""
    try:
        record = json.loads(line)
    except ValueError:
        return []
    message = record.get("message") if isinstance(record, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    at = _parse_timestamp(record.get("timestamp"))
    uses: list[NoteToolUse] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "tool_use":
            continue
        name = item.get("name")
        tool_input: Any = item.get("input")
        file_path = tool_input.get("file_path") if isinstance(tool_input, Mapping) else None
        if name not in WRITE_TOOL_NAMES | READ_TOOL_NAMES or not isinstance(file_path, str):
            continue
        path = Path(os.path.expanduser(file_path))
        if path.parent != notes_dir:
            continue
        uses.append(NoteToolUse(session_id=session_id, file_name=path.name, is_write=name in WRITE_TOOL_NAMES, at=at))
    return uses


def read_note_tool_uses(sources: TranscriptSources) -> list[NoteToolUse]:
    """Every Claude session's note tool calls, from every project's transcripts: a worker runs in a worktree of its
    own, a project folder of its own, and still writes to the one notes folder."""
    notes_marker = str(sources.notes_dir.relative_to(sources.notes_dir.parent.parent))
    uses: list[NoteToolUse] = []
    for config_dir in sources.claude_config_dirs:
        for transcript in sorted((config_dir / "projects").glob(f"*/*{TRANSCRIPT_SUFFIX}")):
            try:
                lines = lines_containing(transcript, (notes_marker.encode(),), TRANSCRIPT_CHUNK_BYTES)
            except OSError as e:
                logger.debug("Skipped transcript {}: {}", transcript, e)
                continue
            for line in lines:
                if '"tool_use"' in line:
                    uses.extend(note_tool_uses(line, transcript.stem, sources.notes_dir))
    return uses


@pure
def pi_note_tool_uses(line: str, session_key: str, notes_dir: Path, cwd: Path) -> list[NoteToolUse]:
    """The note tool calls in one pi session line: an assistant message's ``toolCall`` items."""
    try:
        record = json.loads(line)
    except ValueError:
        return []
    message = record.get("message") if isinstance(record, Mapping) else None
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    at = _parse_timestamp(record.get("timestamp"))
    uses: list[NoteToolUse] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "toolCall":
            continue
        name = item.get("name")
        arguments: Any = item.get("arguments")
        raw_path = arguments.get("path") if isinstance(arguments, Mapping) else None
        if name not in PI_WRITE_TOOL_NAMES | PI_READ_TOOL_NAMES or not isinstance(raw_path, str):
            continue
        path = Path(os.path.expanduser(raw_path))
        if not path.is_absolute():
            path = cwd / path
        if path.parent != notes_dir:
            continue
        uses.append(
            NoteToolUse(session_id=session_key, file_name=path.name, is_write=name in PI_WRITE_TOOL_NAMES, at=at)
        )
    return uses


def read_pi_note_tool_uses(mngr_agents_dir: Path, notes_dir: Path) -> tuple[list[NoteToolUse], dict[str, str]]:
    """Every pi session's note tool calls, and each pi session key's agent id."""
    uses: list[NoteToolUse] = []
    agent_id_by_session: dict[str, str] = {}
    if not mngr_agents_dir.is_dir():
        return uses, agent_id_by_session
    for agent_dir in sorted(mngr_agents_dir.iterdir()):
        sessions_dir = agent_dir / PI_SESSIONS_SUBPATH
        if not sessions_dir.is_dir():
            continue
        session_key = f"{PI_SESSION_PREFIX}{agent_dir.name}"
        agent_id_by_session[session_key] = agent_dir.name
        for transcript in sorted(sessions_dir.glob(f"*/*{TRANSCRIPT_SUFFIX}")):
            cwd = notes_dir
            try:
                lines = lines_containing(
                    transcript, _PI_LINE_MARKERS + (notes_dir.name.encode(),), TRANSCRIPT_CHUNK_BYTES
                )
            except OSError as e:
                logger.debug("Skipped pi transcript {}: {}", transcript, e)
                continue
            for line in lines:
                if '"type":"session"' in line.replace(" ", ""):
                    cwd = _session_cwd(line, cwd)
                elif '"toolCall"' in line and notes_dir.name in line:
                    uses.extend(pi_note_tool_uses(line, session_key, notes_dir, cwd))
    return uses, agent_id_by_session


@pure
def _session_cwd(line: str, fallback: Path) -> Path:
    try:
        record = json.loads(line)
    except ValueError:
        return fallback
    cwd = record.get("cwd") if isinstance(record, Mapping) else None
    return Path(cwd) if isinstance(cwd, str) else fallback


@pure
def _parse_timestamp(timestamp: object) -> datetime | None:
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A time with no offset can't be ordered against the others, so it counts as unknown.
    return parsed if parsed.tzinfo is not None else None


@pure
def parse_session_history(text: str) -> list[str]:
    """The session ids in a ``claude_session_id_history`` file (``<session id> <reason>`` per line)."""
    return [line.split()[0] for line in text.splitlines() if line.strip()]


def read_agent_id_by_session(mngr_agents_dir: Path) -> dict[str, str]:
    agent_id_by_session: dict[str, str] = {}
    if not mngr_agents_dir.is_dir():
        return agent_id_by_session
    for agent_dir in mngr_agents_dir.iterdir():
        try:
            history = (agent_dir / SESSION_HISTORY_FILENAME).read_text(encoding="utf-8")
        except OSError as e:
            logger.trace("No Claude session history for agent {}: {}", agent_dir.name, e)
            continue
        for session_id in parse_session_history(history):
            agent_id_by_session[session_id] = agent_dir.name
    return agent_id_by_session


def fetch_chat_title_by_agent_id(client: httpx.Client, chat_app_url: str) -> dict[str, str] | None:
    """Every agent id a live chat has run on, mapped to the chat's title; None when the chat app cannot answer."""
    started_at = time.monotonic()
    try:
        response = client.get(f"{chat_app_url}/api/chats", timeout=CHAT_LIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Could not list chats for note attribution: {}", e)
        return None
    elapsed = time.monotonic() - started_at
    if elapsed > CHAT_LIST_SLOW_SECONDS:
        logger.warning("Listed chats for note attribution slowly, in {:.1f}s", elapsed)
    title_by_agent_id: dict[str, str] = {}
    for chat in body.get("chats", []) if isinstance(body, Mapping) else []:
        if not isinstance(chat, Mapping):
            continue
        for agent_id in chat.get("agent_ids", []):
            title_by_agent_id[str(agent_id)] = str(chat.get("title", ""))
    return title_by_agent_id


@pure
def attribute_notes(
    uses: Iterable[NoteToolUse],
    agent_id_by_session: Mapping[str, str],
    chat_title_by_agent_id: Mapping[str, str] | None,
) -> dict[str, NoteAttribution]:
    """Each note's authors (latest write per writer) and how many others read it, keyed by file name.

    ``chat_title_by_agent_id`` is None when the chat app could not be asked: every writer is then UNKNOWN rather than
    taken for a deleted chat. A writer also reads the note it edits, so writers are not counted as readers.
    """

    def who(session_id: str) -> tuple[AuthorKind, str, str | None]:
        agent_id = agent_id_by_session.get(session_id)
        if chat_title_by_agent_id is None or agent_id is None:
            return AuthorKind.UNKNOWN, f"session:{agent_id or session_id}", None
        title = chat_title_by_agent_id.get(agent_id)
        if title is None:
            return AuthorKind.NOT_A_CHAT, f"agent:{agent_id}", None
        return AuthorKind.CHAT, f"chat:{title}", title

    latest_write_by_note: dict[str, dict[str, tuple[AuthorKind, str | None, datetime | None]]] = {}
    readers_by_note: dict[str, set[str]] = {}
    for use in uses:
        kind, key, title = who(use.session_id)
        if use.is_write:
            writes = latest_write_by_note.setdefault(use.file_name, {})
            previous = writes.get(key)
            if previous is None or (use.at is not None and (previous[2] is None or use.at > previous[2])):
                writes[key] = (kind, title, use.at)
        else:
            readers_by_note.setdefault(use.file_name, set()).add(key)

    attributions: dict[str, NoteAttribution] = {}
    for file_name in set(latest_write_by_note) | set(readers_by_note):
        writes = latest_write_by_note.get(file_name, {})
        authors = sorted(
            (NoteAuthor(kind=kind, chat_title=title, at=at) for kind, title, at in writes.values()),
            key=lambda author: author.at.timestamp() if author.at is not None else 0.0,
            reverse=True,
        )
        readers = readers_by_note.get(file_name, set()) - set(writes)
        attributions[file_name] = NoteAttribution(authors=tuple(authors), reader_count=len(readers))
    return attributions


def read_attributions(
    sources: TranscriptSources, client: httpx.Client, chat_app_url: str
) -> tuple[dict[str, NoteAttribution], Sequence[str]]:
    """Every note's attribution, plus notes on what could not be read."""
    notes: list[str] = []
    chat_title_by_agent_id = fetch_chat_title_by_agent_id(client, chat_app_url)
    if chat_title_by_agent_id is None:
        notes.append("Chat names could not be read from the chat app, so writers are shown without them.")
    pi_uses, pi_agent_id_by_session = read_pi_note_tool_uses(sources.mngr_agents_dir, sources.notes_dir)
    attributions = attribute_notes(
        [*read_note_tool_uses(sources), *pi_uses],
        {**read_agent_id_by_session(sources.mngr_agents_dir), **pi_agent_id_by_session},
        chat_title_by_agent_id,
    )
    return attributions, notes
