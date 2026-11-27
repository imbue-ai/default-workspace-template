class BackupSetupError(Exception):
    """Base class for everything this library raises."""


class InvalidBackupCredentialsError(BackupSetupError, ValueError):
    """The credentials a client sent do not name a repository and its password."""


class BackupSetupRunningError(BackupSetupError):
    """A repository is being initialised already, so another initialisation cannot start."""
