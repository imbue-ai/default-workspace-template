class WorkspaceProfileError(Exception):
    """Base for the errors this library raises."""


class InvalidChordError(WorkspaceProfileError, ValueError):
    """Raised when a chord cannot be normalized into the canonical grammar."""


class ProfileWriteError(WorkspaceProfileError, OSError):
    """Raised when an override or unsent file cannot be written."""
