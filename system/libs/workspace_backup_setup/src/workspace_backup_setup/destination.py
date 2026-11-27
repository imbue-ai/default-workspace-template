"""Naming where a workspace's backups go: what a client, or the workspace's initial data, asks of ``backup-setup``.

A request names one of three destinations, with the storage credentials that destination needs where the caller
has some. It never carries the repository's password, which the workspace makes itself. Recording a request
changes no credential in use: it writes what was asked under ``data/.state/backup_setup/`` and the storage offered
beside ``restic.env`` (mode 0600), and the ``backup-setup`` program does the rest.
"""

import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from host_backup.config import parse_restic_env_file
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from pydantic import Field, SecretStr, ValidationError
from workspace_bare_scripts.backup_lease import HOLDER_SETUP, read_lease

from workspace_backup_setup.env_files import (
    PASSWORD_KEY,
    REPOSITORY_KEY,
    REST_PASSWORD_KEY,
    REST_USERNAME_KEY,
    BackupDestinationKind,
    destination_kind_of,
    drop_pending_env,
    kept_env_for_destination,
    read_pending_env,
    read_restic_env_text,
    repository_without_credentials,
    storage_of,
    write_pending_env,
)
from workspace_backup_setup.errors import (
    BackupOperationRunningError,
    InvalidBackupDestinationError,
)
from workspace_backup_setup.state import (
    NO_STORAGE_DETAIL,
    BackupSetupState,
    BackupState,
    SetupRecord,
    read_backup_state,
    setup_lock,
    take_another_writers_file,
    write_setup_record,
)

_REQUEST_ID_BYTES: Final[int] = 8
_VARIABLE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What the workspace sets itself: the repository's address, and its password in each form restic reads one.
_VARIABLES_NOT_THE_USERS: Final[frozenset[str]] = frozenset(
    {
        REPOSITORY_KEY,
        "RESTIC_REPOSITORY_FILE",
        PASSWORD_KEY,
        "RESTIC_PASSWORD_FILE",
        "RESTIC_PASSWORD_COMMAND",
    }
)
_PASSWORD_VARIABLES: Final[frozenset[str]] = frozenset(
    {PASSWORD_KEY, "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND"}
)
_REST_REPOSITORY_PREFIX: Final[str] = "rest:"
_QUOTES: Final[tuple[str, ...]] = ('"', "'")


class ImbueCloudStorage(FrozenModel):
    """The storage Imbue Cloud issued for this workspace: what the connector's ``POST /backups/repositories``
    answers."""

    repository_url: str = Field(
        description="The repository's address at the backup store, a rest: address"
    )
    rest_username: str = Field(
        description="The user name restic signs in to the store with"
    )
    rest_password: SecretStr = Field(description="The store's token for the repository")


class OwnStorage(FrozenModel):
    """Storage of the user's own."""

    repository: str = Field(description="The repository's address, as restic takes one")
    backend_env: dict[str, SecretStr] = Field(
        default_factory=dict,
        description="The variables of the storage as the user typed them (its access keys and the like). The "
        "repository's password is not among them: the workspace makes it",
    )


class BackupDestinationRequest(FrozenModel):
    """Where the workspace's backups are to go, with the storage that destination needs where the caller has some."""

    destination: str = Field(
        description="NONE, IMBUE_CLOUD or OWN_STORAGE. NONE turns backups off"
    )
    imbue_cloud_storage: ImbueCloudStorage | None = Field(
        default=None,
        description="With IMBUE_CLOUD: the storage the connector issued. Left out, the workspace uses what it "
        "kept for the destination, and awaits storage when it kept none",
    )
    own_storage: OwnStorage | None = Field(
        default=None,
        description="With OWN_STORAGE: the repository and the variables of its storage. Left out, the workspace "
        "uses what it kept for the destination, and awaits storage when it kept none",
    )


class BackupDestinationOutcome(FrozenModel):
    """What naming a destination came to."""

    state: BackupState = Field(
        description="The backup state once the request is recorded"
    )
    is_setup_begun: bool = Field(
        description="Whether the request left work for backup-setup; false when there was nothing to do"
    )


class _Destination(FrozenModel):
    """A request once it is checked: the destination, and the storage offered for it as ``restic.env`` names it."""

    kind: BackupDestinationKind
    storage: dict[str, str] | None


def _checked_value(name: str, value: str) -> str:
    """``value`` when ``restic.env`` can carry it as it is: on one line, with nothing its reader would strip."""
    is_quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES
    if (
        not value
        or value != value.strip()
        or is_quoted
        or value.splitlines() != [value]
        or "\0" in value
    ):
        raise InvalidBackupDestinationError(
            f"{name} must be one line that is not empty, does not start or end with a space, and is not quoted."
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # Half of a surrogate pair, which JSON can carry and a file cannot.
        raise InvalidBackupDestinationError(
            f"{name} must be text that a file can hold."
        ) from None
    return value


def _imbue_cloud_storage(storage: ImbueCloudStorage) -> dict[str, str]:
    repository = _checked_value("repository_url", storage.repository_url)
    if not repository.startswith(_REST_REPOSITORY_PREFIX):
        raise InvalidBackupDestinationError(
            "repository_url must be a rest: address, as the connector answers one."
        )
    return {
        REPOSITORY_KEY: repository,
        REST_USERNAME_KEY: _checked_value("rest_username", storage.rest_username),
        REST_PASSWORD_KEY: _checked_value(
            "rest_password", storage.rest_password.get_secret_value()
        ),
    }


def _own_storage(storage: OwnStorage) -> dict[str, str]:
    env_values = {REPOSITORY_KEY: _checked_value("repository", storage.repository)}
    for name, value in storage.backend_env.items():
        if name in _PASSWORD_VARIABLES:
            raise InvalidBackupDestinationError(
                f"{name} must not be among the storage's variables: the workspace makes the repository's "
                "password itself."
            )
        if name in _VARIABLES_NOT_THE_USERS:
            raise InvalidBackupDestinationError(
                f"{name} must not be among the storage's variables: name the repository as `repository`."
            )
        if _VARIABLE_NAME.fullmatch(name) is None:
            # The name is not quoted: a line typed `NAME: value` arrives as a name that holds the value.
            raise InvalidBackupDestinationError(
                "A variable of the storage has a name that is not the name of an environment variable."
            )
        env_values[name] = _checked_value(name, value.get_secret_value())
    return env_values


def _place_of(loc: tuple[int | str, ...]) -> str:
    """Where in a request a problem is, with a variable's name that is not one left out, as it may hold a value."""
    parts = [str(part) for part in loc]
    is_a_variable_named = parts[:2] == ["own_storage", "backend_env"] and len(parts) > 2
    if is_a_variable_named and _VARIABLE_NAME.fullmatch(parts[2]) is None:
        parts[2] = "a variable"
    return ".".join(parts) or "the request"


def describe_request_problems(error: ValidationError) -> str:
    """Where a request is not a ``BackupDestinationRequest`` and why, quoting no value of the storage."""
    return "; ".join(
        f"{_place_of(problem['loc'])}: {problem['msg']}"
        for problem in error.errors(include_input=False, include_url=False)
    )


def _checked_destination(body: Mapping[str, object]) -> _Destination:
    try:
        request = BackupDestinationRequest.model_validate(dict(body))
    except ValidationError as e:
        # The error's own text quotes the request, so it is not kept as this one's cause.
        raise InvalidBackupDestinationError(
            f"The destination is not one this workspace can take ({describe_request_problems(e)})."
        ) from None
    try:
        kind = BackupDestinationKind(request.destination)
    except ValueError as e:
        raise InvalidBackupDestinationError(
            f"{request.destination!r} is not a destination this workspace knows; it is one of "
            f"{', '.join(kind.value for kind in BackupDestinationKind)}."
        ) from e
    is_storage_of_another_kind = (
        request.imbue_cloud_storage is not None
        and kind is not BackupDestinationKind.IMBUE_CLOUD
    ) or (
        request.own_storage is not None
        and kind is not BackupDestinationKind.OWN_STORAGE
    )
    if is_storage_of_another_kind:
        raise InvalidBackupDestinationError(
            f"The storage sent is not {kind.value}'s: IMBUE_CLOUD takes imbue_cloud_storage, OWN_STORAGE takes "
            "own_storage, and NONE takes neither."
        )
    if request.imbue_cloud_storage is not None:
        return _Destination(
            kind=kind, storage=_imbue_cloud_storage(request.imbue_cloud_storage)
        )
    if request.own_storage is not None:
        return _Destination(kind=kind, storage=_own_storage(request.own_storage))
    return _Destination(kind=kind, storage=None)


def _new_request_id() -> str:
    return secrets.token_hex(_REQUEST_ID_BYTES)


def _started_afresh(record: SetupRecord) -> SetupRecord:
    """A failed setup named again: tried now, with its count of failures starting over."""
    return record.model_copy_update(
        to_update(record.field_ref().state, BackupSetupState.INITIALIZING),
        to_update(record.field_ref().detail, ""),
        to_update(record.field_ref().retry_at, None),
        to_update(record.field_ref().failed_attempts, 0),
    )


def _is_held(
    record: SetupRecord,
    destination: _Destination,
    pending: Mapping[str, str] | None,
    in_use: Mapping[str, str] | None,
) -> bool:
    """Whether the workspace already holds what ``destination`` asks for, so that the request changes nothing."""
    if record.destination is not destination.kind:
        return False
    if destination.storage is None:
        return True
    if pending is not None:
        return storage_of(pending) == destination.storage
    return in_use is not None and storage_of(in_use) == destination.storage


def _record_for(
    workspace_root: Path,
    record: SetupRecord,
    destination: _Destination,
    in_use: Mapping[str, str] | None,
    now: float,
    workspace_id: str,
) -> SetupRecord:
    """Record a destination the workspace does not hold yet, and answer the setup's new record.

    The ``restic.env`` in use stays where it is whatever was asked: ``backup-setup`` replaces it once the new
    destination is ready, and moves it aside for ``NONE``.
    """
    drop_pending_env(workspace_root, now)
    request_id = _new_request_id()
    if destination.kind is BackupDestinationKind.NONE:
        return SetupRecord(
            request_id=request_id, installed_digest=record.installed_digest
        )
    storage = destination.storage
    if (
        storage is None
        and in_use is not None
        and destination_kind_of(in_use, workspace_id) is destination.kind
    ):
        # The destination named is the one still in use: a change away from it, or a turn-off, that
        # backup-setup had not carried out.
        return SetupRecord(
            request_id=request_id,
            destination=destination.kind,
            repository=repository_without_credentials(in_use.get(REPOSITORY_KEY, "")),
            state=BackupSetupState.READY,
            installed_digest=record.installed_digest,
        )
    credentials = (
        storage
        if storage is not None
        else kept_env_for_destination(workspace_root, destination.kind, workspace_id)
    )
    if credentials is None:
        return SetupRecord(
            request_id=request_id,
            destination=destination.kind,
            state=BackupSetupState.AWAITING_STORAGE,
            detail=NO_STORAGE_DETAIL,
            installed_digest=record.installed_digest,
        )
    write_pending_env(workspace_root, request_id, credentials)
    return SetupRecord(
        request_id=request_id,
        destination=destination.kind,
        repository=repository_without_credentials(credentials[REPOSITORY_KEY]),
        state=BackupSetupState.INITIALIZING,
        installed_digest=record.installed_digest,
    )


def request_backup_destination(
    workspace_root: Path,
    body: Mapping[str, object],
    *,
    lease_path: Path,
    now: float,
    workspace_id: str,
) -> BackupDestinationOutcome:
    """Record where the workspace's backups are to go, for ``backup-setup`` to set up, and answer the backup state.

    ``body`` is the body of ``PUT /api/workspace/backups/destination``, which is also the shape the workspace's
    initial data names a destination in.

    - A request for what the workspace already holds changes nothing, except that a setup that had failed is
      tried again at once.
    - A destination named with no storage is set up from what the workspace kept for it, and awaits storage when
      it kept none.
    - Storage offered for the repository already in use is taken by ``backup-setup`` only once it finds that the
      storage refuses the credentials in use.

    Raises ``InvalidBackupDestinationError`` for a destination the workspace does not know or storage it cannot
    take (a password among the variables of the user's own storage is one), and ``BackupOperationRunningError``
    for a request that would change something while another backup operation holds the backup lease.

    The workspace's own setup holding the lease refuses nothing: the request replaces what ``backup-setup`` was
    asked, and an attempt under way ends without installing anything. Backups can therefore be turned off, or
    sent elsewhere, while an attempt hangs.
    """
    destination = _checked_destination(body)
    with setup_lock(workspace_root):
        record = take_another_writers_file(workspace_root, now, workspace_id)
        in_use_text = read_restic_env_text(workspace_root)
        in_use = parse_restic_env_file(in_use_text) if in_use_text is not None else None
        pending = read_pending_env(workspace_root, record.request_id)
        is_held = _is_held(record, destination, pending, in_use)
        if is_held and record.state is not BackupSetupState.FAILED:
            is_setup_begun = False
        else:
            lease = read_lease(lease_path, now)
            if lease is not None and lease.holder != HOLDER_SETUP:
                raise BackupOperationRunningError(lease)
            new_record = (
                _started_afresh(record)
                if is_held
                else _record_for(
                    workspace_root, record, destination, in_use, now, workspace_id
                )
            )
            write_setup_record(workspace_root, new_record)
            is_setup_begun = new_record.state is BackupSetupState.INITIALIZING or (
                new_record.destination is BackupDestinationKind.NONE
                and in_use_text is not None
            )
    return BackupDestinationOutcome(
        state=read_backup_state(
            workspace_root, lease_path=lease_path, now=now, workspace_id=workspace_id
        ),
        is_setup_begun=is_setup_begun,
    )
