"""The mentions a person has not looked at yet (the multiplayer spec, section 7.4).

When a message mentions a person, the chat app appends ``{"chat_id", "message_id", "sender"}`` to
``data/.apps/chat/mentions/<key>.jsonl``; ``GET /api/mentions/pending`` answers the requester their own, so the
chat list's row wears an ``@`` badge until they have looked, and a page of theirs watching the chat clears that
chat's entries. The file is the whole state: a restart changes nothing about who has been mentioned.
"""

import json
import threading
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.primitives import ParticipantKey

from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.mention_parser import MentionTarget
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

MENTIONS_DIRNAME: Final[str] = "mentions"
DEFAULT_MENTIONS_ROOT: Final[Path] = DEFAULT_CHAT_DATA_DIR / MENTIONS_DIRNAME


class PendingMention(FrozenModel):
    """One mention a person has not looked at: which chat, which message, and who said it."""

    chat_id: ChatId = Field(description="The chat the mention was made in")
    message_id: str = Field(description="The mentioning message's id")
    sender: ParticipantKey = Field(description="Who mentioned them")


class PendingMentionsResponse(FrozenModel):
    """What ``GET /api/mentions/pending`` answers: the requester's unseen mentions, oldest first."""

    mentions: tuple[PendingMention, ...] = Field(description="The mentions, oldest first")


@pure
def mentions_filename(key: ParticipantKey) -> str:
    """The file a participant's mentions are kept in: the key, with the one character a key may carry that a
    file name may not."""
    return f"{str(key).replace('/', '_')}.jsonl"


class MentionActions(MutableModel, ABC):
    """What a message's mentions do (section 7.4), over the roster and the chat list of the moment. Implemented
    beside the room routes, which hold the send path a room's mention reaches an agent through; the state carries
    it so the message route can act on a send's mentions."""

    @abstractmethod
    def table(self) -> tuple[MentionTarget, ...]:
        """The handle table now."""

    @abstractmethod
    def resolve(self, text: str) -> tuple[MentionTarget, ...]:
        """The targets ``text`` mentions."""

    @abstractmethod
    def act(
        self,
        chat_id: ChatId,
        chat_title: str,
        message_id: str,
        text: str,
        sender: ParticipantKey,
        sender_name: str,
        targets: tuple[MentionTarget, ...],
        room_id: ChatId | None,
    ) -> None:
        """Act on a landed message's mentions; ``room_id`` names the room it was said in, or None for an agent chat."""


class MentionStore(MutableModel):
    """Each participant's pending mentions, one append-only file per participant."""

    model_config = {"extra": "forbid", "frozen": False}

    root: Path = Field(frozen=True, description="The mentions directory (``data/.apps/chat/mentions``)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _path(self, key: ParticipantKey) -> Path:
        return self.root / mentions_filename(key)

    def record(self, key: ParticipantKey, chat_id: ChatId, message_id: str, sender: ParticipantKey) -> None:
        mention = PendingMention(chat_id=chat_id, message_id=message_id, sender=sender)
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as mentions_file:
                mentions_file.write(mention.model_dump_json() + "\n")

    def pending(self, key: ParticipantKey) -> tuple[PendingMention, ...]:
        with self._lock:
            return self._read_locked(key)

    def clear(self, key: ParticipantKey, chat_id: ChatId) -> bool:
        """Drop the participant's mentions in ``chat_id``; whether any was dropped."""
        with self._lock:
            pending = self._read_locked(key)
            kept = tuple(mention for mention in pending if mention.chat_id != chat_id)
            if len(kept) == len(pending):
                return False
            path = self._path(key)
            if not kept:
                path.unlink(missing_ok=True)
                return True
            temp_path = path.with_suffix(".jsonl.tmp")
            temp_path.write_text("".join(mention.model_dump_json() + "\n" for mention in kept), encoding="utf-8")
            temp_path.replace(path)
            return True

    def _read_locked(self, key: ParticipantKey) -> tuple[PendingMention, ...]:
        path = self._path(key)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return ()
        except OSError as e:
            logger.warning("Could not read the mentions of {}: {}", key, e)
            return ()
        mentions: list[PendingMention] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                mentions.append(PendingMention.model_validate(json.loads(line)))
            except (ValueError, ValidationError) as e:
                logger.warning("Skipped an unreadable mention line of {}: {}", key, e)
        return tuple(mentions)
