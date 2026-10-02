"""The record of notes the user deleted or edited here, which chats are told about so they do not undo the change.

A chat that saw a note still has it in its conversation after the user deletes or edits it, and the next time it
saves that note it writes back what it remembers: the deleted note returns, or the edit is reverted. So every delete
and edit appends a line to this record, and ``system/scripts/agent_memory_context.py`` turns the record into a
notice each chat reads before every message (Claude through a UserPromptSubmit hook, pi through its memory extension).

A line holds only the note's file name, what was done and when -- never what the note said, so a delete still
erases the content. Lines older than ``CHANGE_MAX_AGE`` are dropped on the next write: by then every chat that saw
the note has long restarted or ended.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from memories.errors import NoteWriteError
from memories.notes import write_atomically

CHANGE_MAX_AGE: Final[timedelta] = timedelta(days=30)


class NoteChangeKind(UpperCaseStrEnum):
    """What the user did to a note."""

    DELETED = auto()
    EDITED = auto()


class NoteChange(FrozenModel):
    """One delete or edit the user made in the app."""

    file_name: str = Field(description="The note's file name in the notes folder")
    change: NoteChangeKind = Field(description="What was done to it")
    at: datetime = Field(description="When, in UTC")


def parse_changes(text: str) -> list[NoteChange]:
    """The record's entries, skipping (with a warning) any line that is not one: a torn write, a hand edit."""
    changes: list[NoteChange] = []
    skipped = 0
    for line in text.splitlines():
        try:
            changes.append(NoteChange.model_validate(json.loads(line)))
        except ValueError:
            skipped += 1
    if skipped:
        logger.warning("Skipped {} unreadable line(s) in the record of the user's note changes", skipped)
    return changes


@pure
def render_changes(changes: Sequence[NoteChange]) -> str:
    return "".join(f"{change.model_dump_json()}\n" for change in changes)


def read_changes(log_path: Path) -> list[NoteChange]:
    try:
        return parse_changes(log_path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return []


def record_note_change(log_path: Path, file_name: str, change: NoteChangeKind, now: datetime) -> None:
    """Append one change, dropping entries older than ``CHANGE_MAX_AGE``. The caller serializes calls."""
    try:
        existing = read_changes(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise NoteWriteError(f"could not update the change record {log_path}: {e}") from e
    kept = [entry for entry in existing if now - entry.at <= CHANGE_MAX_AGE]
    write_atomically(log_path, render_changes([*kept, NoteChange(file_name=file_name, change=change, at=now)]))
