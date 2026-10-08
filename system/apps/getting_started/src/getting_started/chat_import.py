"""The "Bring in your chats" card's state: what the import-chats skill has imported, and whether the user put the
card away.

The skill's script (``.agents/skills/import-chats/scripts/import_chats.py``) records each source it imports in
``data/.skills/import-chats/status.json``; this module only reads that file. A source the file says is still
importing, but whose sync process is gone, was cut off (a restart, a shed), so it is answered as failed rather than
as an import that never ends. Putting the card away is the app's own state, kept under its state directory.
"""

import os
from pathlib import Path
from typing import Any
from typing import Final
from typing import Literal

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel

# Written by the import-chats skill's script, relative to the repo root the app runs from.
CHAT_IMPORT_STATUS_PATH: Final[Path] = Path("data/.skills/import-chats/status.json")
DISMISSAL_FILENAME: Final[str] = "chat_import.json"

ChatImportState = Literal["importing", "imported", "needs_sign_in", "failed"]

_INTERRUPTED_DETAIL: Final[str] = "The import stopped before it finished."


class ChatImportSource(FrozenModel):
    """One source's record in the skill's status file."""

    model_config = ConfigDict(extra="ignore")

    state: ChatImportState = Field(description="Where the source's last import got to")
    conversations: int = Field(default=0, ge=0, description="Pages imported so far, one per conversation")
    updated_at: str = Field(default="", description="When the record was last written, ISO 8601")
    detail: str = Field(default="", description="Why the last import failed; empty otherwise")
    pid: int | None = Field(default=None, description="The sync's process while it is importing")


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _settled(source: ChatImportSource) -> ChatImportSource:
    if source.state == "importing" and (source.pid is None or not is_process_alive(source.pid)):
        return source.model_copy(update={"state": "failed", "detail": _INTERRUPTED_DETAIL, "pid": None})
    return source


def read_chat_import_sources(status_path: Path) -> dict[str, ChatImportSource]:
    """Each source the skill has recorded, by key; a record of another shape is skipped (logged)."""
    document = read_json_object(status_path)
    if document is None:
        return {}
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, dict):
        logger.warning("Skipped the chat import status {}: it has no sources object", status_path)
        return {}
    sources: dict[str, ChatImportSource] = {}
    for key, raw in raw_sources.items():
        try:
            sources[str(key)] = _settled(ChatImportSource.model_validate(raw))
        except ValidationError as e:
            logger.warning("Skipped the chat import record for {} in {}: {}", key, status_path, e)
    return sources


class ChatImportStore(FrozenModel):
    """Reads the skill's status file and keeps the card's dismissal."""

    status_path: Path = Field(description="The import-chats skill's status file")
    dismissal_path: Path = Field(description="Where the app records that the user put the card away")

    def is_dismissed(self) -> bool:
        document = read_json_object(self.dismissal_path)
        return document is not None and document.get("is_dismissed") is True

    def dismiss(self) -> None:
        write_json_atomic(self.dismissal_path, {"is_dismissed": True})

    def wire_json(self) -> dict[str, Any]:
        """The card's state as ``GET /api/chat-import`` answers it."""
        sources = read_chat_import_sources(self.status_path)
        return {
            "is_dismissed": self.is_dismissed(),
            "sources": {key: source.model_dump(exclude={"pid"}) for key, source in sources.items()},
        }
