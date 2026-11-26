"""Applying the document a workspace was handed when it was created.

Whoever creates a workspace leaves one JSON file in the machine's host directory, ``initial_data.json``, as a
cloud machine is left its user data. Each part of it has the shape of the request of the workspace route that
writes the same thing, and is handed to the writer that route calls:

- ``profile``: the profile snapshot (``PUT /api/workspace/profile``);
- ``timezone``: the user's timezone, recorded only where none is (``PUT /api/workspace/timezone``);
- ``update_settings``: the update settings (``PUT /api/workspace/update/settings``);
- ``backups``: where the workspace's backups go, with the storage credentials when the creator had them
  (``PUT /api/workspace/backups/destination``), which the workspace's own backup setup then acts on.

A part that is left out, or null, writes nothing. The trigger is the file being there, not a first boot: a pool
machine's first boot is over when it is baked, long before it has an owner.

Each part is applied at most once. A document one of whose parts was refused is left where it is, and the next
start tries the refused parts again and no other: between two starts the workspace's services run, and a part
written a second time would put the create's value back over what the user chose since (a second write of the
backup destination would turn backups on again after the user turned them off). The step therefore records that
it is about to write a part before it writes it (``status.PartProgress``), under the digest of the document. A
part found half-recorded at a later start was being written when the step ended, and nobody knows whether it
was: it is not written again, and the status says so for good.
"""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from workspace_backup_setup.errors import BackupSetupError
from workspace_profile.errors import (
    InvalidProfileSnapshotError,
    InvalidTimezoneError,
    ProfileWriteError,
)
from workspace_profile.snapshot import write_workspace_profile_snapshot
from workspace_profile.timezone import write_user_timezone
from workspace_update_control.settings import (
    InvalidUpdateSettingsError,
    take_update_settings,
)

from workspace_initial_data.errors import InitialDataPartRefusedError
from workspace_initial_data.status import (
    InitialDataRecord,
    InitialDataState,
    InitialDataStatus,
    PartProgress,
    applied_status,
    failed_status,
    read_initial_data_record,
    write_initial_data_record,
)

INITIAL_DATA_FILE_NAME: Final[str] = "initial_data.json"
# The newest shape of the document this template applies. A document of a higher schema is left for a template
# that knows it.
INITIAL_DATA_SCHEMA: Final[int] = 1
# The document holds storage credentials, so one kept for another attempt is kept readable by its owner alone.
_DOCUMENT_FILE_MODE: Final[int] = 0o600

PROFILE_PART: Final[str] = "profile"
TIMEZONE_PART: Final[str] = "timezone"
UPDATE_SETTINGS_PART: Final[str] = "update_settings"
BACKUPS_PART: Final[str] = "backups"

# Records where a workspace's backups go, from the request ``PUT /api/workspace/backups/destination`` takes: the
# workspace's root, then the request. It raises ``BackupSetupError`` for a request it refuses, which a request made
# while a backup operation runs also is, and ``OSError`` when it cannot write what it was asked.
BackupDestinationRecorder = Callable[[Path, Mapping[str, object]], object]

_INTERRUPTED_DETAIL: Final[str] = (
    "this part was being written when the step was interrupted, so it is not written again"
)
_UNFINISHED_DETAIL: Final[str] = (
    "The step ended before it had applied every part of the document."
)

_PartWriter = Callable[[Path, Mapping[str, object]], None]


def _write_profile(workspace_root: Path, part: Mapping[str, object]) -> None:
    try:
        write_workspace_profile_snapshot(workspace_root, part)
    except (InvalidProfileSnapshotError, ProfileWriteError) as e:
        raise InitialDataPartRefusedError(str(e)) from e


def _write_timezone(workspace_root: Path, part: Mapping[str, object]) -> None:
    zone = part.get("zone")
    is_replacing = part.get("is_replacing", False)
    if not isinstance(zone, str) or not isinstance(is_replacing, bool):
        raise InitialDataPartRefusedError(
            "zone is an IANA timezone name, and is_replacing true or false"
        )
    try:
        write_user_timezone(workspace_root, zone, is_replacing)
    except (InvalidTimezoneError, ProfileWriteError) as e:
        raise InitialDataPartRefusedError(str(e)) from e


def _write_update_settings(workspace_root: Path, part: Mapping[str, object]) -> None:
    # The update library acts on the workspace at the working directory, as its dispatcher does under cron.
    if Path.cwd().resolve() != workspace_root.resolve():
        raise InitialDataPartRefusedError(
            "the update settings are written from the workspace's root, which is not the working directory"
        )
    try:
        take_update_settings(part)
    except (InvalidUpdateSettingsError, OSError) as e:
        raise InitialDataPartRefusedError(str(e)) from e


def _backup_destination_writer(
    record_backup_destination: BackupDestinationRecorder,
) -> _PartWriter:
    def write(workspace_root: Path, part: Mapping[str, object]) -> None:
        try:
            record_backup_destination(workspace_root, part)
        except (BackupSetupError, OSError) as e:
            raise InitialDataPartRefusedError(str(e)) from e

    return write


def _parts_of(document_bytes: bytes) -> dict[str, Mapping[str, object]]:
    """The parts the document carries, by name. Raises ``InitialDataPartRefusedError`` for a document that is not
    one this template applies, with a reason that quotes none of its values."""
    try:
        document = json.loads(document_bytes)
    except json.JSONDecodeError as e:
        raise InitialDataPartRefusedError(
            f"The document is not JSON ({e.msg}, line {e.lineno} column {e.colno})."
        ) from e
    except UnicodeDecodeError as e:
        raise InitialDataPartRefusedError("The document is not JSON text.") from e
    if not isinstance(document, dict):
        raise InitialDataPartRefusedError("The document is not a JSON object.")
    schema = document.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise InitialDataPartRefusedError(
            "The document names no schema: a whole number, 1 or more."
        )
    if schema > INITIAL_DATA_SCHEMA:
        raise InitialDataPartRefusedError(
            f"The document's schema is {schema}, and this workspace's template applies up to "
            f"{INITIAL_DATA_SCHEMA}."
        )
    parts: dict[str, Mapping[str, object]] = {}
    for name in (PROFILE_PART, TIMEZONE_PART, UPDATE_SETTINGS_PART, BACKUPS_PART):
        part = document.get(name)
        if part is None:
            continue
        if not isinstance(part, dict):
            raise InitialDataPartRefusedError(
                f"The document's {name} is not a JSON object."
            )
        parts[name] = part
    return parts


def _keep(
    workspace_root: Path,
    document_digest: str,
    progress_by_part: Mapping[str, PartProgress],
    status: InitialDataStatus,
) -> bool:
    """Keep the status and how far each part of the document has come; whether it was kept."""
    record = InitialDataRecord(
        status=status,
        document_digest=document_digest,
        progress_by_part=dict(progress_by_part),
    )
    try:
        write_initial_data_record(workspace_root, record)
    except OSError as e:
        logger.error("Could not keep the initial data's record: {}", e)
        return False
    return True


def _left_for_the_next_start(
    workspace_root: Path,
    document_path: Path,
    document_digest: str,
    progress_by_part: Mapping[str, PartProgress],
    detail: str,
) -> InitialDataStatus:
    """Record why the document is not applied yet, and leave it where it is."""
    logger.error("The initial data at {} is not applied: {}", document_path, detail)
    try:
        os.chmod(document_path, _DOCUMENT_FILE_MODE)
    except OSError as e:
        logger.warning(
            "Could not restrict the kept document at {}: {}", document_path, e
        )
    status = failed_status(detail)
    _keep(workspace_root, document_digest, progress_by_part, status)
    return status


def _standing(problems: list[str]) -> InitialDataStatus:
    """What the record says while the step is at work, which is what stands if the step ends there."""
    return failed_status("; ".join(problems) or _UNFINISHED_DETAIL)


def apply_initial_data(
    host_dir: Path,
    workspace_root: Path,
    record_backup_destination: BackupDestinationRecorder,
    now: float,
) -> InitialDataStatus:
    """Apply the document in ``host_dir`` when there is one, and answer how the initial data stands.

    With no document nothing is written and the recorded status is answered as it is. A document every part of
    which was written is recorded as applied and deleted, since it holds storage credentials. One that does not
    parse, whose schema this template does not know, or a part of which its writer refuses, is left where it is
    with the reason recorded; the parts that could be written are written all the same.

    A part is written at most once. A later start with the same document tries the parts that were refused and
    leaves every part that was written as the workspace now has it. A part the step was interrupted in the
    middle of is not written again either, and reads as failed from then on. A document left again with the
    bytes of one already applied is deleted and nothing is written.

    ``record_backup_destination`` takes the backup part: the workspace's own backup setup, which records the
    destination and acts on it once the services run. A destination it refuses, and one named while a backup
    operation holds the backup lease, fail that part as any writer's refusal does.
    """
    document_path = host_dir / INITIAL_DATA_FILE_NAME
    recorded = read_initial_data_record(workspace_root)
    try:
        document_bytes = document_path.read_bytes()
    except FileNotFoundError:
        return recorded.status
    except OSError as e:
        return _left_for_the_next_start(
            workspace_root,
            document_path,
            recorded.document_digest,
            recorded.progress_by_part,
            f"The document could not be read: {e}",
        )
    digest = hashlib.sha256(document_bytes).hexdigest()
    is_the_document_recorded = recorded.document_digest == digest
    progress_by_part: dict[str, PartProgress] = (
        dict(recorded.progress_by_part) if is_the_document_recorded else {}
    )
    try:
        parts = _parts_of(document_bytes)
    except InitialDataPartRefusedError as e:
        return _left_for_the_next_start(
            workspace_root, document_path, digest, progress_by_part, str(e)
        )

    writer_by_part: dict[str, _PartWriter] = {
        PROFILE_PART: _write_profile,
        TIMEZONE_PART: _write_timezone,
        UPDATE_SETTINGS_PART: _write_update_settings,
        BACKUPS_PART: _backup_destination_writer(record_backup_destination),
    }
    interrupted = [
        f"{name}: {_INTERRUPTED_DETAIL}"
        for name in parts
        if progress_by_part.get(name) is PartProgress.WRITING
    ]
    refusals: list[str] = []
    for name, part in parts.items():
        if name in progress_by_part:
            continue
        # Kept before the part is written: were the step to end between the write and its record, a later start
        # would otherwise write the part again, over whatever the user chose in between.
        progress_by_part[name] = PartProgress.WRITING
        is_begun = _keep(
            workspace_root,
            digest,
            progress_by_part,
            _standing([*interrupted, *refusals, f"{name}: {_INTERRUPTED_DETAIL}"]),
        )
        if not is_begun:
            del progress_by_part[name]
            refusals.append(
                f"{name}: it could not be recorded that this part is about to be written"
            )
            continue
        try:
            writer_by_part[name](workspace_root, part)
        except InitialDataPartRefusedError as e:
            del progress_by_part[name]
            refusals.append(f"{name}: {e}")
        else:
            progress_by_part[name] = PartProgress.WRITTEN
        _keep(
            workspace_root,
            digest,
            progress_by_part,
            _standing([*interrupted, *refusals]),
        )
    if refusals:
        return _left_for_the_next_start(
            workspace_root,
            document_path,
            digest,
            progress_by_part,
            "; ".join([*interrupted, *refusals]),
        )

    # Nothing is left to try, so the document, which holds storage credentials, goes.
    if interrupted:
        outcome = failed_status("; ".join(interrupted))
    elif is_the_document_recorded and recorded.status.state is InitialDataState.APPLIED:
        outcome = recorded.status
    else:
        outcome = applied_status(now)
    reached = "; ".join(interrupted) if interrupted else "Every part was written"
    if not _keep(workspace_root, digest, progress_by_part, outcome):
        return failed_status(f"{reached}, and that could not be recorded.")
    try:
        document_path.unlink()
    except OSError as e:
        return _left_for_the_next_start(
            workspace_root,
            document_path,
            digest,
            progress_by_part,
            f"{reached}, and the document could not be deleted: {e}",
        )
    if interrupted:
        logger.error("The workspace's initial data is not applied: {}", outcome.detail)
    else:
        logger.info(
            "Applied the workspace's initial data ({})", ", ".join(parts) or "no parts"
        )
    return outcome
