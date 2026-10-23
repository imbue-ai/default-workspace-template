"""Mentions in a room (the multiplayer spec, section 7.4): what a message's ``@handles`` do once it has landed, and
the mentions each person has not looked at yet.

A mention acts only in a room. A mentioned person is marked: the chat app appends ``{"chat_id", "message_id",
"sender"}`` to ``data/.apps/chat/mentions/<key>.jsonl``, ``GET /api/mentions/pending`` answers the requester their
own, so the chat list's row wears an ``@`` badge until they have looked, and a page of theirs watching the chat
clears that chat's entries; the owner is told through the Imbue Studio app's feed as well. The workspace's agent
(``@agent``) or an agent chat by its title is sent the room's message, prefixed with who said it and where, through
the ordinary send path on a thread of its own; the room is marked as waiting on that chat until an agent posts into
it. In an agent chat a handle is a word the agent reads like any other.
"""

import json
import threading
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Final

from flask import current_app
from flask.ctx import AppContext
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import WORKSPACE_AGENT_PARTICIPANT_KEY

from imbue.chat.agent_manager import AgentManager
from imbue.chat.authors import AuthorStore
from imbue.chat.authors import author_prefix
from imbue.chat.authors import new_author_annotation
from imbue.chat.authors import prefix_safe_text
from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.mention_parser import MentionTarget
from imbue.chat.mention_parser import find_mentions
from imbue.chat.mention_parser import handle_table
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.roster_client import RosterClient
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

MENTIONS_DIRNAME: Final[str] = "mentions"
DEFAULT_MENTIONS_ROOT: Final[Path] = DEFAULT_CHAT_DATA_DIR / MENTIONS_DIRNAME

# The send a room's mention goes through: the target chat, the text as delivered, and the message id; the
# refusal's detail, or None once accepted.
MentionSend = Callable[[ChatId, str, str], str | None]


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


class MentionDeliverer(MutableModel, ABC):
    """How a room's mention reaches an agent chat."""

    @abstractmethod
    def deliver(self, chat_id: ChatId, delivered: str, message_id: str) -> None:
        """Send ``delivered`` to the chat as the message ``message_id``."""


class BackgroundMentionDeliverer(MentionDeliverer):
    """Runs the send under the app's context on a thread of its own, since a send can take seconds and the room's
    post must not wait on it; a refusal is logged."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    send: MentionSend = Field(frozen=True, description="The ordinary send path")

    def deliver(self, chat_id: ChatId, delivered: str, message_id: str) -> None:
        threading.Thread(
            target=self._run,
            args=(current_app.app_context(), chat_id, delivered, message_id),
            daemon=True,
            name=f"room-mention-{str(chat_id)[-8:]}",
        ).start()

    def _run(self, app_context: AppContext, chat_id: ChatId, delivered: str, message_id: str) -> None:
        with app_context:
            refusal = self.send(chat_id, delivered, message_id)
        if refusal is not None:
            logger.warning("A room's mention could not reach chat {}: {}", chat_id, refusal)


class MentionHandler(MutableModel):
    """Resolves and acts on a room message's mentions (section 7.4), over the roster and the chat list of the moment."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    agent_manager: AgentManager = Field(
        frozen=True, description="The chats, their titles and kinds, and the main chat"
    )
    roster: RosterClient = Field(frozen=True, description="The people and their handles")
    mentions: MentionStore = Field(frozen=True, description="Where a mentioned person's pending mark is kept")
    authors: AuthorStore = Field(frozen=True, description="Where a forwarded mention's author is recorded")
    deliverer: MentionDeliverer = Field(frozen=True, description="How a room's mention reaches an agent chat")
    on_pending_changed: Callable[[], None] = Field(
        frozen=True, description="Told when someone's pending mentions changed, so every page reads its own again"
    )

    def table(self) -> tuple[MentionTarget, ...]:
        """The handle table now: the roster, then every chat by its title."""
        chats = [(snapshot.chat_id, snapshot.title) for snapshot in self.agent_manager.get_chat_snapshots()]
        return handle_table(self.roster.document().participants, chats)

    def resolve(self, text: str) -> tuple[MentionTarget, ...]:
        return find_mentions(text, self.table())

    def act(
        self, room_id: ChatId, room_title: str, message_id: str, text: str, sender: ParticipantKey, sender_name: str
    ) -> None:
        """Act on a room message's mentions once it has landed: a person is marked (and the owner notified), and
        the workspace's agent or an agent chat is sent the message. A mention of the sender does nothing."""
        is_anyone_marked = False
        for target in self.resolve(text):
            if target.key is not None and target.key == sender:
                continue
            if target.is_person and target.key is not None:
                self.mentions.record(target.key, room_id, message_id, sender)
                is_anyone_marked = True
                if self._is_owner(target.key):
                    self.agent_manager.notify_owner_mentioned(
                        room_id, f"{sender_name} mentioned you in {room_title}", text
                    )
                continue
            self._ask_agent(target, room_id, room_title, message_id, text, sender, sender_name)
        if is_anyone_marked:
            self.on_pending_changed()

    def _is_owner(self, key: ParticipantKey) -> bool:
        entry = self.roster.participant(key)
        return entry is not None and entry.is_owner

    def _ask_agent(
        self,
        target: MentionTarget,
        room_id: ChatId,
        room_title: str,
        message_id: str,
        text: str,
        sender: ParticipantKey,
        sender_name: str,
    ) -> None:
        """Send the room's message to the chat the mention names: the main chat for ``@agent``, the chat by title
        for one; another room, a chat with no agent, and a visiting agent (nothing reaches one) are left alone."""
        if target.key == WORKSPACE_AGENT_PARTICIPANT_KEY:
            chat_id = self.agent_manager.get_main_chat_id()
            if chat_id is None:
                logger.warning("Room {} mentioned the agent, but the workspace has no main chat", room_id)
                return
        elif target.chat_id is not None:
            chat_id = target.chat_id
        else:
            return
        snapshot = self.agent_manager.get_chat_snapshot(str(chat_id))
        if snapshot is None or snapshot.kind is not ChatKind.AGENT:
            return
        delivered = author_prefix(sender_name, prefix_safe_text(room_title, str(room_id))) + text
        # Annotated before delivery, so the chat's turn is stamped with the sender's own key (section 5.2).
        self.authors.record(chat_id, new_author_annotation(message_id, sender, sender_name, delivered))
        self.agent_manager.note_chat_awaiting(room_id, chat_id)
        self.deliverer.deliver(chat_id, delivered, message_id)
