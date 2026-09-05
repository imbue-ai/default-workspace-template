class ShellError(Exception):
    """Base error for everything in the shell subpackage."""


class InvalidAddressError(ShellError, ValueError):
    """A string is not an address of contracts.md section 1."""


class InvalidShellValueError(ShellError, ValueError):
    """A shell identifier or value does not satisfy its rule."""


class ShellStateError(ShellError, OSError):
    """A shell state file cannot be read or written."""


class ProjectNotFoundError(ShellError, LookupError):
    """No project has the given id."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"Project '{project_id}' not found")


class ProjectConflictError(ShellError, ValueError):
    """A new project's id collides with an existing project."""


class ProjectValueError(ShellError, ValueError):
    """A project's name, color, glyph, or shortcut is not usable."""


class EverythingIsNotAProjectError(ShellError, ValueError):
    """A project route was called with the Everything view id."""


class LayoutNotFoundError(ShellError, LookupError):
    """No client layout holds the given tab."""


class UnknownAppError(ShellError, LookupError):
    """No registered app has the given name."""


class AppLifecycleRefusedError(ShellError, ValueError):
    """The app cannot be stopped or started through the workspace."""
