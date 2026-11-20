"""The memory notes' ids, as types of their own, for the pydantic models that carry them.

Plain ``str`` subclasses, so anything that compares or formats them as text keeps working.
``changes.py`` stays stdlib-only and does not use these; the models that serve its records do.
"""

from imbue.imbue_common.primitives import NonEmptyStr


class NoteId(NonEmptyStr):
    """A note's stable id: its file's stem, or ``index-<hash>`` for a note written straight into the index."""


class NoteVersion(NonEmptyStr):
    """A hash of a note's saved text, which a change names so a note edited elsewhere is not overwritten."""


class MemoryChangeId(NonEmptyStr):
    """A change record's id: when it was made, then a random part."""
