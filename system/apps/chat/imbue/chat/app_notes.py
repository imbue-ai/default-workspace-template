"""Short lines another app in the workspace puts into a chat, such as "Claude usage 2%" from AI Usage Limits.

A note is not a message: no agent sees it and nothing replies to it. It is shown between the
chat's messages at the moment it arrived, as a quiet one-line notice. Notes are kept per chat
as an append-only log (``data/.apps/chat/app-notes/<chat_id>.jsonl``, cut back to its newest notes once it
grows past ``MAX_NOTES_KEPT``) and sent live on the
chat's stream as ``app_note`` frames; the page reads the log when it (re)connects, so a note
that arrived while no page was open still shows.

Only processes inside the workspace may add one (the route is loopback-only), and the app that
sends one decides when it does: AI Usage Limits sends nothing unless the user turned its chat
updates on.
"""

import json
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

APP_NOTES_DIRNAME: Final[str] = "app-notes"
APP_NOTE_FRAME_TYPE: Final[str] = "app_note"
# The page reads at most this many of a chat's notes, the newest.
MAX_NOTES_SERVED: Final[int] = 500
# A chat's log is cut back to its newest MAX_NOTES_SERVED notes once it holds more than this many, so it stays bounded.
MAX_NOTES_KEPT: Final[int] = 2 * MAX_NOTES_SERVED


class AppNote(FrozenModel):
    """One line an app put into a chat."""

    note_id: str = Field(description="The note's own id")
    timestamp: str = Field(description="When it arrived (ISO 8601, UTC)")
    source: str = Field(description="The app that sent it, as the user knows it, e.g. 'AI Usage Limits'")
    text: str = Field(description="What the line says, e.g. 'Claude usage 2%'")


class AddAppNoteRequest(FrozenModel):
    """Request body for POST /api/chats/{id}/app-notes."""

    source: str = Field(min_length=1, max_length=80, description="The app sending the note, as the user knows it")
    text: str = Field(min_length=1, max_length=300, description="The line to show")


class AppNotesResponse(FrozenModel):
    """Response from GET /api/chats/{id}/app-notes: the chat's notes, oldest first."""

    notes: tuple[AppNote, ...] = Field(description="The chat's notes, oldest first")


def app_note_frame(note: AppNote) -> dict[str, Any]:
    """The frame a note goes out as on its chat's stream."""
    return {"type": APP_NOTE_FRAME_TYPE, **note.model_dump()}


class AppNoteStore(MutableModel):
    """The notes of every chat, one log per chat under ``root``; ``root`` None keeps them in memory (tests)."""

    root: Path | None = Field(frozen=True, description="The folder holding one log per chat, or None for memory")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _memory: dict[str, list[AppNote]] = PrivateAttr(default_factory=dict)

    def _path(self, chat_id: str) -> Path | None:
        return None if self.root is None else self.root / f"{chat_id}.jsonl"

    def add(self, chat_id: str, source: str, text: str) -> AppNote:
        note = AppNote(
            note_id=uuid4().hex,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=source,
            text=text,
        )
        path = self._path(chat_id)
        with self._lock:
            if path is None:
                held = self._memory.setdefault(chat_id, [])
                held.append(note)
                if len(held) > MAX_NOTES_KEPT:
                    del held[:-MAX_NOTES_SERVED]
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as log:
                    log.write(note.model_dump_json() + "\n")
                _trim_log(path)
        return note

    def notes(self, chat_id: str) -> tuple[AppNote, ...]:
        path = self._path(chat_id)
        if path is None:
            return tuple(self._memory.get(chat_id, [])[-MAX_NOTES_SERVED:])
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            return ()
        except OSError as error:
            logger.warning("Could not read chat {}'s app notes: {}", chat_id, error)
            return ()
        notes: list[AppNote] = []
        for line in lines[-MAX_NOTES_SERVED:]:
            if not line.strip():
                continue
            try:
                notes.append(AppNote.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError):
                logger.warning("Skipping an unreadable app note line in chat {}", chat_id)
        return tuple(notes)


def _trim_log(path: Path) -> None:
    """Cut a chat's log back to its newest ``MAX_NOTES_SERVED`` lines once it holds more than ``MAX_NOTES_KEPT``."""
    lines = path.read_text().splitlines(keepends=True)
    if len(lines) <= MAX_NOTES_KEPT:
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(lines[-MAX_NOTES_SERVED:]))
    temporary.replace(path)
