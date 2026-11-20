class AppInstallError(Exception):
    """Base of every error the installer raises."""


class AppNotFoundError(AppInstallError, KeyError):
    """No app directory, or no app record, answers to the given name."""

    def __str__(self) -> str:
        # A KeyError's own text is the repr of its argument, which would put the message in quotes.
        return str(self.args[0]) if self.args else ""


class AppAlreadyRegisteredError(AppInstallError, ValueError):
    """The app already has a record."""


class CollisionError(AppInstallError, ValueError):
    """The app would take a name, a program, a script, a skill, a routine, an environment unit, or a port that
    something already in the workspace holds; ``problems`` names each one."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class PortUnavailableError(AppInstallError, ValueError):
    """The requested port is held by another app or service."""


class GitCommandError(AppInstallError):
    """A git command the installer ran failed."""


class RepositoryUnreachableError(AppInstallError):
    """An app's repository could not be read, anonymously or with the workspace's GitHub access."""

    def __init__(self, message: str, is_access_needed: bool) -> None:
        self.is_access_needed = is_access_needed
        super().__init__(message)


class NotAnAppError(AppInstallError, ValueError):
    """The repository holds no app: it has no ``app.toml`` at its root, or one that cannot be read."""


class NotInstalledAppError(AppInstallError, ValueError):
    """The app was built in this workspace: it has no repository it came from."""


class UpdateInProgressError(AppInstallError):
    """An update of the app is already waiting for a person, or none is when one was to be continued."""


class NoSnapshotError(AppInstallError):
    """The app's stored data could not be kept safe before an update: it does not fit beside itself and the
    workspace has no recent backup, or copying it aside failed."""
