"""Which chat wrote each note, and how many chats have read it, from the chats' own transcripts.

A note file records nothing reliable about who wrote it, but every chat's transcript records each tool call it made.
Claude: every ``Write``/``Edit`` of a file in the notes folder is a write by that session and every ``Read`` a read,
and a session belongs to the agent whose ``claude_session_id_history`` lists it (under the mngr host dir). pi: its
sessions live under each agent's own folder (``agents/<id>/plugin/pi_coding/sessions/``), so the agent is known
directly, and its ``write``/``edit``/``read`` tool calls count the same way. An agent belongs to the chat whose
``agent_ids`` hold it (the chat app's ``GET /api/chats``). A session no agent claims, or an agent no live chat holds
(a deleted chat), is still counted, just without a chat's name.

Transcripts are read when the page asks, line by line, parsing only the lines that name the notes folder.
"""

import json
import os
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

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
DEFAULT_CHAT_APP_URL: Final[str] = "http://127.0.0.1:8010"
CHAT_LIST_TIMEOUT_SECONDS: Final[float] = 5.0


class NoteToolUse(FrozenModel):
    """One tool call a session made on a note."""

    session_id: str = Field(description="The Claude session that made it")
    file_name: str = Field(description="The note's file name")
    is_write: bool = Field(description="A write or edit, as opposed to a read")
    at: datetime | None = Field(description="When, from the transcript line's timestamp")


class NoteAuthor(FrozenModel):
    """A chat (or an agent no chat holds any more) that wrote a note."""

    chat_title: str | None = Field(description="The chat's title, or None when no live chat holds the agent")
    at: datetime | None = Field(description="When it last wrote the note")


class NoteAttribution(FrozenModel):
    """Who wrote a note and how many chats read it."""

    authors: tuple[NoteAuthor, ...] = Field(description="Every chat that wrote it, most recent first")
    reader_count: int = Field(description="How many distinct chats (or unclaimed sessions) read it")


class TranscriptSources(FrozenModel):
    """Where the records come from; injectable so tests read fake trees."""

    claude_config_dirs: tuple[Path, ...] = Field(description="Claude config dirs holding ``projects/`` transcripts")
    project_dir_name: str = Field(description="The encoded workspace path naming its transcripts folder")
    mngr_agents_dir: Path = Field(description="The mngr host dir's ``agents/`` folder")
    notes_dir: Path = Field(description="The notes folder, absolute")


@pure
def encode_project_dir_name(work_dir: Path) -> str:
    """Claude's name for a project's transcripts folder: the absolute path with ``/`` and ``.`` as ``-``."""
    return str(work_dir).replace("/", "-").replace(".", "-")


def default_transcript_sources(notes_dir: Path, work_dir: Path) -> TranscriptSources:
    home = Path.home()
    accounts_dir = home / ".minds" / "accounts"
    account_dirs = sorted(path for path in accounts_dir.iterdir() if path.is_dir()) if accounts_dir.is_dir() else []
    host_dir = Path(os.environ.get("MNGR_HOST_DIR") or home / ".mngr")
    return TranscriptSources(
        claude_config_dirs=(*account_dirs, home / ".claude"),
        project_dir_name=encode_project_dir_name(work_dir),
        mngr_agents_dir=host_dir / "agents",
        notes_dir=notes_dir.absolute(),
    )


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
    notes_marker = str(sources.notes_dir.relative_to(sources.notes_dir.parent.parent))
    uses: list[NoteToolUse] = []
    for config_dir in sources.claude_config_dirs:
        project_dir = config_dir / "projects" / sources.project_dir_name
        if not project_dir.is_dir():
            continue
        for transcript in sorted(project_dir.glob(f"*{TRANSCRIPT_SUFFIX}")):
            try:
                with transcript.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if notes_marker in line and '"tool_use"' in line:
                            uses.extend(note_tool_uses(line, transcript.stem, sources.notes_dir))
            except OSError as e:
                logger.debug("Skipped transcript {}: {}", transcript, e)
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
                with transcript.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if '"type":"session"' in line.replace(" ", ""):
                            cwd = _session_cwd(line, cwd)
                        elif '"toolCall"' in line and notes_dir.name in line:
                            uses.extend(pi_note_tool_uses(line, session_key, notes_dir, cwd))
            except OSError as e:
                logger.debug("Skipped pi transcript {}: {}", transcript, e)
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
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        except OSError:
            continue
        for session_id in parse_session_history(history):
            agent_id_by_session[session_id] = agent_dir.name
    return agent_id_by_session


def fetch_chat_title_by_agent_id(client: httpx.Client, chat_app_url: str) -> dict[str, str] | None:
    """Every agent id a live chat has run on, mapped to the chat's title; None when the chat app cannot answer."""
    try:
        response = client.get(f"{chat_app_url}/api/chats", timeout=CHAT_LIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Could not list chats for note attribution: {}", e)
        return None
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
    chat_title_by_agent_id: Mapping[str, str],
) -> dict[str, NoteAttribution]:
    """Each note's authors (latest write per chat) and reader count, keyed by file name."""

    def who(session_id: str) -> str:
        agent_id = agent_id_by_session.get(session_id)
        if agent_id is not None and agent_id in chat_title_by_agent_id:
            return f"chat:{chat_title_by_agent_id[agent_id]}"
        return f"unclaimed:{agent_id or session_id}"

    latest_write_by_note: dict[str, dict[str, datetime | None]] = {}
    readers_by_note: dict[str, set[str]] = {}
    for use in uses:
        key = who(use.session_id)
        if use.is_write:
            writes = latest_write_by_note.setdefault(use.file_name, {})
            previous = writes.get(key)
            if key not in writes or (use.at is not None and (previous is None or use.at > previous)):
                writes[key] = use.at
        else:
            readers_by_note.setdefault(use.file_name, set()).add(key)

    attributions: dict[str, NoteAttribution] = {}
    for file_name in set(latest_write_by_note) | set(readers_by_note):
        writes = latest_write_by_note.get(file_name, {})
        authors = sorted(
            (
                NoteAuthor(chat_title=key.removeprefix("chat:") if key.startswith("chat:") else None, at=at)
                for key, at in writes.items()
            ),
            key=lambda author: author.at.timestamp() if author.at is not None else 0.0,
            reverse=True,
        )
        attributions[file_name] = NoteAttribution(
            authors=tuple(authors), reader_count=len(readers_by_note.get(file_name, set()))
        )
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
        chat_title_by_agent_id or {},
    )
    return attributions, notes
