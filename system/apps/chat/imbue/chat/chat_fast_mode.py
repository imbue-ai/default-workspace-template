"""A chat's fast mode: off, auto, or on, kept in the chat's own folder.

Fast mode has three settings the model picker offers: ``off`` (standard speed for the whole
chat), ``auto`` (fast for the first turns, then standard speed once the workspace's turn limit
is reached), and ``on`` (fast throughout). A new chat starts in the workspace's default mode
(``chat_settings.py``); the choice then belongs to the chat, so it travels with the chat across
handoffs and survives reloads, which is why it is a file under the chat's folder beside its
record rather than page state. ``is_switched`` is auto's memory: once the page has switched the
chat to standard speed, a user who turns fast mode back on keeps it.
"""

from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.chat.chat_setting_files import read_chat_setting_file
from imbue.chat.chat_setting_files import write_chat_setting_file
from imbue.chat.chat_settings import FastModeMode
from imbue.imbue_common.frozen_model import FrozenModel

FAST_MODE_FILENAME: Final[str] = "fast_mode.json"


class ChatFastModeState(FrozenModel):
    """Which fast mode a chat is in, and for auto, whether its fast turns have run."""

    mode: FastModeMode = Field(description="off, auto or on")
    is_switched: bool = Field(
        default=False,
        description="Auto only: the chat has run its fast turns and was switched to standard speed",
    )

    @property
    def launches_fast(self) -> bool:
        """Whether an agent launched for this chat now should start in fast mode."""
        return self.mode is FastModeMode.ON or (self.mode is FastModeMode.AUTO and not self.is_switched)


def read_fast_mode_state(chat_dir: Path) -> ChatFastModeState | None:
    """The chat's fast mode as written; None for a chat that has none yet, or an unreadable file (warned about)."""
    return read_chat_setting_file(chat_dir, FAST_MODE_FILENAME, ChatFastModeState, "fast mode")


def write_fast_mode_state(chat_dir: Path, state: ChatFastModeState) -> None:
    """Write the chat's fast mode whole (a temp file renamed into place)."""
    write_chat_setting_file(chat_dir, FAST_MODE_FILENAME, state)
