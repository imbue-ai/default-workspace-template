"""When each chat was last messaged, kept by the chat app for the memory prioritizer's restart seed.

The prioritizer's recency state is in memory, so without a durable stamp a restart of this
process would hand every chat a fresh grace period before it can be shed. The stamps are
chat-owned state (contracts.md section 17), so they live under ``data/.apps/chat/`` rather
than in the shell's client-activity log.
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

from imbue.imbue_common.mutable_model import MutableModel

DEFAULT_STAMPS_PATH: Final[Path] = Path("data/.apps/chat/last_messaged.json")
_STAMPS_KEY: Final[str] = "last_messaged_at_by_agent_id"


class MessageStampStore(MutableModel):
    """Epoch seconds of each chat's most recent message, in one small JSON file; ``None`` keeps them in memory only."""

    path: Path | None = Field(frozen=True, description="The stamps file, or None for memory only (tests)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stamps: dict[str, float] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        self._stamps = self._load()

    def _load(self) -> dict[str, float]:
        if self.path is None:
            return {}
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.opt(exception=e).warning("Ignored an unreadable message-stamps file at {}", self.path)
            return {}
        stamps = raw.get(_STAMPS_KEY) if isinstance(raw, dict) else None
        if not isinstance(stamps, dict):
            logger.warning("Ignored a message-stamps file of the wrong shape at {}", self.path)
            return {}
        return {
            str(agent_id): float(stamp)
            for agent_id, stamp in stamps.items()
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
        }

    def _save_unlocked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f"{self.path.name}.tmp-{uuid4().hex}")
            temp_path.write_text(json.dumps({_STAMPS_KEY: dict(self._stamps)}), encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to write the message stamps at {}", self.path)

    def record(self, agent_id: str, at: float | None = None) -> None:
        stamp = time.time() if at is None else at
        with self._lock:
            self._stamps[agent_id] = stamp
            self._save_unlocked()

    def forget(self, agent_id: str) -> None:
        with self._lock:
            if agent_id not in self._stamps:
                return
            del self._stamps[agent_id]
            self._save_unlocked()

    def read(self) -> dict[str, float]:
        with self._lock:
            return dict(self._stamps)
