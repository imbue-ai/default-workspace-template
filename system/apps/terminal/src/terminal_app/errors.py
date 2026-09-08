from app_instances.errors import AppInstancesError


class TerminalAppError(AppInstancesError):
    """Base error for the terminal app; a library error, so the instances blueprint answers it with a detail body."""


class InvalidTerminalValueError(TerminalAppError, ValueError):
    """A tmux session name, tab id, client tty, or working directory does not satisfy its rule."""


class TmuxCommandError(TerminalAppError):
    """A tmux command could not run, or ran and left the server in a state other than the one asked for."""


class UnsafeDispatchPathError(TerminalAppError):
    """A path that would be baked into a dispatch script needs shell quoting, which the scripts do not do."""
