import tomllib
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from app_manifest.errors import InvalidManifestValueError
from app_manifest.errors import ManifestLoadError
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from app_manifest.primitives import DisplayName
from app_manifest.primitives import IconPath
from app_manifest.primitives import InstancesUrl
from app_manifest.primitives import PriorityName
from app_manifest.primitives import ProgramName

MANIFEST_FILENAME: Final[str] = "app.toml"

DEFAULT_PRIORITY: Final[PriorityName] = PriorityName("user")

# The one action every single-instance app has; the shell synthesizes it, so a
# manifest never declares it but may name it as its default shortcut.
OPEN_ACTION_ID: Final[ActionId] = ActionId("open")


class ShortcutMode(LowerCaseStrEnum):
    """How a rail shortcut behaves: focus the app's most recent tab, or always create anew."""

    FOCUS = auto()
    NEW = auto()


class ActionParam(FrozenModel):
    """One documented key of an action's create body."""

    name: NonEmptyStr = Field(description="The key in the create body's params")
    label: NonEmptyStr = Field(description="What the parameter is called in prose")
    required: bool = Field(default=False, description="Whether the create refuses a body without it")


class AppAction(FrozenModel):
    """A way of creating an instance that an app declares in its manifest."""

    id: ActionId = Field(description="The id shortcuts and layout.py refer to")
    label: NonEmptyStr = Field(description="The action's user-facing label")
    params: tuple[ActionParam, ...] = Field(default=(), description="The create body's documented params")


class DefaultShortcut(FrozenModel):
    """The rail row a new project is seeded with for this app."""

    action: ActionId = Field(description="A declared action id, or 'open' for a single-instance app")
    mode: ShortcutMode = Field(description="focus or new")


class AppManifest(FrozenModel):
    """An app's static declarations, read from its app.toml (contracts.md section 2)."""

    name: AppName = Field(description="The registered app name")
    display_name: DisplayName = Field(description="What users see")
    icon: IconPath | None = Field(default=None, description="The icon file, relative to the manifest; required unless internal")
    instances: bool = Field(default=False, description="Whether the app serves the instances API")
    instances_url: InstancesUrl | None = Field(default=None, description="Where the instances API is served when not at the app URL")
    critical: bool = Field(default=False, description="No Stop verb; snapshot-and-rollback target in the update apply")
    priority: PriorityName = Field(default=DEFAULT_PRIORITY, description="The memory-shedding band name")
    program: ProgramName = Field(description="The supervisord program that runs the app (defaults to the name)")
    internal: bool = Field(default=False, description="Hidden from every open surface")
    default_shortcut: DefaultShortcut | None = Field(default=None, description="The rail row a new project is seeded with")
    actions: tuple[AppAction, ...] = Field(default=(), description="The declared create actions")
    handles: dict[str, Any] = Field(default_factory=dict, description="Reserved; must be absent or empty")

    @model_validator(mode="before")
    @classmethod
    def _default_program_to_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and "program" not in data and "name" in data:
            return {**data, "program": data["name"]}
        return data

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> Self:
        if self.icon is None and not self.internal:
            raise InvalidManifestValueError("icon is required unless internal = true")
        if self.instances_url is not None and not self.instances:
            raise InvalidManifestValueError("instances_url is only allowed with instances = true")
        if self.actions and not self.instances:
            raise InvalidManifestValueError("actions are only allowed with instances = true")
        if self.handles:
            raise InvalidManifestValueError("handles must be absent or empty in this release")
        action_ids = [action.id for action in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise InvalidManifestValueError(f"action ids must be unique, got {action_ids}")
        if self.default_shortcut is not None:
            allowed_ids = set(action_ids) if self.instances else {OPEN_ACTION_ID}
            if self.default_shortcut.action not in allowed_ids:
                raise InvalidManifestValueError(
                    f"default_shortcut.action {self.default_shortcut.action!r} is not one of {sorted(allowed_ids)}"
                )
        return self


def describe_validation_error(error: ValidationError) -> str:
    """One line naming the first failing field and why, for logs and CLI output."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"field {location!r}: {first['msg']}"


def load_manifest(path: Path) -> AppManifest:
    """Read and validate an app.toml, also checking that the icon it names exists beside it."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestLoadError(f"cannot read manifest {path}: {e}") from e
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestLoadError(f"manifest {path} is not valid TOML: {e}") from e
    try:
        manifest = AppManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestLoadError(f"manifest {path} is invalid: {describe_validation_error(e)}") from e
    if manifest.icon is not None and not (path.parent / manifest.icon).is_file():
        raise ManifestLoadError(f"manifest {path} names icon {str(manifest.icon)!r}, which does not exist beside it")
    return manifest


def manifest_icon_path(manifest_path: Path, manifest: AppManifest) -> Path | None:
    """The icon file a manifest names, resolved against the manifest's own directory."""
    if manifest.icon is None:
        return None
    return manifest_path.parent / manifest.icon

