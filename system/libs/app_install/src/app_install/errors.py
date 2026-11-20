class AppInstallError(Exception):
    """Base of every error the installer raises."""


class RecordLoadError(AppInstallError, ValueError):
    """An app record that cannot be read: it is not TOML, or not the record's shape."""


class AppNotFoundError(AppInstallError, KeyError):
    """No app directory, or no app record, answers to the given name."""


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
