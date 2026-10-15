"""A chat's idle compaction setting (on or off), kept in the chat's own folder.

Idle compaction is the chat app's sweep (``autocompact.py``) compacting a chat's context just
before its prompt cache expires. A new chat starts with the workspace's default
(``chat_settings.py``), copied into the chat's folder at its first launch; the choice then
belongs to the chat, so it travels with the chat across handoffs and rebinds. A chat with no
file reads the workspace default.
"""

from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.chat.chat_setting_files import read_chat_setting_file
from imbue.chat.chat_setting_files import write_chat_setting_file
from imbue.imbue_common.frozen_model import FrozenModel

AUTOCOMPACT_FILENAME: Final[str] = "autocompact.json"
AUTOCOMPACT_LABEL_KEY: Final[str] = "autocompact"


class ChatAutocompactState(FrozenModel):
    """Whether the chat app's idle compaction sweep may compact this chat."""

    is_enabled: bool = Field(description="Whether the sweep may compact this chat while it is idle")

    @property
    def label(self) -> str:
        """The ``autocompact=on|off`` label the chat's agents carry, so ``mngr list`` shows the setting."""
        return f"{AUTOCOMPACT_LABEL_KEY}={'on' if self.is_enabled else 'off'}"


def read_autocompact_state(chat_dir: Path) -> ChatAutocompactState | None:
    """The chat's setting as written; None for a chat that has none yet, or an unreadable file (warned about)."""
    return read_chat_setting_file(chat_dir, AUTOCOMPACT_FILENAME, ChatAutocompactState, "autocompact")


def write_autocompact_state(chat_dir: Path, state: ChatAutocompactState) -> None:
    """Write the chat's setting whole (a temp file renamed into place)."""
    write_chat_setting_file(chat_dir, AUTOCOMPACT_FILENAME, state)
