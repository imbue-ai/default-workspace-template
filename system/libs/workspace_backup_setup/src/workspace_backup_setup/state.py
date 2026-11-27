"""What a workspace knows of its own backups: the destination it was asked for, how the setup stands, and what
a client reads of both.

The record of the setup is one file under ``data/.state/backup_setup/``, written under a lock by the destination
request (``destination``) and by the program that does the setup (``program``). It holds no credential.

A ``restic.env`` the program did not write is taken as the destination: a released Imbue Studio desktop writes the
file by its own command, and again from its own copy when it updates the backup service, and a current desktop's
restore and its migration of the backup store write it too. (A current desktop's update of the backup service
writes none into a workspace that has the backup lease.) The record names the digest of the file as the program
last left it, so a file with another digest is someone else's, and a file gone that the record expects is backups
turned off by such a writer.
"""

import fcntl
import json
import math
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Final

from host_backup.config import parse_restic_env_file
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from pydantic import Field, ValidationError
from workspace_bare_scripts.backup_lease import default_lease_path, read_lease

from workspace_backup_setup.env_files import (
    REPOSITORY_KEY,
    BackupDestinationKind,
    destination_kind_of,
    drop_pending_env,
    env_digest,
    read_restic_env_text,
    repository_without_credentials,
    write_atomically,
)
from workspace_backup_setup.verification import read_verification_disabled

SETUP_STATE_RELATIVE_PATH: Final[Path] = Path("data/.state/backup_setup/state.json")
_SETUP_LOCK_RELATIVE_PATH: Final[Path] = SETUP_STATE_RELATIVE_PATH.with_name(
    "state.lock"
)
_STATE_FILE_MODE: Final[int] = 0o644
# The environment variable that names the workspace: the id of the agent its services run under.
WORKSPACE_ID_ENV: Final[str] = "MNGR_AGENT_ID"

FIRST_RETRY_SECONDS: Final[float] = 60.0
LONGEST_RETRY_SECONDS: Final[float] = 3600.0
# The doubling that first reaches the longest delay. A count of failures has no bound, and two to its power
# outgrows a float.
_DOUBLINGS_TO_LONGEST_RETRY: Final[int] = math.ceil(
    math.log2(LONGEST_RETRY_SECONDS / FIRST_RETRY_SECONDS)
)

NO_STORAGE_DETAIL: Final[str] = (
    "This workspace holds no storage credentials for the destination."
)


class BackupSetupState(UpperCaseStrEnum):
    """How the setup of the workspace's backups stands."""

    NONE = auto()
    AWAITING_STORAGE = auto()
    INITIALIZING = auto()
    READY = auto()
    FAILED = auto()


class SetupRecord(FrozenModel):
    """The setup as the workspace keeps it on disk."""

    request_id: str = Field(
        default="", description="Names the request the setup is for; '' before any"
    )
    destination: BackupDestinationKind = Field(
        default=BackupDestinationKind.NONE, description="Where the backups are to go"
    )
    repository: str = Field(
        default="",
        description="The destination's repository, with no credential in it; '' when it is not known",
    )
    state: BackupSetupState = Field(
        default=BackupSetupState.NONE, description="How the setup stands"
    )
    detail: str = Field(default="", description="What failed; '' otherwise")
    started_at: float | None = Field(
        default=None, description="When the last attempt began"
    )
    finished_at: float | None = Field(
        default=None, description="When the last attempt ended"
    )
    retry_at: float | None = Field(
        default=None, description="When the next attempt starts, after one that failed"
    )
    failed_attempts: int = Field(
        default=0, description="How many attempts in a row have failed"
    )
    installed_digest: str = Field(
        default="",
        description="The digest of restic.env as the program last installed or took it; '' when it expects none",
    )


class BackupSetupStatus(FrozenModel):
    """How the setup stands, as a client reads it."""

    state: BackupSetupState = Field(
        description="NONE when no destination is chosen; AWAITING_STORAGE when one is and the workspace holds no "
        "storage credentials that work for it; INITIALIZING while an attempt runs; READY once restic.env is in "
        "place for the destination; FAILED when the last attempt failed"
    )
    detail: str = Field(
        default="",
        description="What failed, or why storage is awaited, with no credential in it; '' otherwise",
    )
    started_at: float | None = Field(
        default=None,
        description="When the last attempt began, seconds since the epoch; null when there was none",
    )
    finished_at: float | None = Field(
        default=None,
        description="When it ended, seconds since the epoch; null while it runs or when there was none",
    )
    retry_at: float | None = Field(
        default=None,
        description="When the workspace tries a failed setup again, seconds since the epoch; null otherwise",
    )


class BackupLeaseHolder(FrozenModel):
    """Who holds the backup lease, which keeps two backup operations apart."""

    holder: str = Field(
        description="'setup' for the workspace's own setup, or the name of a program of the Imbue Studio desktop "
        "('restore', 'backup-service-update', 'store-migration')"
    )
    taken_at: float = Field(description="When it was taken, seconds since the epoch")
    expires_at: float = Field(
        description="When it runs out unless renewed, seconds since the epoch"
    )


class BackupState(FrozenModel):
    """Everything a client reads about a workspace's backups."""

    destination: BackupDestinationKind = Field(
        description="Where the backups go: NONE, IMBUE_CLOUD or OWN_STORAGE"
    )
    setup: BackupSetupStatus = Field(
        description="How the setup of that destination stands"
    )
    is_configured: bool = Field(
        description="Whether restic.env is in place, which is what the backup service runs on"
    )
    is_verification_enabled: bool = Field(
        description="Whether a client checks this workspace's backups"
    )
    repository: str = Field(
        description="The address of the destination's repository, with any credential in it taken out; '' when "
        "there is none or the workspace does not know it yet"
    )
    credentials_digest: str = Field(
        description="The SHA-256, in hex, of the text of the restic.env in use; '' when there is none"
    )
    lease: BackupLeaseHolder | None = Field(
        description="Who holds the backup lease; null when nobody does"
    )


class BackupCredentials(FrozenModel):
    """The credentials the workspace backs up with."""

    env_text: str = Field(
        description="The whole text of the restic.env in use; '' when there is none"
    )
    digest: str = Field(
        description="The SHA-256, in hex, of that text, which the backup state answers as credentials_digest; '' "
        "when there is none"
    )


def backup_lease_path() -> Path:
    """The file this workspace keeps its backup lease in."""
    return default_lease_path()


def workspace_id_from_environment(environ: Mapping[str, str]) -> str:
    """This workspace's id as its services' environment names it; '' where it does not."""
    return environ.get(WORKSPACE_ID_ENV, "")


@contextmanager
def setup_lock(workspace_root: Path) -> Generator[None, None, None]:
    """Serialize a read of the setup's record with the write that follows it."""
    lock_path = workspace_root / _SETUP_LOCK_RELATIVE_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_setup_record(workspace_root: Path) -> SetupRecord:
    """The record on disk; the record of a workspace nothing was asked of when there is none to read."""
    try:
        raw: object = json.loads(
            (workspace_root / SETUP_STATE_RELATIVE_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return SetupRecord()
    try:
        return SetupRecord.model_validate(raw)
    except ValidationError:
        return SetupRecord()


def write_setup_record(workspace_root: Path, record: SetupRecord) -> None:
    write_atomically(
        workspace_root / SETUP_STATE_RELATIVE_PATH,
        json.dumps(record.model_dump(mode="json"), indent=2) + "\n",
        _STATE_FILE_MODE,
    )


def record_with_another_writers_file(
    record: SetupRecord, env_text: str | None, workspace_id: str
) -> SetupRecord:
    """``record`` as it reads beside the ``restic.env`` in place.

    A file the program did not write is the destination, and the setup is ready for the repository it names. A
    file the record expects that is gone is backups turned off.
    """
    digest = env_digest(env_text) if env_text is not None else ""
    if digest == record.installed_digest:
        return record
    if env_text is None:
        return SetupRecord(request_id=record.request_id)
    env_values = parse_restic_env_file(env_text)
    return SetupRecord(
        request_id=f"taken-{digest[:16]}",
        destination=destination_kind_of(env_values, workspace_id),
        repository=repository_without_credentials(env_values.get(REPOSITORY_KEY, "")),
        state=BackupSetupState.READY,
        installed_digest=digest,
    )


def take_another_writers_file(
    workspace_root: Path, now: float, workspace_id: str
) -> SetupRecord:
    """The record brought in line with the ``restic.env`` in place, and written when that changed it.

    A setup the program had not finished is dropped with it. The caller holds ``setup_lock``.
    """
    record = read_setup_record(workspace_root)
    current = record_with_another_writers_file(
        record, read_restic_env_text(workspace_root), workspace_id
    )
    if current != record:
        drop_pending_env(workspace_root, now)
        write_setup_record(workspace_root, current)
    return current


def _status_of(record: SetupRecord) -> BackupSetupStatus:
    return BackupSetupStatus(
        state=record.state,
        detail=record.detail,
        started_at=record.started_at,
        finished_at=record.finished_at,
        retry_at=record.retry_at,
    )


def read_backup_state(
    workspace_root: Path, *, lease_path: Path, now: float, workspace_id: str
) -> BackupState:
    """Everything a client reads about the workspace's backups, as the files stand at ``now``."""
    env_text = read_restic_env_text(workspace_root)
    record = record_with_another_writers_file(
        read_setup_record(workspace_root), env_text, workspace_id
    )
    lease = read_lease(lease_path, now)
    return BackupState(
        destination=record.destination,
        setup=_status_of(record),
        is_configured=env_text is not None,
        is_verification_enabled=not read_verification_disabled(workspace_root),
        repository=record.repository,
        credentials_digest=env_digest(env_text) if env_text is not None else "",
        lease=BackupLeaseHolder(
            holder=lease.holder, taken_at=lease.taken_at, expires_at=lease.expires_at
        )
        if lease is not None
        else None,
    )


def read_backup_credentials(workspace_root: Path) -> BackupCredentials:
    """The text of the ``restic.env`` in use and its digest; both '' when there is none."""
    env_text = read_restic_env_text(workspace_root)
    if env_text is None:
        return BackupCredentials(env_text="", digest="")
    return BackupCredentials(env_text=env_text, digest=env_digest(env_text))


def failed_record(record: SetupRecord, detail: str, now: float) -> SetupRecord:
    """``record`` after an attempt that failed at ``now``: tried again after a minute, then twice as long each
    time, up to an hour."""
    failed_attempts = record.failed_attempts + 1
    doublings = min(failed_attempts - 1, _DOUBLINGS_TO_LONGEST_RETRY)
    delay = min(FIRST_RETRY_SECONDS * 2**doublings, LONGEST_RETRY_SECONDS)
    return record.model_copy_update(
        to_update(record.field_ref().state, BackupSetupState.FAILED),
        to_update(record.field_ref().detail, detail),
        to_update(record.field_ref().finished_at, now),
        to_update(record.field_ref().retry_at, now + delay),
        to_update(record.field_ref().failed_attempts, failed_attempts),
    )
