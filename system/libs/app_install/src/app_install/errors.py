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


class MissingCommandError(GitCommandError):
    """A command the installer needs is not installed; the message names it."""


class NotPrimaryCheckoutError(AppInstallError):
    """The command was run in a linked worktree, and it moves the workspace's primary branch."""


class NotModifiedAppError(AppInstallError, ValueError):
    """The app was installed and nobody has changed it, so it has no repository of this workspace's own."""


class DevRepositoryError(AppInstallError):
    """An app's dev repository could not be created."""


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


class AppChecksFailedError(AppInstallError):
    """An app's own checks do not pass: its environment does not sync from its lock, or its tests fail."""


class InvalidTemplateVersionError(AppInstallError, ValueError):
    """A value given as a template version is not written as one."""


class RecordReferenceError(AppInstallError, ValueError):
    """A reference cannot be added to an app's record, or is not there to remove."""


class PublishError(AppInstallError):
    """A step of a publish could not be done; nothing after it ran."""


class SecretsFoundError(PublishError):
    """The secret scan found something in the tree about to be published; ``findings`` names each, with its
    file and never its value."""

    def __init__(self, findings: tuple[str, ...]) -> None:
        self.findings = findings
        super().__init__("; ".join(findings))


class OutsideSkillsError(PublishError):
    """The tree about to be published names, by path, skills of this workspace that the app does not carry;
    ``uses`` names each line."""

    def __init__(self, uses: tuple[str, ...]) -> None:
        self.uses = uses
        super().__init__("; ".join(uses))


class LockCheckError(PublishError):
    """The app's lock does not match its project when the app stands alone, outside any workspace."""


class NotAssembledError(PublishError):
    """No assembled and scanned tree of the app is waiting to be pushed or recorded."""


class NotPushedError(PublishError):
    """The assembled tree has not been pushed, so there is no published commit to record."""


class PublishedNameTakenError(PublishError):
    """The account already has a repository of the name the app was to be published under."""


class PushRefusedError(PublishError):
    """The published repository holds commits this workspace's publish does not continue."""
