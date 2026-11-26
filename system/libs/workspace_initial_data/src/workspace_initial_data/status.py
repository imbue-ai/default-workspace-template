"""How a workspace's initial data stands, as the shell's status route answers it, and how far the boot step has
come with the document it is applying.

The boot step keeps one record, ``data/.state/initial_data/state.json``: the status a client reads, the digest of
the document it is of, and how far each part of that document has come. A workspace that has found no document
and applied none has no such file, and reads ``NONE``.

A part is applied at most once. The step records that it is about to write a part before it writes it, and that it
wrote it afterwards, so a later start of the workspace's services knows which parts are done whatever happened in
between, and writes none of them again over what the user has chosen since.
"""

import json
import os
import tempfile
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field, ValidationError

STATUS_RELATIVE_PATH: Final[Path] = Path("data/.state/initial_data/state.json")
_STATUS_SCHEMA: Final[int] = 1
_RECORD_FILE_MODE: Final[int] = 0o644


class InitialDataState(UpperCaseStrEnum):
    NONE = auto()
    APPLIED = auto()
    FAILED = auto()


class InitialDataStatus(FrozenModel):
    """How the workspace's initial data stands."""

    state: InitialDataState = Field(
        description="NONE when the workspace found no document and has applied none, APPLIED when every part of "
        "one was written, FAILED when some part of one is not written: the next start of the workspace's services "
        "tries the parts that were refused again, and writes no part a second time"
    )
    detail: str = Field(
        description="Why, for FAILED, naming each part that is not written; '' otherwise"
    )
    applied_at: float | None = Field(
        description="When it was applied, seconds since the epoch; null unless APPLIED"
    )


class PartProgress(UpperCaseStrEnum):
    """How far the step has come with one part of the document."""

    # Recorded before the part's writer is called. A part found so at a later start was being written when the
    # step ended, and nobody knows whether it was.
    WRITING = auto()
    WRITTEN = auto()


class InitialDataRecord(FrozenModel):
    """What the step keeps on disk: the status, and how far it has come with which document."""

    status: InitialDataStatus = Field(description="How the initial data stands")
    document_digest: str = Field(
        default="",
        description="The SHA-256, in hex, of the document the parts below are of; '' when there was none",
    )
    progress_by_part: dict[str, PartProgress] = Field(
        default_factory=dict,
        description="How far each part of that document has come; a part that is not named was not begun, or was "
        "refused, and is still to be written",
    )


NO_INITIAL_DATA: Final[InitialDataStatus] = InitialDataStatus(
    state=InitialDataState.NONE, detail="", applied_at=None
)
NO_RECORD: Final[InitialDataRecord] = InitialDataRecord(status=NO_INITIAL_DATA)


def applied_status(applied_at: float) -> InitialDataStatus:
    return InitialDataStatus(
        state=InitialDataState.APPLIED, detail="", applied_at=applied_at
    )


def failed_status(detail: str) -> InitialDataStatus:
    return InitialDataStatus(
        state=InitialDataState.FAILED, detail=detail, applied_at=None
    )


class _StoredRecord(FrozenModel):
    """The record as the file holds it: the status's own members beside what the step keeps for itself."""

    state: InitialDataState
    detail: str
    applied_at: float | None
    document_digest: str = ""
    parts: dict[str, PartProgress] = Field(default_factory=dict)


def read_initial_data_record(workspace_root: Path) -> InitialDataRecord:
    """The record on disk; the record of a workspace that was handed nothing when there is none, or it cannot be
    read."""
    path = workspace_root / STATUS_RELATIVE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_RECORD
    except (OSError, ValueError) as e:
        logger.warning("Could not read the initial data's record at {}: {}", path, e)
        return NO_RECORD
    try:
        stored = _StoredRecord.model_validate(
            {
                name: value
                for name, value in raw.items()
                if name in _StoredRecord.model_fields
            }
            if isinstance(raw, dict)
            else raw
        )
    except ValidationError as e:
        logger.warning(
            "The initial data's record at {} is not a record: {}",
            path,
            e.errors()[0]["msg"],
        )
        return NO_RECORD
    return InitialDataRecord(
        status=InitialDataStatus(
            state=stored.state, detail=stored.detail, applied_at=stored.applied_at
        ),
        document_digest=stored.document_digest,
        progress_by_part=stored.parts,
    )


def read_initial_data_status(workspace_root: Path) -> InitialDataStatus:
    """The recorded status; ``NONE`` when nothing is recorded, or the record cannot be read."""
    return read_initial_data_record(workspace_root).status


def write_initial_data_record(workspace_root: Path, record: InitialDataRecord) -> None:
    """Keep ``record``, replacing the file in one step and not returning before it is on disk.

    The step writes a part only once the record says it is about to, so a record that a crash could lose would
    let a later start write that part again. Raises ``OSError`` when it cannot be written.
    """
    path = workspace_root / STATUS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": _STATUS_SCHEMA,
        **record.status.model_dump(mode="json"),
        "document_digest": record.document_digest,
        "parts": {
            name: progress.value for name, progress in record.progress_by_part.items()
        },
    }
    handle, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(document, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(scratch, _RECORD_FILE_MODE)
        os.replace(scratch, path)
    except OSError:
        Path(scratch).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
