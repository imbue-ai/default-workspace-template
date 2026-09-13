"""The chat record store: which agents a chat has run on, in order (``docs/system/blueprint/chat-agent-split/`` 4.2).

A record exists only for a chat that has had a handoff; a chat with no record is its one agent
(the own-chat rule). Records live under ``data/.apps/chat/chats/<chat_id>/record.json``, written
atomically under a per-chat lock, with the accounts index's version discipline: a record from a
newer build refuses to load rather than being read wrong. The store sits behind a small interface
so the manager reads records the same way whether they come from disk or, in tests, from memory.
"""

import contextlib
import fcntl
import json
import os
import shutil
from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.models import HandoffPhase
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

# Bumped when the on-disk shape changes. A record whose version is newer than this refuses to
# load, so an older build never reads a newer record wrong.
RECORD_VERSION: Final[int] = 1

DEFAULT_CHAT_RECORDS_ROOT: Final[Path] = Path("data/.apps/chat/chats")

_RECORD_FILENAME: Final[str] = "record.json"
_LOCK_FILENAME: Final[str] = "record.lock"


class ChatRecordError(RuntimeError):
    """A chat record could not be read or written."""


class ChatAgentEntry(FrozenModel):
    """One agent of a chat: its place in the sequence and what it ran on."""

    seq: int = Field(ge=1, description="The agent's 1-based position in the chat")
    agent_id: str = Field(description="The agent's mngr id")
    lane: str = Field(description="The lane the agent was created on")
    account_id: str = Field(description="The account the agent was bound to")
    harness: HarnessType = Field(description="The harness the agent runs")
    started_at: datetime = Field(description="When the agent became the chat's active agent")
    ended_at: datetime | None = Field(default=None, description="When the agent was archived; None while active")
    archived_name: str | None = Field(default=None, description="The agent's archival mngr name, once archived")
    final_event_count: int | None = Field(
        default=None, ge=0, description="The agent's main-transcript event count, recorded when it was archived"
    )


class ChatHandoffRecord(FrozenModel):
    """The in-progress handoff a record carries while the chat converges on a new agent.

    Phase 4 of the chat-agent split extends this with the handoff's working state; this phase
    reads only the phase and target the chat snapshot shows.
    """

    phase: HandoffPhase = Field(description="Which step of the handoff the chat is in")
    target_lane: str = Field(description="The lane the chat is moving to")
    target_account_id: str = Field(description="The account the chat is moving to")


class ChatRecord(FrozenModel):
    """A multi-agent chat: its agents in order, and its handoff state."""

    version: int = Field(default=RECORD_VERSION, description="The on-disk shape this record was written with")
    chat_id: ChatId = Field(description="The chat's id: its first agent's id")
    agents: tuple[ChatAgentEntry, ...] = Field(min_length=1, description="The chat's agents, in order")
    handoff: ChatHandoffRecord | None = Field(default=None, description="The in-progress handoff, or None")

    @property
    def member_agent_ids(self) -> tuple[str, ...]:
        return tuple(entry.agent_id for entry in self.agents)

    @property
    def active_entry(self) -> ChatAgentEntry | None:
        """The agent the chat runs on: the last entry, unless it has already been archived."""
        last = self.agents[-1]
        return None if last.ended_at is not None else last

    @property
    def archived_entries(self) -> tuple[ChatAgentEntry, ...]:
        return tuple(entry for entry in self.agents if entry.ended_at is not None)

    def entry_for(self, agent_id: str) -> ChatAgentEntry | None:
        return next((entry for entry in self.agents if entry.agent_id == agent_id), None)


class ChatRecordStore(MutableModel, ABC):
    """Where the chat app keeps its chat records."""

    @abstractmethod
    def read(self, chat_id: ChatId) -> ChatRecord | None:
        """The record of one chat, or None when the chat has none."""

    @abstractmethod
    def read_all(self) -> dict[ChatId, ChatRecord]:
        """Every readable record, by chat id; an unreadable one is logged and left out."""

    @abstractmethod
    def write(self, record: ChatRecord) -> None:
        """Replace the chat's record with ``record``."""

    @abstractmethod
    def delete(self, chat_id: ChatId) -> None:
        """Drop the chat's record and everything stored beside it; a no-op for a chat with none."""


class InMemoryChatRecordStore(ChatRecordStore):
    """Records held in memory: the default a manager built without a root gets, and what tests use."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    records: dict[ChatId, ChatRecord] = Field(default_factory=dict, description="The records, by chat id")

    def read(self, chat_id: ChatId) -> ChatRecord | None:
        return self.records.get(chat_id)

    def read_all(self) -> dict[ChatId, ChatRecord]:
        return dict(self.records)

    def write(self, record: ChatRecord) -> None:
        self.records[record.chat_id] = record

    def delete(self, chat_id: ChatId) -> None:
        self.records.pop(chat_id, None)


def _parse_record(payload: str, path: Path) -> ChatRecord:
    """Raises ``ChatRecordError`` for a record this build cannot read: bad JSON, a newer version, or a bad shape."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ChatRecordError(f"chat record at {path} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ChatRecordError(f"chat record at {path} is not an object: {type(raw).__name__}")
    version = raw.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ChatRecordError(f"chat record at {path} has a non-numeric version: {version!r}")
    if version > RECORD_VERSION:
        raise ChatRecordError(
            f"chat record at {path} is version {version}, but this build understands {RECORD_VERSION}; "
            "refusing to read it rather than reading it wrong"
        )
    try:
        return ChatRecord.model_validate(raw)
    except ValidationError as e:
        raise ChatRecordError(f"chat record at {path} is not readable: {e}") from e


class FileChatRecordStore(ChatRecordStore):
    """Records on disk under ``<root>/<chat_id>/record.json``, each written atomically under its chat's lock."""

    root: Path = Field(frozen=True, description="The directory holding one folder per chat")

    def _chat_dir(self, chat_id: ChatId) -> Path:
        return self.root / chat_id

    @contextlib.contextmanager
    def _chat_lock(self, chat_id: ChatId) -> Iterator[None]:
        chat_dir = self._chat_dir(chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        with (chat_dir / _LOCK_FILENAME).open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read_path(self, path: Path) -> ChatRecord | None:
        if not path.exists():
            return None
        try:
            payload = path.read_text()
        except OSError as e:
            raise ChatRecordError(f"chat record at {path} is unreadable: {e}") from e
        return _parse_record(payload, path)

    def read(self, chat_id: ChatId) -> ChatRecord | None:
        return self._read_path(self._chat_dir(chat_id) / _RECORD_FILENAME)

    def read_all(self) -> dict[ChatId, ChatRecord]:
        """Every readable record. A record this build cannot read is logged and skipped, so one bad
        file costs its chat the record (its agents fall under the own-chat rule) and nothing else."""
        if not self.root.is_dir():
            return {}
        records: dict[ChatId, ChatRecord] = {}
        for record_path in sorted(self.root.glob(f"*/{_RECORD_FILENAME}")):
            try:
                record = self._read_path(record_path)
            except ChatRecordError as e:
                logger.error("Skipped an unreadable chat record: {}", e)
                continue
            if record is None:
                continue
            if record.chat_id != record_path.parent.name:
                logger.error(
                    "Skipped the chat record at {}: it names chat {} but lives in {}'s folder",
                    record_path,
                    record.chat_id,
                    record_path.parent.name,
                )
                continue
            records[record.chat_id] = record
        return records

    def write(self, record: ChatRecord) -> None:
        with self._chat_lock(record.chat_id):
            path = self._chat_dir(record.chat_id) / _RECORD_FILENAME
            temp_path = path.with_suffix(".json.tmp")
            # Written at this build's version whatever version was read, so an older build that
            # later opens it refuses by version rather than by a field it does not know.
            current = record.model_copy_update(to_update(record.field_ref().version, RECORD_VERSION))
            temp_path.write_text(json.dumps(current.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
            os.replace(temp_path, path)

    def delete(self, chat_id: ChatId) -> None:
        chat_dir = self._chat_dir(chat_id)
        if not chat_dir.exists():
            return
        with self._chat_lock(chat_id):
            shutil.rmtree(chat_dir, ignore_errors=True)
