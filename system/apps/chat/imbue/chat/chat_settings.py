"""The chat app's workspace-wide settings: the fast mode and idle compaction a new chat starts with, the notices
shown, and which suggestions are offered.

One small JSON file beside the chat app's other state (``data/.apps/chat/settings.json``),
read on every use so an edit from another process lands without a restart, and written whole.
``path`` None keeps the settings in memory, for tests and a manager built with no workspace.
"""

import json
import os
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

SETTINGS_FILENAME: Final[str] = "settings.json"
# How many of the user's turns a chat in auto mode runs with fast mode on before the chat app
# switches it to standard speed.
DEFAULT_FAST_MODE_TURN_LIMIT: Final[int] = 2


class FastModeMode(LowerCaseStrEnum):
    """The fast-mode setting a chat runs under."""

    # Standard speed for the whole chat.
    OFF = auto()
    # Fast for the first turns, then standard speed once the workspace's turn limit is reached.
    AUTO = auto()
    # Fast for the whole chat.
    ON = auto()


class ChatSettings(FrozenModel):
    """What the settings file holds. Every field has a default, so an older file reads whole."""

    fast_mode_default: FastModeMode = Field(
        default=FastModeMode.OFF,
        description="The fast mode a new chat starts in",
    )
    fast_mode_turn_limit: int = Field(
        default=DEFAULT_FAST_MODE_TURN_LIMIT,
        ge=1,
        description="User turns a chat in auto mode runs fast for before it is switched to standard speed",
    )
    is_fast_mode_notice_shown: bool = Field(
        default=False,
        description="Whether the one-time notice explaining the first automatic switch to standard speed has been shown",
    )
    autocompact_default: bool = Field(
        default=True,
        description="Whether a new chat starts with idle compaction on",
    )
    is_autocompact_notice_shown: bool = Field(
        default=False,
        description="Whether the one-time notice explaining idle compaction has been shown",
    )
    is_next_step_suggestions_enabled: bool = Field(
        default=True,
        description="Whether suggested replies are offered under an agent's reply",
    )
    is_activity_suggestions_enabled: bool = Field(
        default=True,
        description="Whether things to try in Studio are offered while an agent works on a reply",
    )
    is_new_chat_suggestions_enabled: bool = Field(
        default=True,
        description="Whether things to try in Studio are offered on a chat with no message yet",
    )


class SuggestionSettingsUpdate(FrozenModel):
    """A change to the suggestion switches alone: a switch left out, or null, stays as it is.

    Replacing the settings whole would let a writer that knows fewer fields reset the rest, so the
    writers of these switches (a strip's disable link, the Imbue Studio app's workspace settings) send
    only the switches they mean to change.
    """

    is_next_step_suggestions_enabled: bool | None = Field(
        default=None, description="The new value of the next-step switch; None leaves it"
    )
    is_activity_suggestions_enabled: bool | None = Field(
        default=None, description="The new value of the activity switch; None leaves it"
    )
    is_new_chat_suggestions_enabled: bool | None = Field(
        default=None, description="The new value of the new-chat switch; None leaves it"
    )

    def apply_to(self, settings: ChatSettings) -> ChatSettings:
        """``settings`` with the switches this update names set, and everything else as it was."""
        updated = settings
        if self.is_next_step_suggestions_enabled is not None:
            updated = updated.model_copy_update(
                to_update(updated.field_ref().is_next_step_suggestions_enabled, self.is_next_step_suggestions_enabled)
            )
        if self.is_activity_suggestions_enabled is not None:
            updated = updated.model_copy_update(
                to_update(updated.field_ref().is_activity_suggestions_enabled, self.is_activity_suggestions_enabled)
            )
        if self.is_new_chat_suggestions_enabled is not None:
            updated = updated.model_copy_update(
                to_update(updated.field_ref().is_new_chat_suggestions_enabled, self.is_new_chat_suggestions_enabled)
            )
        return updated


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
        except (OSError, ValueError) as e:
            logger.warning("Ignoring an unreadable chat settings file at {}: {}", self.path, e)
            return ChatSettings()
        try:
            return ChatSettings.model_validate(payload)
        except ValidationError as e:
            logger.warning("Ignoring a chat settings file at {} that does not fit the settings: {}", self.path, e)
            return ChatSettings()

    def write(self, settings: ChatSettings) -> None:
        with self._lock:
            self._write_locked(settings)

    def update(self, change: Callable[[ChatSettings], ChatSettings]) -> ChatSettings:
        """Write ``change`` of the settings as they stand, and answer what was written. The read and the write
        are one step under the store's lock, so two changes to different fields both land."""
        with self._lock:
            settings = change(self.read())
            self._write_locked(settings)
        return settings

    def _write_locked(self, settings: ChatSettings) -> None:
        if self.path is None:
            self._in_memory = settings
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, self.path)
