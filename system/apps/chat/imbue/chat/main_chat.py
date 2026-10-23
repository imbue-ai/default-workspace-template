"""The workspace's main chat: where a text with no chat of its own goes, listed first in the chat list.

The main chat is a pointer, like a star on one chat, kept in one small JSON file beside the chat app's
other state; no chat is special in itself, so a handoff, a crash, or a relaunch changes nothing about
which chat it is. The welcome chat the Imbue Studio app seeds (``chat_seed.py``) takes it first.
When the chat it names is gone (deleted, or never listed), a new chat named ``MAIN_CHAT_NAME``,
awaiting its first send, takes it at once; no other chat is promoted in its place. A workspace with
no main chat yet gives it to the welcome chat if one is listed, else the chat messaged most recently,
else one still awaiting its first send. A chat another agent started (a helper) never takes it.

A chat made the main chat keeps its own name. The one it replaces, when still named
``MAIN_CHAT_NAME``, is renamed (``former_main_chat_name``) so no chat called Main is left below the
main chat; a name anyone else chose stays.
"""

import json
import os
import threading
from collections.abc import Iterable
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.naming import AUTO_NAME_WORD
from imbue.chat.naming import first_free_numbered_name
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

MAIN_CHAT_FILENAME: Final[str] = "main_chat.json"
# What the seeded chat is called: a name the app chose, so the first message never renames it.
MAIN_CHAT_NAME: Final[str] = "Main"
# What marks the chat that was the main chat, after the first free "Chat N" it is renamed to.
FORMER_MAIN_CHAT_SUFFIX: Final[str] = f" (was: {MAIN_CHAT_NAME})"
_CHAT_ID_KEY: Final[str] = "chat_id"


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
def adopted_main_chat_id(candidates: Sequence[MainChatCandidate]) -> ChatId | None:
    """The chat a workspace with no main chat yet gives it to: the earliest-seeded welcome chat, else the chat
    messaged most recently (one never messaged counting as oldest); None with none."""
    seeded = [candidate for candidate in candidates if candidate.seeded_at is not None]
    if seeded:
        return min(seeded, key=lambda candidate: (candidate.seeded_at, candidate.chat_id)).chat_id
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.last_messaged_at or 0.0).chat_id


@pure
def is_main_chat_name(name: str) -> bool:
    """Whether a chat is named ``MAIN_CHAT_NAME``, however it is capitalized."""
    return name.strip().casefold() == MAIN_CHAT_NAME.casefold()


@pure
def former_main_chat_name(taken_names: Iterable[str]) -> str:
    """The name a chat still named ``MAIN_CHAT_NAME`` takes when another chat becomes the main chat: the first
    "Chat N" free both bare and marked, then marked ("Chat 7 (was: Main)")."""
    taken = list(taken_names)
    unmarked = [name.removesuffix(FORMER_MAIN_CHAT_SUFFIX) for name in taken]
    return first_free_numbered_name(AUTO_NAME_WORD, [*taken, *unmarked]) + FORMER_MAIN_CHAT_SUFFIX


class MainChatStore(MutableModel):
    """The main chat's id, in one small JSON file; ``None`` keeps it in memory only."""

    path: Path | None = Field(frozen=True, description="The file, or None for memory only (tests)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _chat_id: ChatId | None = PrivateAttr(default=None)

    def model_post_init(self, context: object, /) -> None:
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
            self._save_unlocked()
