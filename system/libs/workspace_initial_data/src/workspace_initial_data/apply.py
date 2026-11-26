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
machine's first boot is over when it is baked, long before it has an owner. Every writer is safe to run twice, so a
document that could not be applied whole is left where it is and applied again, whole, at the next start.
"""

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
    InitialDataStatus,
    applied_status,
    failed_status,
    read_initial_data_status,
    write_initial_data_status,
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
# workspace's root, then the request. It raises ``BackupSetupError`` for a request it refuses.
BackupDestinationRecorder = Callable[[Path, Mapping[str, object]], object]

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
    record_backup_destination: BackupDestinationRecorder | None,
) -> _PartWriter:
    def write(workspace_root: Path, part: Mapping[str, object]) -> None:
        if record_backup_destination is None:
            raise InitialDataPartRefusedError(
                "nothing in this workspace takes a backup destination, so the one it was handed is not set up"
            )
        try:
            record_backup_destination(workspace_root, part)
        except (BackupSetupError, OSError) as e:
            raise InitialDataPartRefusedError(str(e)) from e

    return write


def _parts_of(text: str) -> dict[str, Mapping[str, object]]:
    """The parts the document carries, by name. Raises ``InitialDataPartRefusedError`` for a document that is not
    one this template applies, with a reason that quotes none of its values."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise InitialDataPartRefusedError(
            f"The document is not JSON ({e.msg}, line {e.lineno} column {e.colno})."
        ) from e
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


def _record(workspace_root: Path, status: InitialDataStatus) -> bool:
    """Record ``status``; whether it was recorded."""
    try:
        write_initial_data_status(workspace_root, status)
    except OSError as e:
        logger.error("Could not record the initial data's status: {}", e)
        return False
    return True


def _failed(
    workspace_root: Path, document_path: Path, detail: str
) -> InitialDataStatus:
    """Record that the document could not be applied, and leave it for the next start."""
    logger.error("The initial data at {} was not applied: {}", document_path, detail)
    try:
        os.chmod(document_path, _DOCUMENT_FILE_MODE)
    except OSError as e:
        logger.warning(
            "Could not restrict the kept document at {}: {}", document_path, e
        )
    status = failed_status(detail)
    _record(workspace_root, status)
    return status


def apply_initial_data(
    host_dir: Path,
    workspace_root: Path,
    record_backup_destination: BackupDestinationRecorder | None,
    now: float,
) -> InitialDataStatus:
    """Apply the document in ``host_dir`` when there is one, and answer how the initial data stands.

    With no document nothing is written and the recorded status is answered as it is. A document every part of
    which was written is recorded as applied and deleted, since it holds storage credentials. One that does not
    parse, whose schema this template does not know, or a part of which its writer refuses, is left where it is
    with the reason recorded, so the next start applies it again whole; the parts that could be written are
    written all the same.

    ``record_backup_destination`` takes the backup part. With none, a document that carries a backup part fails.
    """
    document_path = host_dir / INITIAL_DATA_FILE_NAME
    try:
        text = document_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return read_initial_data_status(workspace_root)
    except (OSError, UnicodeDecodeError) as e:
        return _failed(
            workspace_root, document_path, f"The document could not be read: {e}"
        )
    try:
        parts = _parts_of(text)
    except InitialDataPartRefusedError as e:
        return _failed(workspace_root, document_path, str(e))

    writer_by_part: dict[str, _PartWriter] = {
        PROFILE_PART: _write_profile,
        TIMEZONE_PART: _write_timezone,
        UPDATE_SETTINGS_PART: _write_update_settings,
        BACKUPS_PART: _backup_destination_writer(record_backup_destination),
    }
    refusals: list[str] = []
    for name, part in parts.items():
        try:
            writer_by_part[name](workspace_root, part)
        except InitialDataPartRefusedError as e:
            refusals.append(f"{name}: {e}")
    if refusals:
        return _failed(workspace_root, document_path, "; ".join(refusals))

    applied = applied_status(now)
    if not _record(workspace_root, applied):
        return failed_status("Every part was written, and that could not be recorded.")
    try:
        document_path.unlink()
    except OSError as e:
        return _failed(
            workspace_root,
            document_path,
            f"Every part was written, and the document could not be deleted: {e}",
        )
    logger.info(
        "Applied the workspace's initial data ({})", ", ".join(parts) or "no parts"
    )
    return applied
