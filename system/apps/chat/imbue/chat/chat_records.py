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
from typing import Self

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.models import SummaryOutcome
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


class InvalidChatRecordError(ChatRecordError, ValueError):
    """A chat record whose agents contradict what it says about them; raised inside validation, so
    pydantic reports it as a ``ValidationError`` and the store's read turns it into a ``ChatRecordError``."""


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

    Everything a resumed handoff needs to pick up where it stopped lives here (spec 5.11): the
    phase, the target, the pre-minted successor id, the sends held for it, the summary's
    outcome, and the prompt the successor is created with, so a chat-app restart at any point
    reconciles against mngr's state rather than replaying steps.
    """

    handoff_id: str = Field(
        description="Minted per handoff; a runner that finds another id stops (the handoff was cancelled)"
    )
    phase: HandoffPhase = Field(description="Which step of the handoff the chat is in")
    started_at: datetime = Field(description="When the handoff was confirmed")
    target_lane: str = Field(description="The lane the chat is moving to")
    target_account_id: str = Field(description="The account the chat is moving to")
    target_harness: HarnessType = Field(
        description="The harness the target account runs, fixed when the handoff began"
    )
    retiring_seq: int = Field(ge=1, description="The sequence number of the agent the chat is leaving")
    next_agent_id: str = Field(
        description="The successor's id, minted before its create so a resume can tell whether it landed"
    )
    next_seq: int = Field(ge=2, description="The successor's sequence number")
    chat_name: str = Field(description="The chat's canonical mngr name, which the successor takes over")
    chat_title: str = Field(description="The name the user sees, kept for the archival display name and the prompt")
    project_label: str = Field(default="", description="The retiring agent's project label, carried to the successor")
    trigger_message_id: str = Field(
        description="The id of the message that confirmed the handoff, the successor's first"
    )
    held_sends: tuple[HeldSend, ...] = Field(
        default=(), description="The sends received while converging, in order; delivered to the successor"
    )
    returned_block: str = Field(
        default="", description="The queued text draining took off the retiring agent, for the composer"
    )
    summary_outcome: SummaryOutcome | None = Field(
        default=None, description="How summarizing ended; None before it has"
    )
    prompt: str | None = Field(
        default=None, description="The successor's first message, built once and resent verbatim"
    )
    error: str | None = Field(default=None, description="Why the successor's create failed, in the failed phase")

    def entry_of(self, message_id: str) -> HeldSend | None:
        return next((held for held in self.held_sends if held.message_id == message_id), None)


class ChatRecord(FrozenModel):
    """A multi-agent chat: its agents in order, and its handoff state."""

    version: int = Field(default=RECORD_VERSION, description="The on-disk shape this record was written with")
    chat_id: ChatId = Field(description="The chat's id: its first agent's id")
    agents: tuple[ChatAgentEntry, ...] = Field(min_length=1, description="The chat's agents, in order")
    handoff: ChatHandoffRecord | None = Field(default=None, description="The in-progress handoff, or None")

    @model_validator(mode="after")
    def _check_agents_are_the_chats_in_order(self) -> Self:
        """The record's own claims about its agents hold: the first is the chat's namesake, every
        ``seq`` is its position, no agent appears twice, and only the last can still be running."""
        if self.agents[0].agent_id != self.chat_id:
            raise InvalidChatRecordError(
                f"chat {self.chat_id}'s first agent is {self.agents[0].agent_id}, but a chat's id is its first agent's"
            )
        for index, entry in enumerate(self.agents):
            if entry.seq != index + 1:
                raise InvalidChatRecordError(
                    f"chat {self.chat_id}: agent {entry.agent_id} is entry {index + 1} but carries seq {entry.seq}"
                )
        if len(set(self.member_agent_ids)) != len(self.agents):
            raise InvalidChatRecordError(f"chat {self.chat_id} names an agent twice")
        for entry in self.agents[:-1]:
            if entry.ended_at is None:
                raise InvalidChatRecordError(
                    f"chat {self.chat_id}: agent {entry.agent_id} (seq {entry.seq}) has a successor but no ended_at"
                )
        if self.handoff is not None:
            last = self.agents[-1]
            # The successor is appended while the handoff still delivers the sends it held, so
            # the last agent is either the one retiring or the one taking over.
            is_successor_appended = last.agent_id == self.handoff.next_agent_id and last.seq == self.handoff.next_seq
            if not is_successor_appended and self.handoff.retiring_seq != last.seq:
                raise InvalidChatRecordError(
                    f"chat {self.chat_id}: the handoff retires seq {self.handoff.retiring_seq} but the last agent is "
                    f"seq {last.seq}"
                )
            if self.handoff.next_seq != self.handoff.retiring_seq + 1:
                raise InvalidChatRecordError(
                    f"chat {self.chat_id}: the handoff's successor is seq {self.handoff.next_seq}, not the next"
                )
            if self.handoff.next_agent_id in self.member_agent_ids and not is_successor_appended:
                raise InvalidChatRecordError(f"chat {self.chat_id}: the handoff's successor is already a member")
        return self

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
        """Drop the chat's record and everything stored beside it; a no-op for a chat with none.

        Raises ``ChatRecordError`` when the record exists but cannot be removed.
        """


class InMemoryChatRecordStore(ChatRecordStore):
    """Records held in memory: the default a manager built without a root gets, and what tests use."""

    record_by_chat_id: dict[ChatId, ChatRecord] = Field(default_factory=dict, description="The records, by chat id")

    def read(self, chat_id: ChatId) -> ChatRecord | None:
        return self.record_by_chat_id.get(chat_id)

    def read_all(self) -> dict[ChatId, ChatRecord]:
        return dict(self.record_by_chat_id)

    def write(self, record: ChatRecord) -> None:
        self.record_by_chat_id[record.chat_id] = record

    def delete(self, chat_id: ChatId) -> None:
        self.record_by_chat_id.pop(chat_id, None)


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
        record_by_chat_id: dict[ChatId, ChatRecord] = {}
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
            record_by_chat_id[record.chat_id] = record
        return record_by_chat_id

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
            try:
                shutil.rmtree(chat_dir)
            except OSError as e:
                raise ChatRecordError(f"chat record folder {chat_dir} could not be removed: {e}") from e
