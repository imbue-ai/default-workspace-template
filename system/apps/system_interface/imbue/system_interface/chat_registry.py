"""The durable registry of logical chats and the physical agents backing them.

A chat is what the user sees: one continuous conversation with a stable
identity. An mngr agent is the process currently backing it. Today the two are
one-to-one and a chat's id is its first agent's id, so this registry is
bootstrapped idempotently from the discovered agents and resolution falls back
to identity when a chat has no record. The registry exists so a chat can later
be re-pointed at a replacement agent (harness switching) without changing any
persisted reference to the chat -- project refs, panel ids, drafts, and API
routes all keep addressing the chat id.

Storage is one JSON file per chat under ``<layout_dir>/chats/<chat_id>.json``,
following the per-entity precedent of ``projects/<project_id>.json``: a future
handoff mutates exactly one record, the per-file atomic replace keeps each
chat's record whole for concurrent readers, and deleting a chat is an unlink.
A ``chats_dir`` of None keeps the registry in memory only (tests, and a server
with no workspace to persist into), mirroring ``AutoOpenLedger``.
"""

import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.atomic_write import write_json_atomic
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.models import ChatId

_CHATS_SUBDIR = "chats"


class ChatRecordError(ValueError):
    """Raised when a chat record violates the active-segment invariant.

    A ``ValueError`` subclass so pydantic folds it into the record's
    ``ValidationError`` and the registry's load path catches it like any other
    malformed record.
    """

    ...


def chats_dir_for_layout_dir(layout_dir: Path) -> Path:
    """Where chat records live for a workspace whose layouts are saved under ``layout_dir``."""
    return layout_dir / _CHATS_SUBDIR


class ChatSegment(FrozenModel):
    """One physical agent's tenure backing a chat."""

    agent_id: str = Field(description="The mngr agent backing the chat during this segment")
    harness: HarnessType = Field(description="The harness the segment's agent runs")
    account_id: str | None = Field(
        default=None,
        description="Snapshot of the provider account the agent was bound to when the segment "
        "started (the agent's 'account' label), so history stays attributable after the "
        "account is renamed or removed. None for an unbound agent.",
    )
    started_at: str = Field(description="ISO-8601 UTC timestamp of when this segment began")
    ended_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp of when this segment was retired; None while active",
    )


class ChatRecord(FrozenModel):
    """A logical chat: its stable id, its active agent, and its ordered segments."""

    chat_id: str = Field(description="The chat's stable identity (its first agent's id)")
    active_agent_id: str = Field(description="The id of the agent currently backing the chat")
    segments: tuple[ChatSegment, ...] = Field(description="Ordered backing-agent segments, oldest first")

    @model_validator(mode="after")
    def _check_active_segment(self) -> "ChatRecord":
        """Exactly the last segment is active, and it names ``active_agent_id``."""
        if not self.segments:
            raise ChatRecordError(f"Chat {self.chat_id} has no segments")
        for segment in self.segments[:-1]:
            if segment.ended_at is None:
                raise ChatRecordError(f"Chat {self.chat_id} has a non-final segment with no ended_at")
        last = self.segments[-1]
        if last.ended_at is not None:
            raise ChatRecordError(f"Chat {self.chat_id} has no active segment (last segment already ended)")
        if last.agent_id != self.active_agent_id:
            raise ChatRecordError(
                f"Chat {self.chat_id} names {self.active_agent_id} active but its last segment "
                f"belongs to {last.agent_id}"
            )
        return self


class ChatRegistry(MutableModel):
    """All logical chats of one workspace, loaded once and written through atomically.

    Writes are per-chat and atomic; a record that cannot be persisted still
    exists in memory (a full disk must not break chat creation), matching how
    the other durable stores beside the workspace layout degrade.
    """

    model_config = {"extra": "forbid", "frozen": False}

    chats_dir: Path | None
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _records: dict[ChatId, ChatRecord] = PrivateAttr(default_factory=dict)
    # Every agent that has ever backed a chat, active or retired, mapped to that chat.
    # Kept beside ``_records`` rather than derived on demand because it answers the
    # question ``ensure_chat`` has to ask on every discovery pass: has this agent
    # already got a chat above it? Scanning all segments of all chats to answer that
    # would make discovery quadratic.
    _chat_id_by_any_agent: dict[str, ChatId] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        self._records = self._load()
        self._chat_id_by_any_agent = {
            segment.agent_id: chat_id for chat_id, record in self._records.items() for segment in record.segments
        }

    def _load(self) -> dict[ChatId, ChatRecord]:
        if self.chats_dir is None or not self.chats_dir.is_dir():
            return {}
        records: dict[ChatId, ChatRecord] = {}
        for path in sorted(self.chats_dir.glob("*.json")):
            try:
                record = ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                _loguru_logger.opt(exception=e).warning("Ignoring an unreadable chat record at {}", path)
                continue
            records[ChatId(record.chat_id)] = record
        return records

    def _record_path(self, chat_id: ChatId) -> Path | None:
        if self.chats_dir is None:
            return None
        return self.chats_dir / f"{chat_id}.json"

    def _save_record_unlocked(self, record: ChatRecord) -> None:
        path = self._record_path(ChatId(record.chat_id))
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, record.model_dump_json(indent=2))
        except OSError as e:
            _loguru_logger.opt(exception=e).warning("Failed to write the chat record at {}", path)

    def get(self, chat_id: ChatId) -> ChatRecord | None:
        with self._lock:
            return self._records.get(chat_id)

    def resolve_active_agent_id(self, chat_id: ChatId) -> str:
        """The id of the agent currently backing ``chat_id``.

        Falls back to the chat id itself when no record exists: today every
        chat is backed by the agent whose id it adopted, so an unrecorded chat
        (a dev/test server with no persistence, a workspace mid-bootstrap)
        resolves exactly as it did before this registry existed.
        """
        with self._lock:
            record = self._records.get(chat_id)
        return record.active_agent_id if record is not None else str(chat_id)

    def chat_id_for_active_agent(self, agent_id: str) -> ChatId | None:
        """Which chat ``agent_id`` currently backs, or None if it backs none.

        The inverse of ``resolve_active_agent_id``, and the answer the agents
        projection needs: an agent knows nothing about the chat above it, so the
        chat id every product surface addresses has to be looked up from here.
        Retired agents deliberately do not match -- they back no chat any more.
        """
        with self._lock:
            for chat_id, record in self._records.items():
                if record.active_agent_id == agent_id:
                    return chat_id
        # An unrecorded chat resolves by identity, so an agent with no record is its
        # own chat -- exactly what the fallback in ``resolve_active_agent_id`` assumes.
        return None

    def chat_id_by_active_agent(self) -> dict[str, ChatId]:
        """Every recorded chat's backing agent, mapped to the chat it backs.

        The bulk form of ``chat_id_for_active_agent``, for the agents projection: it
        stamps a chat id on every row it lists, and doing that a row at a time would
        take this lock once per agent.
        """
        with self._lock:
            return {record.active_agent_id: chat_id for chat_id, record in self._records.items()}

    def retired_agent_ids(self, chat_id: ChatId) -> tuple[str, ...]:
        """The chat's previous backing agents, oldest first. Empty until it switches.

        These are what the transcript archive is keyed by, so reading a chat's whole
        history is this list plus its active agent -- in exactly this order.
        """
        with self._lock:
            record = self._records.get(chat_id)
        if record is None:
            return ()
        return tuple(segment.agent_id for segment in record.segments[:-1])

    def ensure_chat(self, chat_id: ChatId, agent_id: str, harness: HarnessType, account_id: str | None) -> None:
        """Record a chat backed by ``agent_id``, if neither it nor the agent is known yet.

        Idempotent: an existing record is left untouched (its segments are its
        history; discovery must never rewrite them), which is what makes the
        bootstrap safe to run on every discovery pass and every restart.

        An agent that already appears in some chat's segments is equally a no-op,
        and that is the case that matters after a harness switch: the successor
        agent's id is not the chat's id, so a bootstrap that only checked
        ``chat_id`` would give it a second chat of its own -- which then wins the
        agents projection's chat lookup and makes the chat's id appear to change
        under the user at the commit point.
        """
        with self._lock:
            if chat_id in self._records or agent_id in self._chat_id_by_any_agent:
                return
            record = ChatRecord(
                chat_id=str(chat_id),
                active_agent_id=agent_id,
                segments=(
                    ChatSegment(
                        agent_id=agent_id,
                        harness=harness,
                        account_id=account_id,
                        started_at=datetime.now(timezone.utc).isoformat(),
                    ),
                ),
            )
            self._records[chat_id] = record
            self._chat_id_by_any_agent[agent_id] = chat_id
            self._save_record_unlocked(record)

    def begin_segment(
        self, chat_id: ChatId, agent_id: str, harness: HarnessType, account_id: str | None
    ) -> ChatRecord:
        """Re-point ``chat_id`` at ``agent_id``, closing the outgoing segment.

        This single call is the commit point of a harness handoff: the chat's
        active agent and its segment history move together, under one lock and
        one atomic file write, so no reader and no restart can observe a chat
        whose active agent and whose open segment disagree. Everything before it
        is reversible (the candidate agent can be destroyed and the old one
        unfrozen); nothing after it is.

        Raises ``ChatRecordError`` for an unrecorded chat: a chat with no record
        resolves by identity, and re-pointing something that resolves by
        identity would silently do nothing.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            record = self._records.get(chat_id)
            if record is None:
                raise ChatRecordError(f"Chat {chat_id} has no record to re-point")
            outgoing = record.segments[-1]
            retired = outgoing.model_copy_update(to_update(outgoing.field_ref().ended_at, now))
            successor = ChatSegment(
                agent_id=agent_id,
                harness=harness,
                account_id=account_id,
                started_at=now,
            )
            updated = ChatRecord(
                chat_id=record.chat_id,
                active_agent_id=agent_id,
                segments=(*record.segments[:-1], retired, successor),
            )
            self._records[chat_id] = updated
            self._chat_id_by_any_agent[agent_id] = chat_id
            self._save_record_unlocked(updated)
            return updated

    def remove(self, chat_id: ChatId) -> None:
        """Drop a deleted chat's record and its file. No-op for an unrecorded chat."""
        with self._lock:
            removed = self._records.pop(chat_id, None)
            if removed is None:
                return
            for segment in removed.segments:
                self._chat_id_by_any_agent.pop(segment.agent_id, None)
            path = self._record_path(chat_id)
            if path is None:
                return
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                _loguru_logger.opt(exception=e).warning("Failed to remove the chat record at {}", path)
