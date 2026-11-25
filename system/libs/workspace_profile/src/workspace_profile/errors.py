class WorkspaceProfileError(Exception):
    """Base for the errors this library raises."""


class InvalidChordError(WorkspaceProfileError, ValueError):
    """Raised when a chord cannot be normalized into the canonical grammar."""


class InvalidKeybindingDeclarationError(WorkspaceProfileError, ValueError):
    """Raised when an app declares a default chord that is not in the canonical grammar."""


class ProfileWriteError(WorkspaceProfileError, OSError):
    """Raised when an override or unsent file cannot be written."""


class InvalidProfileSnapshotError(WorkspaceProfileError, ValueError):
    """Raised when a client sends a snapshot that is not an object with a whole-number format."""


class InvalidTimezoneError(WorkspaceProfileError, ValueError):
    """Raised when a client names a timezone that is not an IANA zone."""
