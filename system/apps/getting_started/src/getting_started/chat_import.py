"""The "Bring in your chats" card's state: what the import-chats skill has imported, and whether the user put the
card away.

The skill's script (``.agents/skills/import-chats/scripts/import_chats.py``) records each source it imports in
``data/.skills/import-chats/status.json``; this module only reads that file. A source the file says is still
importing, but whose sync process is gone or whose record has stopped being rewritten, was cut off (a restart, a
shed), so it is answered as failed rather than as an import that never ends. Putting the card away is the app's own state, kept under its state directory.
"""

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
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
from imbue.imbue_common.model_update import to_update

# Written by the import-chats skill's script, relative to the repo root the app runs from.
CHAT_IMPORT_STATUS_PATH: Final[Path] = Path("data/.skills/import-chats/status.json")
DISMISSAL_FILENAME: Final[str] = "chat_import.json"

ChatImportState = Literal["importing", "imported", "needs_sign_in", "failed"]

_INTERRUPTED_DETAIL: Final[str] = "The import stopped before it finished."

# A running sync rewrites its record every ten seconds (the script's PROGRESS_INTERVAL_SECONDS), from its install on.
# A record that says importing but is older than this belongs to a sync that died, even if its pid now names some
# other process, as it can after the container restarts.
HEARTBEAT_STALE_AFTER: Final[timedelta] = timedelta(minutes=2)


class ChatImportSource(FrozenModel):
    """One source's record in the skill's status file."""

    model_config = ConfigDict(extra="ignore")

    state: ChatImportState = Field(description="Where the source's last import got to")
    conversations: int = Field(default=0, ge=0, description="Pages imported so far, one per conversation")
    updated_at: str = Field(default="", description="When the record was last written, ISO 8601")
    detail: str = Field(default="", description="Why the last import failed; empty otherwise")
    pid: int | None = Field(default=None, description="The sync's process while it is importing")
    fetched: int | None = Field(
        default=None, ge=0, description="Conversations the running import has fetched; None when it reports no total"
    )
    to_fetch: int | None = Field(
        default=None, ge=0, description="Conversations the running import set out to fetch; None when unknown"
    )


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_heartbeat_stale(updated_at: str, now: datetime) -> bool:
    """Whether a record written at ``updated_at`` has gone longer than a running sync ever leaves it.

    A record with no readable time says nothing either way, so it is never called stale; the pid still decides.
    """
    try:
        written = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    return now - written > HEARTBEAT_STALE_AFTER


def _settled(source: ChatImportSource, now: datetime) -> ChatImportSource:
    if source.state == "importing" and (
        source.pid is None or not is_process_alive(source.pid) or is_heartbeat_stale(source.updated_at, now)
    ):
        fields = source.field_ref()
        return source.model_copy_update(
            to_update(fields.state, "failed"),
            to_update(fields.detail, _INTERRUPTED_DETAIL),
            to_update(fields.pid, None),
            to_update(fields.fetched, None),
            to_update(fields.to_fetch, None),
        )
    return source


def read_chat_import_sources(status_path: Path, now: datetime | None = None) -> dict[str, ChatImportSource]:
    """Each source the skill has recorded, by key, as of ``now`` (the current time when omitted); a record of another
    shape is skipped (logged)."""
    as_of = now if now is not None else datetime.now(timezone.utc)
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
            sources[str(key)] = _settled(ChatImportSource.model_validate(raw), as_of)
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
