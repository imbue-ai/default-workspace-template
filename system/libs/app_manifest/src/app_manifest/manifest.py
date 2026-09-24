import re
import tomllib
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from app_manifest.errors import InvalidManifestValueError
from app_manifest.errors import ManifestLoadError
from app_manifest.primitives import AppName
from app_manifest.primitives import DisplayName
from app_manifest.primitives import ExcludeGlob
from app_manifest.primitives import IconPath
from app_manifest.primitives import LaunchParamName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
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

# The one launch path an app that declares none has, at its root; the shell synthesizes it, so a
# manifest never declares it but may name it as its default shortcut.
OPEN_LAUNCH_PATH_ID: Final[LaunchPathId] = LaunchPathId("open")

# The body fields the shell adds to every POST launch beside the params (the post-launch-paths plan,
# section 3.2): the requesting client, its desktop, and the path of the window the launch is aimed at.
# A manifest may declare neither a param nor a preset by these names.
RESERVED_LAUNCH_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {"client_id", "desktop_id", "window_path"}
)

# The placeholders a preview table's command, args, and env values may carry
# (desktop-interface contracts.md): ``{port:<name>}`` for a declared port,
# ``{copy:<key>}`` for a declared copy, and the bare ``{host}``, ``{scratch}``, and
# ``{registry}``. ``open_path`` alone may carry ``{key}``. The isolated-instance
# script fills the port, copy, host, and scratch ones once it has allocated them,
# so it mirrors this pattern; the preview script fills the registry.
PREVIEW_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\{(?P<kind>[a-z_]+)(?::(?P<name>[a-z0-9_-]+))?\}"
)
PREVIEW_PORT_PLACEHOLDER_KIND: Final[str] = "port"
PREVIEW_COPY_PLACEHOLDER_KIND: Final[str] = "copy"
PREVIEW_BARE_PLACEHOLDER_KINDS: Final[frozenset[str]] = frozenset(
    {"host", "scratch", "registry"}
)
PREVIEW_KEY_PLACEHOLDER: Final[str] = "{key}"
MAIN_PORT_NAME: Final[PreviewName] = PreviewName("main")
PREVIEW_DATA_COPY_KEY: Final[PreviewName] = PreviewName("data")
DEFAULT_PREVIEW_HEALTH_PATH: Final[str] = "/health"
DEFAULT_PREVIEW_OPEN_PATH: Final[str] = "/"


class ShortcutMode(LowerCaseStrEnum):
    """How a desktop shortcut behaves: focus the app's most recent window, or always open a new one."""

    FOCUS = auto()
    NEW = auto()


class LaunchPathMethod(UpperCaseStrEnum):
    """How the shell runs a launch path: opens a window at the path (GET), or posts to it and opens a window at the
    path the app answers (POST)."""

    GET = auto()
    POST = auto()


class PinStyle(LowerCaseStrEnum):
    """How a pinned entry may be drawn: the plain icon-and-title entry, or the avatar the shell ships."""

    PLAIN = auto()
    AVATAR = auto()


class LocationScope(LowerCaseStrEnum):
    """Whether a window's path and title are followed by every client, or kept by each client for itself."""

    LINKED = auto()
    INDEPENDENT = auto()


class EntryMode(LowerCaseStrEnum):
    """Where one client shows a pinned entry: in the taskbar, or floating above the windows."""

    BAR = auto()
    FLOATING = auto()


class Pin(FrozenModel):
    """An app's pinned taskbar entry (pinned-taskbar-entries plan section 3.1): one window of the app on every
    desktop, never closed, drawn in the bar or floating."""

    path: LaunchPathValue = Field(
        description="The home path: where the pinned window opens, a page safe to open any number of times"
    )
    style: PinStyle = Field(
        default=PinStyle.PLAIN,
        description="How the entry is drawn by a client that chose nothing",
    )
    scope: LocationScope = Field(
        default=LocationScope.LINKED,
        description="Whether the pinned window keeps a path per client",
    )
    default_mode: EntryMode = Field(
        default=EntryMode.BAR,
        description="Where a client that chose nothing shows the entry",
    )


class LaunchParam(FrozenModel):
    """One documented parameter of a launch path: a GET launch path's query parameter, a POST launch path's body field."""

    name: LaunchParamName = Field(description="The parameter's name")
    label: NonEmptyStr = Field(description="What the parameter is called in prose")
    required: bool = Field(
        default=False,
        description="Whether the launch path refuses a request without it",
    )


class LaunchPath(FrozenModel):
    """A way of starting something the desktop offers (desktop-interface contracts.md section 2): a page path the
    shell opens a window at (GET), or a route the shell posts to for the path of the page to open (POST)."""

    id: LaunchPathId = Field(description="The id shortcuts and layout.py refer to")
    label: NonEmptyStr = Field(description="The launch path's user-facing label")
    path: LaunchPathValue = Field(
        description="The path under the app origin, without a query string"
    )
    method: LaunchPathMethod = Field(
        default=LaunchPathMethod.GET,
        description="GET opens a window at the path with the params as its query; POST posts the params to the path "
        "and opens a window at the path the app answers (post-launch-paths plan section 3.1)",
    )
    params: tuple[LaunchParam, ...] = Field(
        default=(),
        description="The parameters a caller may supply (the query for a GET, the body for a POST), documented",
    )
    presets: dict[LaunchParamName, str] = Field(
        default_factory=dict,
        description="Fixed name-value pairs the shell sends with every launch of the path, beside the caller's params",
    )
    text_param: LaunchParamName | None = Field(
        default=None,
        description="The declared param the launcher fills with typed text; a launch path with one is a free-text "
        "row of the launcher (launcher-and-getting-started plan section 3.1)",
    )
    draft_param: LaunchParamName | None = Field(
        default=None,
        description="The declared param the shell fills with text to be drafted rather than sent: a free-text row of "
        "the launcher too, and what the avatar dialog's draft goes through (post-launch-paths plan section 3.1)",
    )

    @model_validator(mode="after")
    def _check_params_presets_and_text_params(self) -> Self:
        param_names = [str(param.name) for param in self.params]
        for field_name, chosen in (
            ("text_param", self.text_param),
            ("draft_param", self.draft_param),
        ):
            if chosen is not None and chosen not in param_names:
                raise InvalidManifestValueError(
                    f"{field_name} {str(chosen)!r} is not one of the launch path's params {param_names}"
                )
        if self.text_param is not None and self.draft_param is not None:
            raise InvalidManifestValueError(
                "a launch path declares at most one of text_param and draft_param"
            )
        for preset_name in self.presets:
            if preset_name in param_names:
                raise InvalidManifestValueError(
                    f"preset {str(preset_name)!r} is also one of the launch path's params; a name is one or the other"
                )
        for reserved in sorted(
            RESERVED_LAUNCH_PARAM_NAMES.intersection(
                [*param_names, *map(str, self.presets)]
            )
        ):
            raise InvalidManifestValueError(
                f"{reserved!r} is reserved for the shell's launch envelope and cannot be a param or a preset"
            )
        return self


class AppReference(FrozenModel):
    """An artifact outside the app's own directory that belongs to the app."""

    path: ReferencePath = Field(
        description="The literal repo-root-relative file or directory"
    )
    note: ReferenceNote | None = Field(
        default=None,
        description="One line: why it belongs to the app and which surface it uses",
    )


class ScopeRules(FrozenModel):
    """What an app's footprint leaves out on top of the built-in exclusions."""

    exclude: tuple[ExcludeGlob, ...] = Field(
        default=(),
        description="Repo-root-relative gitignore-style globs no pass ever considers",
    )


class WiringRules(FrozenModel):
    """The supervisord programs an app owns beyond its own and its ``<name>-<role>`` sidecars."""

    programs: tuple[ProgramName, ...] = Field(
        default=(),
        description="Program names whose [program:<name>] blocks belong to the app, such as a "
        "display server that exists only for it",
    )


class DefaultShortcut(FrozenModel):
    """The shortcut a new desktop is seeded with for this app."""

    launch: LaunchPathId = Field(
        description="A declared launch path id, or 'open' when the app declares none"
    )
    mode: ShortcutMode = Field(description="focus or new")


def _preview_placeholders(text: str) -> list[tuple[str, str | None]]:
    return [
        (match.group("kind"), match.group("name"))
        for match in PREVIEW_PLACEHOLDER_PATTERN.finditer(text)
    ]


class PreviewSpec(FrozenModel):
    """How a throwaway instance of the app boots for a preview: the manifest's ``[preview]`` table."""

    command: tuple[NonEmptyStr, ...] = Field(
        default=(),
        description="The launch argv; empty runs the app's program as its console script",
    )
    ports: tuple[PreviewName, ...] = Field(
        default=(MAIN_PORT_NAME,),
        description="The named free ports the instance is given; main is always one",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment for the instance; values may carry placeholders",
    )
    args: tuple[str, ...] = Field(
        default=(),
        description="Arguments appended to the command; may carry placeholders",
    )
    copies: dict[PreviewName, str] = Field(
        default_factory=dict,
        description="Repo-relative directories copied into the instance's scratch space, by key",
    )
    health_path: str = Field(
        default=DEFAULT_PREVIEW_HEALTH_PATH,
        description="The path probed for a 200 once booted",
    )
    open_path: str = Field(
        default=DEFAULT_PREVIEW_OPEN_PATH,
        description="The path the preview window opens on",
    )
    open_path_takes_key: bool = Field(
        default=False, description="Whether open_path carries {key}, an instance key"
    )

    @model_validator(mode="after")
    def _check_placeholders_and_names(self) -> Self:
        if MAIN_PORT_NAME not in self.ports:
            raise InvalidManifestValueError(
                f"preview.ports must include {str(MAIN_PORT_NAME)!r}"
            )
        if len(set(self.ports)) != len(self.ports):
            raise InvalidManifestValueError(
                f"preview.ports must be unique, got {list(self.ports)}"
            )
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
            raise InvalidManifestValueError(
                "preview.health_path and preview.open_path must start with '/'"
            )
        if _preview_placeholders(self.health_path):
            raise InvalidManifestValueError("preview.health_path takes no placeholders")
        open_path_without_key = self.open_path.replace(PREVIEW_KEY_PLACEHOLDER, "")
        if _preview_placeholders(open_path_without_key):
            raise InvalidManifestValueError(
                f"preview.open_path may carry only {PREVIEW_KEY_PLACEHOLDER}"
            )
        has_key = PREVIEW_KEY_PLACEHOLDER in self.open_path
        if has_key != self.open_path_takes_key:
            raise InvalidManifestValueError(
                f"preview.open_path carries {PREVIEW_KEY_PLACEHOLDER} exactly when open_path_takes_key is true"
            )
        return self

    def _placeholder_bearing_texts(self) -> list[tuple[str, str]]:
        texts = [("command", part) for part in self.command] + [
            ("args", part) for part in self.args
        ]
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
                raise InvalidManifestValueError(
                    f"preview.{field_name}: {{{kind}}} takes no name, got {name!r}"
                )
        else:
            raise InvalidManifestValueError(
                f"preview.{field_name} carries an unknown placeholder {{{kind}}}"
            )


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
    """An app's static declarations, read from its app.toml (desktop-interface contracts.md section 2)."""

    name: AppName = Field(description="The registered app name")
    display_name: DisplayName = Field(description="What users see")
    icon: IconPath | None = Field(
        default=None,
        description="The icon file, relative to the manifest; required unless internal",
    )
    critical: bool = Field(
        default=False,
        description="No Stop verb; snapshot-and-rollback target in the update apply",
    )
    priority: PriorityName = Field(
        default=DEFAULT_PRIORITY, description="The memory-shedding band name"
    )
    program: ProgramName = Field(
        description="The supervisord program that runs the app (defaults to the name)"
    )
    internal: bool = Field(default=False, description="Hidden from every open surface")
    default_shortcut: DefaultShortcut | None = Field(
        default=None, description="The shortcut a new desktop is seeded with"
    )
    launch_paths: tuple[LaunchPath, ...] = Field(
        default=(),
        description="The paths the desktop interface opens windows at, with their labels and params",
    )
    launcher_rank: int | None = Field(
        default=None,
        ge=1,
        description="The app's place among the launcher's leading tiles (lower first); "
        "an app without one follows every ranked app",
    )
    pin: Pin | None = Field(
        default=None, description="The app's pinned taskbar entry, when it declares one"
    )
    window_closed_path: LaunchPathValue | None = Field(
        default=None,
        description="The path under the app's origin the shell posts to when a window of the app closes; "
        "an app whose resources live as long as their windows sweeps on it",
    )
    references: tuple[AppReference, ...] = Field(
        default=(),
        description="The artifacts outside the app's directory that belong to it",
    )
    scope: ScopeRules = Field(
        default_factory=ScopeRules,
        description="The app's own exclusions from its footprint",
    )
    wiring: WiringRules = Field(
        default_factory=WiringRules,
        description="The extra supervisord programs the app owns",
    )
    handles: dict[str, Any] = Field(
        default_factory=dict, description="Reserved; must be absent or empty"
    )
    preview: PreviewSpec = Field(
        description="How a throwaway instance boots for a preview (the scaffold convention by default)"
    )

    @model_validator(mode="before")
    @classmethod
    def _default_program_to_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and "program" not in data and "name" in data:
            return {**data, "program": data["name"]}
        return data

    @model_validator(mode="before")
    @classmethod
    def _default_preview_to_scaffold_convention(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "preview" not in data
            and isinstance(data.get("name"), str)
        ):
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
        if self.handles:
            raise InvalidManifestValueError(
                "handles must be absent or empty in this release"
            )
        reference_paths = [reference.path for reference in self.references]
        if len(set(reference_paths)) != len(reference_paths):
            raise InvalidManifestValueError(
                f"reference paths must be unique, got {reference_paths}"
            )
        wiring_programs = list(self.wiring.programs)
        if len(set(wiring_programs)) != len(wiring_programs):
            raise InvalidManifestValueError(
                f"wiring programs must be unique, got {wiring_programs}"
            )
        if self.program in wiring_programs:
            raise InvalidManifestValueError(
                f"wiring programs must not repeat the app's own program {str(self.program)!r}"
            )
        launch_path_ids = [launch_path.id for launch_path in self.launch_paths]
        if len(set(launch_path_ids)) != len(launch_path_ids):
            raise InvalidManifestValueError(
                f"launch path ids must be unique, got {launch_path_ids}"
            )
        if OPEN_LAUNCH_PATH_ID in launch_path_ids:
            raise InvalidManifestValueError(
                f"launch path id {str(OPEN_LAUNCH_PATH_ID)!r} is reserved for the synthesized root launch path"
            )
        if self.default_shortcut is not None:
            allowed_launch_ids = (
                set(launch_path_ids) if launch_path_ids else {OPEN_LAUNCH_PATH_ID}
            )
            if self.default_shortcut.launch not in allowed_launch_ids:
                raise InvalidManifestValueError(
                    f"default_shortcut.launch {self.default_shortcut.launch!r} is not one of {sorted(allowed_launch_ids)}"
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


def _is_inside_another_apps_directory(
    repo_root: Path, reference_path: ReferencePath
) -> bool:
    """Whether a reference reaches into some app package under system/apps/."""
    parts = reference_path.split("/")
    # A file that sits directly in system/apps/ (its README) is not an app; only a
    # reference into an app's directory is.
    return (
        tuple(parts[:2]) == APPS_DIRECTORY_PARTS
        and len(parts) > 2
        and repo_root.joinpath(*APPS_DIRECTORY_PARTS, parts[2]).is_dir()
    )


def _find_symlinked_component(
    repo_root: Path, reference_path: ReferencePath
) -> str | None:
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
        if own_app_directory is not None and is_path_covered_by(
            own_app_directory, reference.path
        ):
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
        raise ManifestLoadError(
            f"manifest {path} is invalid: {describe_validation_error(e)}"
        ) from e
    if manifest.icon is not None and not (path.parent / manifest.icon).is_file():
        raise ManifestLoadError(
            f"manifest {path} names icon {str(manifest.icon)!r}, which does not exist beside it"
        )
    resolved_repo_root = (
        repo_root.resolve() if repo_root is not None else repo_root_for_manifest(path)
    )
    if resolved_repo_root is not None:
        _check_references_against_repo_root(path, manifest, resolved_repo_root)
    return manifest


def manifest_icon_path(manifest_path: Path, manifest: AppManifest) -> Path | None:
    """The icon file a manifest names, resolved against the manifest's own directory."""
    if manifest.icon is None:
        return None
    return manifest_path.parent / manifest.icon
