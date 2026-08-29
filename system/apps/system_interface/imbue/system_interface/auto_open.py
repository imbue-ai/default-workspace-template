"""Which chats have had their tab surfaced, remembered across restarts.

A chat created with an auto-open label is owed its tab exactly once: opened in
front of the user the first time a client can show it, and never again -- a
tab the user has since closed stays closed through every later snapshot and
restart. That "once" has to survive the interface restarting, because the one
flow that most needs it (an update run started while nobody was looking) ends
by restarting the interface itself. So the delivered set lives on disk beside
the workspace layout, not in memory.
"""

import json
import os
import threading
from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

_LEDGER_FILENAME = "auto_opened_chats.json"


def ledger_path_for_layout_dir(layout_dir: Path) -> Path:
    """Where the ledger lives for a workspace whose layouts are saved under ``layout_dir``."""
    return layout_dir / _LEDGER_FILENAME


class AutoOpenLedger(MutableModel):
    """The set of chat agent ids whose auto-open has been delivered to a client.

    A ``path`` of None keeps the set in memory only (tests, and a server with
    no workspace to persist into).
    """

    model_config = {"extra": "forbid", "frozen": False}

    path: Path | None
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _delivered: set[str] = PrivateAttr(default_factory=set)

    def model_post_init(self, context: object, /) -> None:
        self._delivered = self._load()

    def _load(self) -> set[str]:
        if self.path is None or not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _loguru_logger.opt(exception=e).warning("Ignoring an unreadable auto-open ledger at {}", self.path)
            return set()
        delivered = data.get("delivered") if isinstance(data, dict) else None
        if not isinstance(delivered, list):
            _loguru_logger.warning(
                "Ignoring an auto-open ledger of the wrong shape at {} (expected a JSON object with a "
                "'delivered' list, got {})",
                self.path,
                type(data).__name__,
            )
            return set()
        return {str(agent_id) for agent_id in delivered}

    def _save_unlocked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps({"delivered": sorted(self._delivered)}), encoding="utf-8")
            os.replace(tmp_path, self.path)
        except OSError as e:
            _loguru_logger.opt(exception=e).warning("Failed to write the auto-open ledger at {}", self.path)

    def is_delivered(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._delivered

    def mark_delivered(self, agent_id: str) -> None:
        with self._lock:
            if agent_id in self._delivered:
                return
            self._delivered.add(agent_id)
            self._save_unlocked()

    def forget(self, agent_id: str) -> None:
        """Drop a destroyed agent's entry, so the ledger only ever names live chats."""
        with self._lock:
            if agent_id not in self._delivered:
                return
            self._delivered.discard(agent_id)
            self._save_unlocked()
