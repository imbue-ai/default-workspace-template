"""A chat's idle compaction setting (on or off), kept in the chat's own folder.

Idle compaction is the chat app's sweep (``autocompact.py``) compacting a chat's context just
before its prompt cache expires. A new chat starts with the workspace's default
(``chat_settings.py``), copied into the chat's folder at its first launch; the choice then
belongs to the chat, so it travels with the chat across handoffs and rebinds, exactly like fast
mode (``chat_fast_mode.py``). A chat created before this setting existed has no file and reads
the workspace default until the user changes it.
"""

import json
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

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
    path = chat_dir / AUTOCOMPACT_FILENAME
    if not path.is_file():
        return None
    try:
        return ChatAutocompactState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as e:
        logger.warning("Ignoring an unreadable autocompact file at {}: {}", path, e)
        return None


def write_autocompact_state(chat_dir: Path, state: ChatAutocompactState) -> None:
    """Write the chat's setting whole (a temp file renamed into place)."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / AUTOCOMPACT_FILENAME
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)
