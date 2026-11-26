from workspace_bare_scripts.backup_lease import BackupLease


class BackupSetupError(Exception):
    """Base class for everything this library raises."""


class InvalidBackupDestinationError(BackupSetupError, ValueError):
    """A request names a destination this workspace does not know, or storage it cannot take."""


class BackupOperationRunningError(BackupSetupError):
    """Someone holds the backup lease, so the destination cannot change now."""

    def __init__(self, lease: BackupLease) -> None:
        super().__init__(
            f"A backup operation is running ({lease.holder}); the destination cannot change until it ends."
        )
        self.lease = lease
