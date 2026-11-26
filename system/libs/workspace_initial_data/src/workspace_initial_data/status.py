"""How a workspace's initial data stands, as the shell's status route answers it.

The boot step records the outcome of each attempt in ``data/.state/initial_data/state.json``. A workspace that has
found no document and applied none has no such file, and reads ``NONE``.
"""

import json
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field, ValidationError
from workspace_profile.overrides import write_flat_value_map

STATUS_RELATIVE_PATH: Final[Path] = Path("data/.state/initial_data/state.json")
_STATUS_SCHEMA: Final[int] = 1


class InitialDataState(UpperCaseStrEnum):
    NONE = auto()
    APPLIED = auto()
    FAILED = auto()


class InitialDataStatus(FrozenModel):
    """How the workspace's initial data stands."""

    state: InitialDataState = Field(
        description="NONE when the workspace found no document and has applied none, APPLIED when every part of "
        "one was written, FAILED when the last attempt could not apply one, which the next start of the "
        "workspace's services tries again"
    )
    detail: str = Field(description="Why, for FAILED; '' otherwise")
    applied_at: float | None = Field(
        description="When it was applied, seconds since the epoch; null unless APPLIED"
    )


NO_INITIAL_DATA: Final[InitialDataStatus] = InitialDataStatus(
    state=InitialDataState.NONE, detail="", applied_at=None
)


def applied_status(applied_at: float) -> InitialDataStatus:
    return InitialDataStatus(
        state=InitialDataState.APPLIED, detail="", applied_at=applied_at
    )


def failed_status(detail: str) -> InitialDataStatus:
    return InitialDataStatus(
        state=InitialDataState.FAILED, detail=detail, applied_at=None
    )


def read_initial_data_status(workspace_root: Path) -> InitialDataStatus:
    """The recorded status; ``NONE`` when nothing is recorded, or the record cannot be read."""
    path = workspace_root / STATUS_RELATIVE_PATH
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_INITIAL_DATA
    except (OSError, ValueError) as e:
        logger.warning("Could not read the initial data's status at {}: {}", path, e)
        return NO_INITIAL_DATA
    try:
        return InitialDataStatus.model_validate(
            {
                name: value
                for name, value in stored.items()
                if name in InitialDataStatus.model_fields
            }
            if isinstance(stored, dict)
            else stored
        )
    except ValidationError as e:
        logger.warning(
            "The initial data's status at {} is not a status: {}",
            path,
            e.errors()[0]["msg"],
        )
        return NO_INITIAL_DATA


def write_initial_data_status(workspace_root: Path, status: InitialDataStatus) -> None:
    """Record ``status``, atomically. Raises ``OSError`` when it cannot be written."""
    write_flat_value_map(
        workspace_root / STATUS_RELATIVE_PATH,
        {"schema": _STATUS_SCHEMA, **status.model_dump(mode="json")},
    )
