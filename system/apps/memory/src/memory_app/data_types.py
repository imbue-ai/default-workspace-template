"""What the Memory app's routes exchange: the models its API document publishes and every answer is checked against."""

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
