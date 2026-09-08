import os
import tomllib
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from app_manifest.errors import RegistryReadError
from app_manifest.manifest import DEFAULT_PRIORITY
from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.primitives import DisplayName
from app_manifest.primitives import InstancesUrl
from app_manifest.primitives import PriorityName

# The registry's location, exactly as system/scripts/forward_port.py and
# system/scripts/layout.py resolve it: relative to the cwd (the repo root under
# supervisord) unless MINDS_APPS_FILE points elsewhere.
DEFAULT_APPS_FILE: Final[str] = "data/.state/apps.toml"
ENV_APPS_FILE: Final[str] = "MINDS_APPS_FILE"


class RegistryAction(FrozenModel):
    """An action as copied onto a registry row: the id and label only."""

    id: ActionId = Field(description="The declared action id")
    label: NonEmptyStr = Field(description="The action's user-facing label")


class RegistryRow(FrozenModel):
    """One ``[[apps]]`` row of data/.state/apps.toml, with the defaults absent keys read as (contracts.md section 3).

    Unknown keys are ignored rather than rejected: the registry outlives any one release of the
    reader, and a key a newer registration script added must not hide an app from an older shell.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=False)

    name: AppName = Field(description="The registered app name")
    url: AppUrl = Field(description="Where the app is reachable from inside the workspace")
    label: str = Field(default="", description="The unguessable origin label; never an identifier")
    icon: str | None = Field(default=None, description="The registered SVG markup, verbatim")
    internal: bool = Field(default=False, description="Hidden from every open surface")
    program: str | None = Field(default=None, description="The supervisord program that runs the app, when supervised")
    display_name: DisplayName | None = Field(default=None, description="What users see; absent on manifest-less rows")
    instances: bool = Field(default=False, description="Whether the app serves the instances API")
    instances_url: InstancesUrl | None = Field(default=None, description="Where the instances API is served; absent reads as url")
    critical: bool = Field(default=False, description="No Stop verb; snapshot-and-rollback target in the update apply")
    priority: PriorityName = Field(default=DEFAULT_PRIORITY, description="The memory-shedding band name")
    default_shortcut: DefaultShortcut | None = Field(default=None, description="The rail row a new project is seeded with")
    actions: tuple[RegistryAction, ...] = Field(default=(), description="The declared create actions")


def registry_path() -> Path:
    return Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE))


def read_registry(path: Path) -> list[RegistryRow]:
    """Every valid row of the registry at ``path``, in file order; a row that fails validation is logged and skipped.

    Raises RegistryReadError when the file itself cannot be read or parsed. A missing file is an empty registry.
    """
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryReadError(f"cannot read registry {path}: {e}") from e
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryReadError(f"registry {path} is not valid TOML: {e}") from e
    raw_rows = data.get("apps", [])
    if not isinstance(raw_rows, list):
        raise RegistryReadError(f"registry {path} has an 'apps' key that is not an array of tables")
    rows: list[RegistryRow] = []
    for row_idx, raw_row in enumerate(raw_rows):
        try:
            rows.append(RegistryRow.model_validate(raw_row))
        except ValidationError as e:
            logger.warning(
                "Skipped registry row {} ({}) in {}: {}",
                row_idx,
                raw_row.get("name", "<unnamed>") if isinstance(raw_row, dict) else "<not a table>",
                path,
                describe_validation_error(e),
            )
    return rows
