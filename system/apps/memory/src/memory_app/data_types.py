"""What the Memory app's routes exchange: the models its API document publishes and every answer is checked against."""

from datetime import datetime
from pathlib import Path

from pydantic import Field

from agent_memory.notes import IndexOutline
from agent_memory.notes import Note
from agent_memory.notes import NoteDraft
from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class NotesListing(FrozenModel):
    """Every remembered note, in the index's order."""

    notes: tuple[Note, ...] = Field(description="The notes")
    folder: Path = Field(description="The memory folder the notes are read from")
    index: IndexOutline = Field(description="The index (MEMORY.md) line by line, as the navigation mirrors it")
    index_line_limit: int = Field(description="How long an index line may be before the editor warns about it")


class NoteChange(FrozenModel):
    """New content for an existing note."""

    version: str = Field(description="The note's version when the editor loaded it")
    note: NoteDraft = Field(description="The note's new content")


class NoteDeletion(FrozenModel):
    """Which version of a note to delete."""

    version: str = Field(description="The note's version when the editor loaded it")


class DeletedNote(FrozenModel):
    """The note that was deleted."""

    id: str = Field(description="The deleted note's id")


class MemoryAppChangedFile(FrozenModel):
    """One file a change touched: its text before and after."""

    name: str = Field(description="The file's name in the memory folder")
    before: str | None = Field(description="Its text before the change, or null if the change created it")
    after: str | None = Field(description="Its text after the change, or null if the change deleted it")
    is_index_lines: bool = Field(
        description="Whether this is the index holding only the change's own lines (a note's pointer line, say), "
        "rather than the whole file"
    )


class MemoryAppChange(FrozenModel):
    """What one agent turn changed in one note (with its index line), or in the index's other lines, undone as one."""

    id: str = Field(description="The change's id; ids sort by time")
    time: datetime = Field(description="When the turn first changed memory (ISO 8601, UTC)")
    summary: str = Field(description="What changed, in one line, e.g. 'added the note \"Tea\" and its index line'")
    chat_id: str = Field(description="The chat whose agent made it")
    agent_name: str = Field(description="The name of the agent that made it")
    note: str | None = Field(description="The note file the change belongs to; null for the index's other lines")
    files: tuple[MemoryAppChangedFile, ...] = Field(description="Every file it changed")
    undone_at: datetime | None = Field(description="When it was undone, or null")


class MemoryAppChangesListing(FrozenModel):
    """Every change an agent made to the notes, newest first."""

    changes: tuple[MemoryAppChange, ...] = Field(description="The changes")


class UndoRequest(FrozenModel):
    """How to undo a change."""

    is_forced: bool = Field(default=False, description="Undo even if a file was changed again since")
