"""The chat app's workspace-wide settings: how long a new chat runs fast, and whether the user has been told.

One small JSON file beside the chat app's other state (``data/.apps/chat/settings.json``),
read on every use so an edit from another process lands without a restart, and written whole.
``path`` None keeps the settings in memory, for tests and a manager built with no workspace.
"""

import json
import os
import threading
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

DEFAULT_SETTINGS_PATH: Final[Path] = Path("data/.apps/chat/settings.json")
# How many of the user's turns a new chat runs with fast mode on before the chat app turns
# it off. Zero means a new chat never launches fast.
DEFAULT_FAST_MODE_TURN_LIMIT: Final[int] = 5


class ChatSettings(FrozenModel):
    """What the settings file holds. Every field has a default, so an older file reads whole."""

    fast_mode_turn_limit: int = Field(
        default=DEFAULT_FAST_MODE_TURN_LIMIT,
        ge=0,
        description="User turns a new chat runs fast for before fast mode is turned off; 0 launches every chat at standard speed",
    )
    is_fast_mode_notice_shown: bool = Field(
        default=False,
        description="Whether the one-time notice explaining the first automatic switch to standard speed has been shown",
    )


class ChatSettingsStore(MutableModel):
    """The settings file: read whole on every read, written whole under a lock."""

    path: Path | None = Field(frozen=True, description="The settings file, or None for memory only (tests)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _in_memory: ChatSettings = PrivateAttr(default_factory=ChatSettings)

    def read(self) -> ChatSettings:
        """The settings as they stand; an absent or unreadable file reads as the defaults, with a warning for the latter."""
        if self.path is None:
            return self._in_memory
        if not self.path.exists():
            return ChatSettings()
        try:
            payload = json.loads(self.path.read_text())
            return ChatSettings.model_validate(payload)
        except (OSError, ValueError, ValidationError) as e:
            logger.warning("Ignoring an unreadable chat settings file at {}: {}", self.path, e)
            return ChatSettings()

    def write(self, settings: ChatSettings) -> None:
        with self._lock:
            if self.path is None:
                self._in_memory = settings
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
            os.replace(temp_path, self.path)
