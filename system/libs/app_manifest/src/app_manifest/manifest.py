import re
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
from app_manifest.primitives import ExcludeGlob
from app_manifest.primitives import IconPath
from app_manifest.primitives import InstancesUrl
from app_manifest.primitives import PreviewName
from app_manifest.primitives import PriorityName
from app_manifest.primitives import ProgramName
from app_manifest.primitives import ReferenceNote
from app_manifest.primitives import ReferencePath
from app_manifest.primitives import is_path_covered_by

MANIFEST_FILENAME: Final[str] = "app.toml"

# The directory every app package sits in, which the reference location rules key on.
APPS_DIRECTORY_PARTS: Final[tuple[str, str]] = ("system", "apps")

DEFAULT_PRIORITY: Final[PriorityName] = PriorityName("user")

# The one action every single-instance app has; the shell synthesizes it, so a
# manifest never declares it but may name it as its default shortcut.
OPEN_ACTION_ID: Final[ActionId] = ActionId("open")

# The placeholders a preview table's command, args, and env values may carry
# (contracts.md section 2): ``{port:<name>}`` for a declared port, ``{copy:<key>}``
# for a declared copy, and the bare ``{host}``, ``{scratch}``, ``{registry}``, and
# ``{shell_url}``. ``open_path`` alone may carry ``{key}``. The isolated-instance
# script fills the port, copy, host, and scratch ones once it has allocated them,
# so it mirrors this pattern; the preview script fills the other two.
PREVIEW_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{(?P<kind>[a-z_]+)(?::(?P<name>[a-z0-9_-]+))?\}")
PREVIEW_PORT_PLACEHOLDER_KIND: Final[str] = "port"
PREVIEW_COPY_PLACEHOLDER_KIND: Final[str] = "copy"
PREVIEW_BARE_PLACEHOLDER_KINDS: Final[frozenset[str]] = frozenset({"host", "scratch", "registry", "shell_url"})
PREVIEW_KEY_PLACEHOLDER: Final[str] = "{key}"
MAIN_PORT_NAME: Final[PreviewName] = PreviewName("main")
PREVIEW_DATA_COPY_KEY: Final[PreviewName] = PreviewName("data")
DEFAULT_PREVIEW_HEALTH_PATH: Final[str] = "/health"
DEFAULT_PREVIEW_OPEN_PATH: Final[str] = "/"


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


class AppReference(FrozenModel):
    """An artifact outside the app's own directory that belongs to the app."""

    path: ReferencePath = Field(description="The literal repo-root-relative file or directory")
    note: ReferenceNote | None = Field(
        default=None, description="One line: why it belongs to the app and which surface it uses"
    )


class ScopeRules(FrozenModel):
    """What an app's footprint leaves out on top of the built-in exclusions."""

    exclude: tuple[ExcludeGlob, ...] = Field(
        default=(), description="Repo-root-relative gitignore-style globs no pass ever considers"
    )


class WiringRules(FrozenModel):
    """The supervisord programs an app owns beyond its own and its ``<name>-<role>`` sidecars."""

    programs: tuple[ProgramName, ...] = Field(
        default=(),
        description="Program names whose [program:<name>] blocks belong to the app, such as a "
        "display server that exists only for it",
    )


class DefaultShortcut(FrozenModel):
    """The rail row a new project is seeded with for this app."""

    action: ActionId = Field(description="A declared action id, or 'open' for a single-instance app")
    mode: ShortcutMode = Field(description="focus or new")


def _preview_placeholders(text: str) -> list[tuple[str, str | None]]:
    return [(match.group("kind"), match.group("name")) for match in PREVIEW_PLACEHOLDER_PATTERN.finditer(text)]


class PreviewSpec(FrozenModel):
    """How a throwaway instance of the app boots for a preview: the manifest's ``[preview]`` table."""

    command: tuple[NonEmptyStr, ...] = Field(default=(), description="The launch argv; empty runs the app's program as its console script")
    ports: tuple[PreviewName, ...] = Field(default=(MAIN_PORT_NAME,), description="The named free ports the instance is given; main is always one")
    env: dict[str, str] = Field(default_factory=dict, description="Environment for the instance; values may carry placeholders")
    args: tuple[str, ...] = Field(default=(), description="Arguments appended to the command; may carry placeholders")
    copies: dict[PreviewName, str] = Field(default_factory=dict, description="Repo-relative directories copied into the instance's scratch space, by key")
    health_path: str = Field(default=DEFAULT_PREVIEW_HEALTH_PATH, description="The path probed for a 200 once booted")
    open_path: str = Field(default=DEFAULT_PREVIEW_OPEN_PATH, description="The path the preview tab opens on")
    open_path_takes_key: bool = Field(default=False, description="Whether open_path carries {key}, an instance key")

    @model_validator(mode="after")
    def _check_placeholders_and_names(self) -> Self:
        if MAIN_PORT_NAME not in self.ports:
            raise InvalidManifestValueError(f"preview.ports must include {str(MAIN_PORT_NAME)!r}")
        if len(set(self.ports)) != len(self.ports):
            raise InvalidManifestValueError(f"preview.ports must be unique, got {list(self.ports)}")
        for key, source in self.copies.items():
            path = Path(source)
            if not source or path.is_absolute() or ".." in path.parts:
                raise InvalidManifestValueError(
                    f"preview.copies[{str(key)!r}] must be a repo-relative directory, got {source!r}"
                )
        for field_name, text in self._placeholder_bearing_texts():
            for kind, name in _preview_placeholders(text):
                self._check_placeholder(field_name, kind, name)
        if not self.health_path.startswith("/") or not self.open_path.startswith("/"):
            raise InvalidManifestValueError("preview.health_path and preview.open_path must start with '/'")
        if _preview_placeholders(self.health_path):
            raise InvalidManifestValueError("preview.health_path takes no placeholders")
        open_path_without_key = self.open_path.replace(PREVIEW_KEY_PLACEHOLDER, "")
        if _preview_placeholders(open_path_without_key):
            raise InvalidManifestValueError(f"preview.open_path may carry only {PREVIEW_KEY_PLACEHOLDER}")
        has_key = PREVIEW_KEY_PLACEHOLDER in self.open_path
        if has_key != self.open_path_takes_key:
            raise InvalidManifestValueError(
                f"preview.open_path carries {PREVIEW_KEY_PLACEHOLDER} exactly when open_path_takes_key is true"
            )
        return self

    def _placeholder_bearing_texts(self) -> list[tuple[str, str]]:
        texts = [("command", part) for part in self.command] + [("args", part) for part in self.args]
        texts.extend(("env", value) for value in self.env.values())
        return texts

    def _check_placeholder(self, field_name: str, kind: str, name: str | None) -> None:
        if kind == PREVIEW_PORT_PLACEHOLDER_KIND:
            if name is None or name not in self.ports:
                raise InvalidManifestValueError(
                    f"preview.{field_name} refers to port {name!r}, which preview.ports does not declare"
                )
        elif kind == PREVIEW_COPY_PLACEHOLDER_KIND:
            if name is None or name not in self.copies:
                raise InvalidManifestValueError(
                    f"preview.{field_name} refers to copy {name!r}, which preview.copies does not declare"
                )
        elif kind in PREVIEW_BARE_PLACEHOLDER_KINDS:
            if name is not None:
                raise InvalidManifestValueError(f"preview.{field_name}: {{{kind}}} takes no name, got {name!r}")
        else:
            raise InvalidManifestValueError(f"preview.{field_name} carries an unknown placeholder {{{kind}}}")


def scaffold_env_prefix(name: AppName) -> str:
    """The ``<PACKAGE_UPPER>`` prefix the build-app scaffold gives an app's env vars."""
    return name.replace("-", "_").upper()


def scaffold_preview_spec(name: AppName) -> PreviewSpec:
    """The preview table an app gets by saying nothing: the build-app scaffold's convention.

    The scaffold binds ``<PACKAGE_UPPER>_PORT`` and ``<PACKAGE_UPPER>_HOST`` and reads its
    store from ``<PACKAGE_UPPER>_DATA_DIR``, so a scaffolded app previews by construction
    over a scratch copy of its data.
    """
    prefix = scaffold_env_prefix(name)
    return PreviewSpec(
        env={
            f"{prefix}_PORT": f"{{{PREVIEW_PORT_PLACEHOLDER_KIND}:{MAIN_PORT_NAME}}}",
            f"{prefix}_HOST": "{host}",
            f"{prefix}_DATA_DIR": f"{{{PREVIEW_COPY_PLACEHOLDER_KIND}:{PREVIEW_DATA_COPY_KEY}}}",
        },
        copies={PREVIEW_DATA_COPY_KEY: f"data/.apps/{name}"},
    )


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
    launcher_rank: int | None = Field(
        default=None,
        ge=1,
        description="The app's place among the New Tab page's leading tiles (lower first); "
        "an app without one follows every ranked app",
    )
    references: tuple[AppReference, ...] = Field(
        default=(), description="The artifacts outside the app's directory that belong to it"
    )
    scope: ScopeRules = Field(
        default_factory=ScopeRules, description="The app's own exclusions from its footprint"
    )
    wiring: WiringRules = Field(
        default_factory=WiringRules, description="The extra supervisord programs the app owns"
    )
    handles: dict[str, Any] = Field(default_factory=dict, description="Reserved; must be absent or empty")
    preview: PreviewSpec = Field(description="How a throwaway instance boots for a preview (the scaffold convention by default)")

    @model_validator(mode="before")
    @classmethod
    def _default_program_to_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and "program" not in data and "name" in data:
            return {**data, "program": data["name"]}
        return data

    @model_validator(mode="before")
    @classmethod
    def _default_preview_to_scaffold_convention(cls, data: Any) -> Any:
        if isinstance(data, dict) and "preview" not in data and isinstance(data.get("name"), str):
            try:
                name = AppName(data["name"])
            except InvalidManifestValueError:
                return data
            return {**data, "preview": scaffold_preview_spec(name)}
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
        reference_paths = [reference.path for reference in self.references]
        if len(set(reference_paths)) != len(reference_paths):
            raise InvalidManifestValueError(
                f"reference paths must be unique, got {reference_paths}"
            )
        wiring_programs = list(self.wiring.programs)
        if len(set(wiring_programs)) != len(wiring_programs):
            raise InvalidManifestValueError(f"wiring programs must be unique, got {wiring_programs}")
        if self.program in wiring_programs:
            raise InvalidManifestValueError(
                f"wiring programs must not repeat the app's own program {str(self.program)!r}"
            )
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


def repo_root_for_manifest(manifest_path: Path) -> Path | None:
    """The repo root a manifest at ``system/apps/<package>/app.toml`` sits in, or None off that layout."""
    resolved = manifest_path.resolve()
    if len(resolved.parents) < 4:
        return None
    system_directory_name, apps_directory_name = APPS_DIRECTORY_PARTS
    if resolved.parents[1].name != apps_directory_name:
        return None
    if resolved.parents[2].name != system_directory_name:
        return None
    return resolved.parents[3]


def app_package_directory(repo_root: Path, manifest_path: Path) -> str | None:
    """The manifest's own app directory as a repo-relative path with a trailing slash, when it is inside the root."""
    app_directory = manifest_path.resolve().parent
    if not app_directory.is_relative_to(repo_root):
        return None
    return f"{app_directory.relative_to(repo_root).as_posix()}/"


def _is_inside_another_apps_directory(repo_root: Path, reference_path: ReferencePath) -> bool:
    """Whether a reference reaches into some app package under system/apps/."""
    parts = reference_path.split("/")
    # A file that sits directly in system/apps/ (its README) is not an app; only a
    # reference into an app's directory is.
    return (
        tuple(parts[:2]) == APPS_DIRECTORY_PARTS
        and len(parts) > 2
        and repo_root.joinpath(*APPS_DIRECTORY_PARTS, parts[2]).is_dir()
    )


def _find_symlinked_component(repo_root: Path, reference_path: ReferencePath) -> str | None:
    """The first component of a reference that is itself a symlink, or None when none is.

    Git reports a changed file under the real directory and never under a symlink to it
    (``.claude/skills`` is a tracked symlink to ``.agents/skills``), so a reference that
    goes through one covers nothing at all.
    """
    walked = repo_root
    for segment in reference_path.split("/"):
        walked = walked / segment
        if walked.is_symlink():
            return walked.relative_to(repo_root).as_posix()
    return None


def _check_references_against_repo_root(
    path: Path, manifest: AppManifest, repo_root: Path
) -> None:
    """Raises ManifestLoadError when a reference sits where it may not, or names nothing that exists."""
    own_app_directory = app_package_directory(repo_root, path)
    for reference in manifest.references:
        if own_app_directory is not None and is_path_covered_by(own_app_directory, reference.path):
            raise ManifestLoadError(
                f"manifest {path} is invalid: reference {str(reference.path)!r} is inside the app's "
                f"own directory {own_app_directory!r}, which is already implicit"
            )
        if _is_inside_another_apps_directory(repo_root, reference.path):
            raise ManifestLoadError(
                f"manifest {path} is invalid: reference {str(reference.path)!r} names another app's "
                "directory; an app-to-app dependency belongs in pyproject.toml, where it is already "
                "derivable"
            )
        if not (repo_root / reference.path).exists():
            raise ManifestLoadError(
                f"manifest {path} references {str(reference.path)!r}, which does not exist under {repo_root}"
            )
        symlinked_component = _find_symlinked_component(repo_root, reference.path)
        if symlinked_component is not None:
            raise ManifestLoadError(
                f"manifest {path} references {str(reference.path)!r}, which goes through the symlink "
                f"{symlinked_component!r}; git never reports a changed file through a symlink, so the "
                "reference would cover nothing -- name the real path instead"
            )


def load_manifest(path: Path, *, repo_root: Path | None = None) -> AppManifest:
    """Read and validate an app.toml, also checking that the icon it names exists beside it.

    The reference rules that need the manifest's place in the tree (every reference exists, and
    none names this app's own directory or another app's) are checked against ``repo_root`` when
    it is given, and otherwise against the root derived from a manifest that sits at
    ``system/apps/<package>/app.toml``. A manifest anywhere else (a temp directory in a test)
    with no explicit root skips those checks; the value rules still apply.
    """
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
    resolved_repo_root = repo_root.resolve() if repo_root is not None else repo_root_for_manifest(path)
    if resolved_repo_root is not None:
        _check_references_against_repo_root(path, manifest, resolved_repo_root)
    return manifest


def manifest_icon_path(manifest_path: Path, manifest: AppManifest) -> Path | None:
    """The icon file a manifest names, resolved against the manifest's own directory."""
    if manifest.icon is None:
        return None
    return manifest_path.parent / manifest.icon
