"""The workspace's main chat: where a text with no chat of its own goes, listed first in the chat list.

The main chat is a pointer, like a star on one chat, kept in one small JSON file beside the chat app's
other state; no chat is special in itself, so a handoff, a crash, or a relaunch changes nothing about
which chat it is. The welcome chat the Imbue Studio app seeds (``chat_seed.py``) takes it first.
When the chat it names is gone (deleted, or never listed), another chat takes it at once: the
welcome chat if one is still listed, else the chat messaged most recently, else one still awaiting
its first send. A chat another agent started (a helper) never takes it. With no chat at all there
is no main chat until one exists: the chat list opens a chat awaiting its first send, which takes it.
The welcome chat is recorded as the main chat before any of its own files are written, so a chat app
stopped part-way through seeding it never leaves a welcome chat with no main chat file.

A workspace that had chats before there was a main chat (one updated from an earlier version) has
no main chat file and chats already listed when this app first reads the agent list. Rather than
hand the main chat to whichever of those was messaged last (often the chat that ran the update),
the chat app opens a new one on ``MAIN_CHAT_INTRODUCTION`` and makes it the main chat, or makes a
"Main" chat an earlier, interrupted opening left behind the main chat.
"""

import json
import os
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

MAIN_CHAT_FILENAME: Final[str] = "main_chat.json"
# What the seeded chat is called: a name the app chose, so the first message never renames it.
MAIN_CHAT_NAME: Final[str] = "Main"
_CHAT_ID_KEY: Final[str] = "chat_id"

# What the main chat opened for a workspace that had chats before there was a main chat says.
MAIN_CHAT_INTRODUCTION: Final[tuple[SeedTurn, ...]] = (
    SeedTurn(
        role=SeedRole.ASSISTANT,
        text=(
            "This is your **main chat**.\n\n"
            "Your workspace now has a main chat: the default place to talk to me. It stays at the top of "
            "the chat list, it's where a message typed into the desktop launcher lands, and it never goes "
            "to sleep, so it's always ready to pick up where you left off.\n\n"
            "All your other chats are still here, below it. They're still good for focused pieces of work, "
            'and you can make any of them the main chat from its menu ("Make main chat").\n\n'
            "What would you like to work on?"
        ),
    ),
)


class MainChatCandidate(FrozenModel):
    """A chat that may be the main chat, with what choosing among them reads."""

    chat_id: ChatId = Field(description="The chat's id")
    seeded_at: datetime | None = Field(
        description="When the chat's seed was written, for a chat that opened on one (the welcome chat); else None"
    )
    last_messaged_at: float | None = Field(
        description="Epoch seconds of its last message, or None when never messaged"
    )


@pure
def resolved_main_chat_id(current: ChatId | None, candidates: Sequence[MainChatCandidate]) -> ChatId | None:
    """The main chat among ``candidates``: ``current`` while it is one of them, else the earliest-seeded
    welcome chat, else the chat messaged most recently (one never messaged counting as oldest); None with none."""
    if current is not None and any(candidate.chat_id == current for candidate in candidates):
        return current
    seeded = [candidate for candidate in candidates if candidate.seeded_at is not None]
    if seeded:
        return min(seeded, key=lambda candidate: (candidate.seeded_at, candidate.chat_id)).chat_id
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.last_messaged_at or 0.0).chat_id


class MainChatStore(MutableModel):
    """The main chat's id, in one small JSON file; ``None`` keeps it in memory only."""

    path: Path | None = Field(frozen=True, description="The file, or None for memory only (tests)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _chat_id: ChatId | None = PrivateAttr(default=None)
    _is_file_written: bool = PrivateAttr(default=False)

    def model_post_init(self, context: object, /) -> None:
        self._is_file_written = self.path is not None and self.path.exists()
        self._chat_id = self._load()

    def _load(self) -> ChatId | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.opt(exception=e).warning("Ignored an unreadable main-chat file at {}", self.path)
            return None
        chat_id = raw.get(_CHAT_ID_KEY) if isinstance(raw, dict) else None
        if not isinstance(chat_id, str) or not AGENT_ID_PATTERN.fullmatch(chat_id):
            logger.warning("Ignored a main-chat file of the wrong shape at {}", self.path)
            return None
        return ChatId(chat_id)

    def _save_unlocked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f"{self.path.name}.tmp-{uuid4().hex}")
            temp_path.write_text(json.dumps({_CHAT_ID_KEY: self._chat_id}), encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to write the main chat at {}", self.path)

    def read(self) -> ChatId | None:
        with self._lock:
            return self._chat_id

    def write(self, chat_id: ChatId | None) -> None:
        with self._lock:
            if chat_id == self._chat_id:
                return
            self._chat_id = chat_id
            self._is_file_written = True
            self._save_unlocked()

    def is_file_never_written(self) -> bool:
        """Whether the store keeps a file that has never been written: absent when loaded, and nothing written
        since. A file there at load counts as written even when it names no chat or cannot be read. False for a
        store in memory only, which has no workspace history behind it."""
        with self._lock:
            return self.path is not None and not self._is_file_written
