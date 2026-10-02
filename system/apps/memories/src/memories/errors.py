class MemoriesError(Exception):
    """Base error for the memories app."""


class ServeError(MemoriesError):
    """The page server cannot be started or stopped as asked."""


class NoteNotFoundError(MemoriesError, LookupError):
    """No note by the name asked for."""


class NoteNameError(MemoriesError, ValueError):
    """A note name that is not a plain Markdown file name inside the notes folder."""


class NoteChangedError(MemoriesError):
    """The note changed on disk since the page read it (a chat wrote to it meanwhile)."""


class NoteWriteError(MemoriesError, OSError):
    """A note or the index could not be written or deleted."""


class ControlsReadError(MemoriesError):
    """The memory switches, or Claude's settings file they also set, could not be read as expected."""
