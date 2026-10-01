class WorkspaceLayoutError(Exception):
    """Base error for everything in the workspace_layout library."""


class InvalidLayoutValueError(WorkspaceLayoutError, ValueError):
    """A value on the shell's layout wire (an id, a requester) does not satisfy its rule."""


class ShellOpError(WorkspaceLayoutError):
    """The shell did not carry out what it was asked."""


class ShellUnreachableError(ShellOpError):
    """The shell could not be reached (it is down, restarting, or did not answer in time)."""


class ShellRefusedOpError(ShellOpError):
    """The shell answered with an error status."""

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class WindowPoppedOutError(ShellRefusedOpError):
    """The shell refused an op that would change where a window sits because the target client has it popped out
    into its own window; the op applies with ``force``."""


class ShellAnswerMalformedError(ShellOpError):
    """The shell answered with a success status and a body that is not the answer the contract gives."""
