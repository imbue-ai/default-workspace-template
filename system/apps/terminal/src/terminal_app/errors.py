class TerminalAppError(Exception):
    """Base error for the terminal app; the wrapper pages answer every subclass with a detail body."""


class InvalidTerminalValueError(TerminalAppError, ValueError):
    """A tmux session name, title, or working directory does not satisfy its rule."""


class UnknownTerminalError(TerminalAppError, LookupError):
    """No terminal, live or remembered, has the given name."""


class TerminalConflictError(TerminalAppError):
    """The app refuses the verb right now: an agent's session, a title another terminal holds, or a name tmux will not create."""


class TerminalStoreError(TerminalAppError, OSError):
    """The terminal's JSON store cannot be read or written."""


class TmuxCommandError(TerminalAppError):
    """A tmux command could not run, or ran and left the server in a state other than the one asked for."""


class UnsafeDispatchPathError(TerminalAppError):
    """A path that would be baked into a dispatch script needs shell quoting, which the scripts do not do."""


class TtydStartError(TerminalAppError):
    """ttyd could not be started in place of the pty program."""


class TerminalServeError(TerminalAppError):
    """The app cannot serve: an app URL without a port, a port it cannot bind, or a wait run off the main thread."""


class UnknownSessionPageError(TerminalAppError):
    """The wrapper was asked for the page of a session name that cannot be one."""
