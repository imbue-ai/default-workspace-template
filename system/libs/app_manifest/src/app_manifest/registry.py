import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from app_manifest.errors import AppRegistrationError
from app_manifest.errors import RegistryReadError
from app_manifest.manifest import DEFAULT_PRIORITY
from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import describe_validation_error
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.primitives import DisplayName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.primitives import PriorityName

# The registry's location, exactly as system/scripts/forward_port.py and
# system/scripts/layout.py resolve it: relative to the cwd (the repo root under
# supervisord) unless MINDS_APPS_FILE points elsewhere.
DEFAULT_APPS_FILE: Final[str] = "data/.state/apps.toml"
ENV_APPS_FILE: Final[str] = "MINDS_APPS_FILE"

# The workspace shell's registered name: the row whose origin label a plain app page reads
# (``read_origin_label``) to import the app contract module from the shell's origin.
SHELL_APP_NAME: Final[AppName] = AppName("system_interface")

# The registration script, relative to the repo root every supervised program runs from.
FORWARD_PORT_SCRIPT: Final[Path] = Path("system/scripts/forward_port.py")

# Registration is one local file write; past the first threshold it is suspicious, past the
# second it is broken.
REGISTRATION_SLOW_SECONDS: Final[float] = 2.0
REGISTRATION_TIMEOUT_SECONDS: Final[float] = 15.0


class RegistryLaunchPath(FrozenModel):
    """A launch path as copied onto a registry row: the id, the label, the path, and the names of its params."""

    id: LaunchPathId = Field(description="The declared launch path id")
    label: NonEmptyStr = Field(description="The launch path's user-facing label")
    path: LaunchPathValue = Field(description="The path under the app origin")
    params: tuple[NonEmptyStr, ...] = Field(
        default=(), description="The names of the query parameters the shell may append, in manifest order"
    )


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
    critical: bool = Field(default=False, description="No Stop verb; snapshot-and-rollback target in the update apply")
    priority: PriorityName = Field(default=DEFAULT_PRIORITY, description="The memory-shedding band name")
    default_shortcut: DefaultShortcut | None = Field(default=None, description="The shortcut a new desktop is seeded with")
    launch_paths: tuple[RegistryLaunchPath, ...] = Field(
        default=(), description="The paths the desktop interface opens windows at"
    )
    launcher_rank: int | None = Field(
        default=None, description="The app's place among the launcher's leading tiles; absent reads as none"
    )


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


def read_origin_label(path: Path, name: AppName) -> str:
    """The origin label of the app registered as ``name``, or "" when none is, or the registry cannot be read.

    Read per page load rather than watched: a label is minted once per workspace and the
    registry is one small file, and an unreadable registry costs the page the origin it wanted
    to derive, never the page.
    """
    try:
        rows = read_registry(path)
    except RegistryReadError as e:
        logger.warning("Could not read the app registry for the origin label of {}: {}", name, e)
        return ""
    for row in rows:
        if row.name == name:
            return row.label
    return ""


def register_app(manifest_path: Path, app_url: AppUrl) -> None:
    """Upsert the app's registry row through ``forward_port.py --manifest``; raises AppRegistrationError when that fails.

    Run under this interpreter from the repo root, the way every app's entry point registers itself
    at startup (the supervisord program lines of apps with no entry point run the script directly).
    """
    if not FORWARD_PORT_SCRIPT.is_file():
        raise AppRegistrationError(
            f"registration script {FORWARD_PORT_SCRIPT} not found; the app must run from the repo root"
        )
    command = [sys.executable, str(FORWARD_PORT_SCRIPT), "--manifest", str(manifest_path), "--url", app_url]
    started_at = time.monotonic()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=REGISTRATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise AppRegistrationError(
            f"registration of {manifest_path} did not finish within {REGISTRATION_TIMEOUT_SECONDS}s"
        ) from e
    elapsed = time.monotonic() - started_at
    if completed.returncode != 0:
        raise AppRegistrationError(
            f"registration of {manifest_path} failed with exit code {completed.returncode}: {completed.stderr.strip()}"
        )
    if elapsed > REGISTRATION_SLOW_SECONDS:
        logger.warning("Registered {} slowly, in {:.1f}s", manifest_path, elapsed)
