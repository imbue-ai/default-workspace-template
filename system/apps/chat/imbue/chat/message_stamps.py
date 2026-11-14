"""When each chat was last messaged, and when its agent last finished a turn, kept by the chat app.

The message stamps seed the memory prioritizer on restart: its recency state is in memory, so
without a durable stamp a restart of this process would hand every chat a fresh grace period
before it can be shed. The reply stamps, with the message stamps, are when each chat was last
active, which the chat list orders on and shows. The stamps are chat-owned state (contracts.md
section 17), so they live under ``data/.apps/chat/``.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.primitives import ChatId
from imbue.imbue_common.mutable_model import MutableModel

STAMPS_FILENAME: Final[str] = "last_messaged.json"
REPLY_STAMPS_FILENAME: Final[str] = "last_replied.json"
# The on-disk key predates the chat-versus-agent split: its entries are keyed by chat id, which
# is the first agent's id, so the historical name still reads every existing file.
STAMPS_KEY: Final[str] = "last_messaged_at_by_agent_id"
REPLY_STAMPS_KEY: Final[str] = "last_replied_at_by_chat_id"


class MessageStampStore(MutableModel):
    """Epoch seconds of one moment per chat, in one small JSON file; ``None`` keeps them in memory only."""

    path: Path | None = Field(frozen=True, description="The stamps file, or None for memory only (tests)")
    key: str = Field(default=STAMPS_KEY, frozen=True, description="The file's top-level key the stamps live under")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stamps: dict[ChatId, float] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        self._stamps = self._load()

    def _load(self) -> dict[ChatId, float]:
        if self.path is None:
            return {}
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.opt(exception=e).warning("Ignored an unreadable message-stamps file at {}", self.path)
            return {}
        stamps = raw.get(self.key) if isinstance(raw, dict) else None
        if not isinstance(stamps, dict):
            logger.warning("Ignored a message-stamps file of the wrong shape at {}", self.path)
            return {}
        return {
            ChatId(str(chat_id)): float(stamp)
            for chat_id, stamp in stamps.items()
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
        }

    def _save_unlocked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f"{self.path.name}.tmp-{uuid4().hex}")
            temp_path.write_text(json.dumps({self.key: dict(self._stamps)}), encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to write the message stamps at {}", self.path)

    def record(self, chat_id: ChatId, at: float | None = None) -> None:
        stamp = time.time() if at is None else at
        with self._lock:
            self._stamps[chat_id] = stamp
            self._save_unlocked()

    def forget(self, chat_id: ChatId) -> None:
        with self._lock:
            if chat_id not in self._stamps:
                return
            del self._stamps[chat_id]
            self._save_unlocked()

    def read(self) -> dict[ChatId, float]:
        with self._lock:
            return dict(self._stamps)
