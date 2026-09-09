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


class StaleLayoutSaveError(ShellError, ValueError):
    """A browser's save is based on an older arrangement than the one stored (answered 409)."""


class ClientNotFoundError(ShellError, LookupError):
    """No client record has the given id."""


class NoTargetClientError(ShellError, ValueError):
    """An op could not be settled on exactly one client (answered 412)."""


class InstanceNotListedError(ShellError, LookupError):
    """No app lists an instance at the given address."""


class PanelNotFoundError(ShellError, LookupError):
    """The client's arrangement of the view holds no panel for the address."""


class LayoutOpError(ShellError, ValueError):
    """An op's arguments cannot be applied to the arrangement."""


class InstanceCreateRefusedError(ShellError):
    """An app (or the relay in front of it) refused the create an op asked for; carries the app's status and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class UnknownAppError(ShellError, LookupError):
    """No registered app has the given name."""


class AppLifecycleRefusedError(ShellError, ValueError):
    """The app cannot be stopped or started through the workspace."""


class SupervisorProgramActionError(ShellError, RuntimeError):
    """Supervisord refused, or could not be reached for, a stop or start."""


class UpdateNoticeRecordError(ShellError, ValueError):
    """The kept rollback point's file does not hold a record."""


class UpdateNoticeRefusedError(ShellError, ValueError):
    """The update notice is not in a state the verb applies to: nothing kept, a rollback already running, or one done."""


class UpdateNoticeCommandError(ShellError, RuntimeError):
    """The update-self script behind a notice verb could not be run, or failed."""
