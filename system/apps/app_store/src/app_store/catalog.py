"""The curated app catalog: the open-source apps the store offers, and exactly how each is installed and run.

The catalog ships inside this package (``catalog/apps.toml``, with one icon per app under ``catalog/icons/``)
rather than being fetched: an install recipe is effectively code that runs in the user's workspace, so it is
reviewed and versioned with the workspace like any other code. Every entry is validated when the store starts, and
a catalog that does not validate stops the store rather than offering a half-checked app.

An install recipe is a discriminated union on ``kind`` -- a Python package into its own uv-made environment, an
npm package into its own prefix, either of those straight from a pinned GitHub commit, a site built from a pinned
GitHub commit, or a ready-made site taken from one pinned Docker Hub image layer -- and never a shell string. The
launch command is an argv list whose only substitutions are the placeholders below; it must run something from the
app's own environment and bind loopback.

The same models carry the apps a user adds (``added_apps.py``), which are held to every rule here except the shipped
catalog's own extras: a category and measured resource figures.
"""

import re
import string
import tomllib
from pathlib import Path
from typing import Annotated
from typing import Final
from typing import Literal

from pydantic import AfterValidator
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

from app_manifest.manifest import describe_validation_error
from app_store.errors import CatalogEntryError
from app_store.errors import CatalogError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

CATALOG_DIRECTORY: Final[Path] = Path(__file__).parent / "catalog"
CATALOG_PATH: Final[Path] = CATALOG_DIRECTORY / "apps.toml"
ICONS_DIRECTORY: Final[Path] = CATALOG_DIRECTORY / "icons"

# What a launch or init argv may substitute: the port the app listens on, its own data folder, and its environment.
PORT_PLACEHOLDER: Final[str] = "port"
DATA_DIR_PLACEHOLDER: Final[str] = "data_dir"
ENV_DIR_PLACEHOLDER: Final[str] = "env_dir"
# The label of the app's own origin (its hostname's first label, e.g. jupyterlab-f8n3n82u), which the workspace mints
# when the app first registers; random per workspace, so an app can trust requests from its own address and refuse
# every other site's without a cookie. Known only at launch, after registration, so an init argv cannot use it.
ORIGIN_LABEL_PLACEHOLDER: Final[str] = "origin_label"
INIT_PLACEHOLDERS: Final[frozenset[str]] = frozenset({PORT_PLACEHOLDER, DATA_DIR_PLACEHOLDER, ENV_DIR_PLACEHOLDER})
ALLOWED_PLACEHOLDERS: Final[frozenset[str]] = INIT_PLACEHOLDERS | {ORIGIN_LABEL_PLACEHOLDER}
LOOPBACK_HOST: Final[str] = "127.0.0.1"

# A slug is the installed app's name, so it becomes a supervisord program, a drop-in file name and a hostname label.
# No hyphen: a hyphenated program with no manifest under system/apps/ reads as a standalone program whose first label
# would have to be a reserved app name (system/test_app_manifests.py).
SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]{1,30}$")
_PYTHON_REQUIREMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,_-]+\])?==(?P<version>[A-Za-z0-9.+!-]+)$"
)
_NPM_PACKAGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>(?:@[a-z0-9._-]+/)?[a-z0-9._-]+)@(?P<version>\d+\.\d+\.\d+[A-Za-z0-9.+-]*)$"
)
_PYTHON_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^3\.\d{1,2}$")
_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^https://[^\s]+$")


def _check_python_version(value: str) -> str:
    if _PYTHON_VERSION_PATTERN.match(value) is None:
        raise CatalogEntryError(f"python {value!r} must be a 3.x version")
    return value


def _check_pinned_node_tools(value: tuple[str, ...]) -> tuple[str, ...]:
    for package in value:
        if _NPM_PACKAGE_PATTERN.match(package) is None:
            raise CatalogEntryError(f"node tool {package!r} must pin one exact version, as name@x.y.z")
    return value


@pure
def _is_plain_relative_path(value: str) -> bool:
    """Whether ``value`` is a relative path of plain names that cannot climb out of where it is joined."""
    return _RELATIVE_PATH_PATTERN.match(value) is not None and ".." not in value.split("/")


class PythonRecipe(FrozenModel):
    """A Python package, pinned, installed into the app's own environment by ``uv``."""

    kind: Literal["python"] = Field(description="The recipe kind")
    requirement: str = Field(description="The pinned requirement, e.g. jupyterlab==4.4.0")
    python: str = Field(default="3.12", description="The Python version the environment is made with")

    @field_validator("requirement")
    @classmethod
    def _requirement_is_pinned(cls, value: str) -> str:
        if _PYTHON_REQUIREMENT_PATTERN.match(value) is None:
            raise CatalogEntryError(f"requirement {value!r} must pin one exact version, as name==version")
        return value

    @field_validator("python")
    @classmethod
    def _python_is_a_version(cls, value: str) -> str:
        return _check_python_version(value)

    @property
    def pinned_version(self) -> str:
        match = _PYTHON_REQUIREMENT_PATTERN.match(self.requirement)
        assert match is not None, "validated on construction"
        return match.group("version")

    @property
    def source_label(self) -> str:
        return "Python package"

    @property
    def package_spec(self) -> str:
        return self.requirement


class NodeRecipe(FrozenModel):
    """An npm package, pinned, installed into the app's own prefix by ``npm``."""

    kind: Literal["node"] = Field(description="The recipe kind")
    package: str = Field(description="The pinned package, e.g. node-red@4.0.9")

    @field_validator("package")
    @classmethod
    def _package_is_pinned(cls, value: str) -> str:
        if _NPM_PACKAGE_PATTERN.match(value) is None:
            raise CatalogEntryError(f"package {value!r} must pin one exact version, as name@x.y.z")
        return value

    @property
    def pinned_version(self) -> str:
        match = _NPM_PACKAGE_PATTERN.match(self.package)
        assert match is not None, "validated on construction"
        return match.group("version")

    @property
    def source_label(self) -> str:
        return "npm package"

    @property
    def package_spec(self) -> str:
        return self.package


_GITHUB_REPOSITORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+[A-Za-z0-9.+-]*)$")
_RELATIVE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
# What a build step may substitute: only the app's environment (where its pinned build tools are).
BUILD_PLACEHOLDERS: Final[frozenset[str]] = frozenset({ENV_DIR_PLACEHOLDER})
SHORT_COMMIT_LENGTH: Final[int] = 12
GITHUB_ROOT: Final[str] = "https://github.com/"
# A Python package name or extra name (PEP 508's identifier).
_EXTRA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def check_github_repository(value: str) -> str:
    """``value`` when it is a GitHub repository's address, https://github.com/<owner>/<name>."""
    if _GITHUB_REPOSITORY_PATTERN.match(value) is None:
        raise CatalogEntryError(f"repository {value!r} must be https://github.com/<owner>/<name>")
    return value


def check_commit(value: str) -> str:
    """``value`` when it is a full 40-character commit hash."""
    if _COMMIT_PATTERN.match(value) is None:
        raise CatalogEntryError(f"commit {value!r} must be a full 40-character hash")
    return value


# A recipe field holding a GitHub repository's address, or a full commit hash; refused otherwise.
GithubRepository = Annotated[str, AfterValidator(check_github_repository)]
CommitHash = Annotated[str, AfterValidator(check_commit)]


@pure
def short_commit(commit: str) -> str:
    return commit[:SHORT_COMMIT_LENGTH]


class GithubBuildRecipe(FrozenModel):
    """A reviewed app built from one exact commit of its GitHub repository: pinned npm tools are installed into the
    app's environment, the commit is fetched by its hash (so what is built is exactly what was reviewed), the build
    steps run in the checkout, and only the built site (``output``) is kept, as ``{env_dir}/site``."""

    kind: Literal["github_build"] = Field(description="The recipe kind")
    repository: GithubRepository = Field(description="The repository, e.g. https://github.com/excalidraw/excalidraw")
    ref: str = Field(
        default="",
        description="The release tag the commit is, e.g. v0.18.1; empty for a commit that is not a release (the "
        "commit is what is fetched either way)",
    )
    commit: CommitHash = Field(description="The full commit hash")
    node_tools: tuple[str, ...] = Field(
        description="Pinned npm packages installed into the environment: the build tools and the server"
    )
    build: tuple[tuple[str, ...], ...] = Field(
        min_length=1, description="The build steps, argv lists run in the checkout"
    )
    build_env: dict[str, str] = Field(default_factory=dict, description="Environment for the build steps")
    output: str = Field(description="The built site's folder, relative to the checkout")

    @field_validator("ref")
    @classmethod
    def _ref_is_a_release(cls, value: str) -> str:
        if value != "" and _RELEASE_TAG_PATTERN.match(value) is None:
            raise CatalogEntryError(f"ref {value!r} must be a release tag like v1.2.3, or empty")
        return value

    @field_validator("node_tools")
    @classmethod
    def _tools_are_pinned(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_pinned_node_tools(value)

    @field_validator("output")
    @classmethod
    def _output_is_inside_the_checkout(cls, value: str) -> str:
        if not _is_plain_relative_path(value):
            raise CatalogEntryError(f"output {value!r} must be a plain path inside the checkout")
        return value

    @model_validator(mode="after")
    def _build_runs_pinned_tools(self) -> "GithubBuildRecipe":
        for step in self.build:
            _check_argv(step, "build step", BUILD_PLACEHOLDERS)
        _check_placeholders(tuple(self.build_env.values()), BUILD_PLACEHOLDERS, "build env")
        return self

    @property
    def pinned_version(self) -> str:
        match = _RELEASE_TAG_PATTERN.match(self.ref)
        return match.group("version") if match is not None else short_commit(self.commit)

    @property
    def source_label(self) -> str:
        return "Built from GitHub"

    @property
    def package_spec(self) -> str:
        repository = self.repository.removeprefix(GITHUB_ROOT)
        if self.ref == "":
            return f"{repository} ({short_commit(self.commit)})"
        return f"{repository} {self.ref} ({short_commit(self.commit)})"


class PythonGitRecipe(FrozenModel):
    """A Python package installed by ``uv`` straight from one exact commit of its GitHub repository (an unreleased
    package, or an unreleased version of one), into the app's own environment."""

    kind: Literal["python_git"] = Field(description="The recipe kind")
    repository: GithubRepository = Field(description="The repository, e.g. https://github.com/simonw/datasette")
    commit: CommitHash = Field(description="The full commit hash installed")
    subdirectory: str = Field(default="", description="The package's folder in the repository; empty for the top")
    package_name: str = Field(
        default="", description="The package's own name, e.g. glances; needed only to ask for extras"
    )
    extras: tuple[str, ...] = Field(default=(), description="Optional extras to install with it, e.g. web")
    python: str = Field(default="3.12", description="The Python version the environment is made with")

    @field_validator("subdirectory")
    @classmethod
    def _subdirectory_is_inside(cls, value: str) -> str:
        if value != "" and not _is_plain_relative_path(value):
            raise CatalogEntryError(f"subdirectory {value!r} must be a plain path inside the repository")
        return value

    @field_validator("python")
    @classmethod
    def _python_is_a_version(cls, value: str) -> str:
        return _check_python_version(value)

    @field_validator("extras")
    @classmethod
    def _extras_are_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for extra in value:
            if _EXTRA_PATTERN.match(extra) is None:
                raise CatalogEntryError(f"extra {extra!r} must be a plain name, e.g. web")
        return value

    @model_validator(mode="after")
    def _extras_name_the_package(self) -> "PythonGitRecipe":
        if self.package_name != "" and _EXTRA_PATTERN.match(self.package_name) is None:
            raise CatalogEntryError(f"package_name {self.package_name!r} must be a plain package name")
        if self.extras and self.package_name == "":
            raise CatalogEntryError("extras need the package's own name in package_name, e.g. glances")
        return self

    @property
    def requirement(self) -> str:
        """What ``uv pip install`` is given: the repository at the commit, the package's folder when not the top, and
        as ``name[extras] @ ...`` when extras are asked for."""
        suffix = f"#subdirectory={self.subdirectory}" if self.subdirectory else ""
        url = f"git+{self.repository}@{self.commit}{suffix}"
        if not self.extras:
            return url
        return f"{self.package_name}[{','.join(self.extras)}] @ {url}"

    @property
    def pinned_version(self) -> str:
        return short_commit(self.commit)

    @property
    def source_label(self) -> str:
        return "Python package from GitHub"

    @property
    def package_spec(self) -> str:
        return self.requirement


class NodeGitRecipe(FrozenModel):
    """A Node package installed by ``npm`` straight from one exact commit of its GitHub repository, into the app's
    own prefix."""

    kind: Literal["node_git"] = Field(description="The recipe kind")
    repository: GithubRepository = Field(description="The repository, e.g. https://github.com/owner/name")
    commit: CommitHash = Field(description="The full commit hash installed")

    @property
    def package(self) -> str:
        """What ``npm install`` is given: the repository at the commit."""
        return f"git+{self.repository}.git#{self.commit}"

    @property
    def pinned_version(self) -> str:
        return short_commit(self.commit)

    @property
    def source_label(self) -> str:
        return "npm package from GitHub"

    @property
    def package_spec(self) -> str:
        return self.package


_DOCKER_HUB_IMAGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*$"
)
_LAYER_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLISHED_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ImageSiteRecipe(FrozenModel):
    """A reviewed app whose own project publishes it ready-made as a Docker Hub image: the one image layer holding the
    built site is downloaded by its digest (which is its sha256, checked, so what is installed is exactly what was
    reviewed), the site's folder inside the layer is kept as ``{env_dir}/site``, and pinned npm tools (the server) are
    installed beside it. Nothing is built, so the install needs no more memory than a download."""

    kind: Literal["image_site"] = Field(description="The recipe kind")
    image: str = Field(description="The Docker Hub image, e.g. excalidraw/excalidraw")
    published: str = Field(description="The day the reviewed image was published, YYYY-MM-DD; shown as the version")
    layer: str = Field(description="The digest of the layer that holds the site, sha256:<64 hex>")
    path: str = Field(description="The site's folder inside the layer, e.g. usr/share/nginx/html")
    node_tools: tuple[str, ...] = Field(min_length=1, description="Pinned npm packages installed: the server")

    @field_validator("image")
    @classmethod
    def _image_is_on_docker_hub(cls, value: str) -> str:
        if _DOCKER_HUB_IMAGE_PATTERN.match(value) is None:
            raise CatalogEntryError(f"image {value!r} must be a Docker Hub <owner>/<name>")
        return value

    @field_validator("published")
    @classmethod
    def _published_is_a_date(cls, value: str) -> str:
        if _PUBLISHED_DATE_PATTERN.match(value) is None:
            raise CatalogEntryError(f"published {value!r} must be a date, YYYY-MM-DD")
        return value

    @field_validator("layer")
    @classmethod
    def _layer_is_a_digest(cls, value: str) -> str:
        if _LAYER_DIGEST_PATTERN.match(value) is None:
            raise CatalogEntryError(f"layer {value!r} must be a digest, sha256:<64 hex>")
        return value

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        if not _is_plain_relative_path(value):
            raise CatalogEntryError(f"path {value!r} must be a plain relative path inside the layer")
        return value

    @field_validator("node_tools")
    @classmethod
    def _tools_are_pinned(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_pinned_node_tools(value)

    @property
    def pinned_version(self) -> str:
        return self.published

    @property
    def source_label(self) -> str:
        return "Ready-made image"

    @property
    def package_spec(self) -> str:
        return f"{self.image} {self.published} ({self.layer.removeprefix('sha256:')[:12]})"


AnyRecipe = PythonRecipe | NodeRecipe | PythonGitRecipe | NodeGitRecipe | GithubBuildRecipe | ImageSiteRecipe
InstallRecipe = Annotated[AnyRecipe, Field(discriminator="kind")]
# The kinds that install from one commit of a GitHub repository.
GitRecipe = PythonGitRecipe | NodeGitRecipe | GithubBuildRecipe


@pure
def argv_placeholders(argv: tuple[str, ...]) -> set[str]:
    """Every ``{name}`` placeholder the argv uses."""
    names: set[str] = set()
    for argument in argv:
        for _literal, field_name, _spec, _conversion in string.Formatter().parse(argument):
            if field_name is not None:
                names.add(field_name)
    return names


def _check_placeholders(values: tuple[str, ...], allowed: frozenset[str], what: str) -> None:
    try:
        unknown = argv_placeholders(values) - allowed
    except ValueError as e:
        raise CatalogEntryError(f"{what} has a malformed placeholder: {e}") from e
    if unknown:
        raise CatalogEntryError(f"{what} uses unknown placeholders {sorted(unknown)}; allowed: {sorted(allowed)}")


def _check_argv(argv: tuple[str, ...], what: str, allowed: frozenset[str]) -> None:
    if len(argv) == 0:
        raise CatalogEntryError(f"{what} is empty")
    _check_placeholders(argv, allowed, what)
    # Whatever runs comes from the app's own environment, never from the workspace's PATH.
    if not argv[0].startswith("{" + ENV_DIR_PLACEHOLDER + "}/"):
        raise CatalogEntryError(
            f"{what} must run a program from the app's environment ({{env_dir}}/...), not {argv[0]!r}"
        )


# A settings file an app is started with: at most this many, each at most this long.
MAX_SETUP_FILES: Final[int] = 5
MAX_SETUP_FILE_CHARACTERS: Final[int] = 20_000
_SETUP_PATH_PART: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_MAX_SETUP_PATH_DEPTH: Final[int] = 4
# Settings, never code: a file the store writes must be one an app reads, not one it runs.
SETUP_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"yaml", "yml", "json", "toml", "ini", "cfg", "conf", "env", "properties", "txt"}
)


@pure
def setup_path_problem(path: str) -> str | None:
    """Why ``path`` cannot name a settings file inside an app's data folder, in plain words; None when it can. Only a
    relative path of plain names: never absolute, never ``..``, never hidden tricks."""
    if path == "" or path.startswith(("/", "~")) or "\\" in path:
        return f"{path!r} must be a plain path inside the app's own folder, like config.yaml."
    parts = path.split("/")
    if len(parts) > _MAX_SETUP_PATH_DEPTH or any(part in ("", ".", "..") for part in parts):
        return f"{path!r} must be a plain path inside the app's own folder, like config.yaml."
    if any(_SETUP_PATH_PART.match(part) is None for part in parts):
        return f"{path!r} may use only letters, digits, dots, dashes and underscores."
    if "." not in parts[-1] or parts[-1].rsplit(".", 1)[1].lower() not in SETUP_FILE_EXTENSIONS:
        return (
            f"{path!r} isn't a settings file. Only settings files can be written "
            f"({', '.join('.' + extension for extension in sorted(SETUP_FILE_EXTENSIONS))}), never code."
        )
    return None


@pure
def render_setup_content(content: str, port: int, data_dir: Path) -> str:
    """A settings file's text with ``{port}`` and ``{data_dir}`` filled in; every other brace is left as written."""
    return content.replace("{port}", str(port)).replace("{data_dir}", str(data_dir))


class SetupFile(FrozenModel):
    """A settings file written into the app's data folder before its first start (an existing file is never
    replaced, so the person's own edits survive a repair)."""

    path: str = Field(description="Where it goes, relative to the app's data folder, e.g. config.yaml")
    content: str = Field(description="Its text; {port} and {data_dir} are filled in")

    @model_validator(mode="after")
    def _is_safe(self) -> "SetupFile":
        problem = setup_path_problem(self.path)
        if problem is not None:
            raise CatalogEntryError(problem)
        if len(self.content) > MAX_SETUP_FILE_CHARACTERS:
            raise CatalogEntryError(f"{self.path} is longer than {MAX_SETUP_FILE_CHARACTERS} characters")
        return self


class LaunchCommand(FrozenModel):
    """How the installed app is started: an optional one-time init, then the long-running server."""

    argv: tuple[str, ...] = Field(
        description="The server's argv; placeholders {port}, {data_dir}, {env_dir} and {origin_label}"
    )
    init_argv: tuple[str, ...] = Field(
        default=(),
        description="Run once while configuring, before the first start, when the app needs its data folder set up; "
        "placeholders {port}, {data_dir} and {env_dir}",
    )
    env: dict[str, str] = Field(default_factory=dict, description="Extra environment variables, placeholders allowed")
    setup_files: tuple[SetupFile, ...] = Field(
        default=(), description="Settings files written into the data folder before the first start"
    )

    @model_validator(mode="after")
    def _argv_is_safe(self) -> "LaunchCommand":
        if len(self.setup_files) > MAX_SETUP_FILES:
            raise CatalogEntryError(f"at most {MAX_SETUP_FILES} settings files")
        _check_argv(self.argv, "launch argv", ALLOWED_PLACEHOLDERS)
        _check_placeholders(tuple(self.env.values()), ALLOWED_PLACEHOLDERS, "launch env")
        if PORT_PLACEHOLDER not in argv_placeholders(self.argv):
            raise CatalogEntryError("launch argv must pass the app its {port}")
        # Bound to loopback: the workspace's forwarder reaches the app from inside the container.
        if not any(LOOPBACK_HOST in argument for argument in self.argv) and not any(
            LOOPBACK_HOST in value for value in self.env.values()
        ):
            raise CatalogEntryError(f"launch argv must bind {LOOPBACK_HOST}")
        if self.init_argv:
            _check_argv(self.init_argv, "init argv", INIT_PLACEHOLDERS)
        return self


class ResourceEstimate(FrozenModel):
    """What the app costs. A curated entry carries all three figures, measured when it was verified; an app a user
    added carries a disk estimate worked out from its package registry (or none, when that failed) and no memory
    figure, which is measured once it has run."""

    disk_mb: int | None = Field(gt=0, description="Disk its environment takes once installed, in MB; null if unknown")
    memory_idle_mb: int | None = Field(
        default=None, gt=0, description="Memory it holds with nobody using it, in MB; null if unknown"
    )
    memory_in_use_mb: int | None = Field(
        default=None, gt=0, description="Memory it holds while one person uses it, in MB; null if unknown"
    )
    build_disk_mb: int | None = Field(
        default=None, gt=0, description="Disk the install needs while it builds (checkout and tools), in MB"
    )
    build_memory_mb: int | None = Field(default=None, gt=0, description="Memory the build needs while it runs, in MB")
    basis: str = Field(default="", description="How the figures were worked out, in plain words")

    @property
    def install_disk_mb(self) -> int | None:
        """The most disk the install needs at any point: while it builds, or once installed."""
        figures = [figure for figure in (self.disk_mb, self.build_disk_mb) if figure is not None]
        return max(figures) if figures else None

    @property
    def install_memory_mb(self) -> int | None:
        """The most memory it needs: while it builds, or while it is used."""
        figures = [figure for figure in (self.memory_in_use_mb, self.build_memory_mb) if figure is not None]
        return max(figures) if figures else None

    @property
    def is_complete(self) -> bool:
        return None not in (self.disk_mb, self.memory_idle_mb, self.memory_in_use_mb)

    @model_validator(mode="after")
    def _in_use_is_at_least_idle(self) -> "ResourceEstimate":
        if (
            self.memory_in_use_mb is not None
            and self.memory_idle_mb is not None
            and self.memory_in_use_mb < self.memory_idle_mb
        ):
            raise CatalogEntryError(
                f"memory_in_use_mb ({self.memory_in_use_mb}) is below memory_idle_mb ({self.memory_idle_mb})"
            )
        return self


CATEGORY_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class AppCategory(FrozenModel):
    """One of the store's filters: a kind of app."""

    key: str = Field(description="What an app's tags name it by")
    title: str = Field(min_length=1, max_length=40, description="What the filter says")

    @field_validator("key")
    @classmethod
    def _key_is_a_slug(cls, value: str) -> str:
        if CATEGORY_KEY_PATTERN.match(value) is None:
            raise CatalogEntryError(f"category key {value!r} must be lowercase letters, digits and hyphens")
        return value


class CatalogApp(FrozenModel):
    """One app the store offers."""

    slug: str = Field(description="The installed app's name: lowercase letters and digits")
    tags: tuple[str, ...] = Field(
        default=(), description="The keys of the categories it belongs to (a curated app has at least one)"
    )
    title: str = Field(min_length=1, max_length=64, description="The name users see")
    description: str = Field(min_length=1, max_length=200, description="One line on what it is for")
    about: str = Field(min_length=1, description="A short paragraph for its detail page")
    author: str = Field(min_length=1, description="Who makes it")
    license: str = Field(min_length=1, description="Its license, as an SPDX identifier")
    homepage_url: str = Field(description="The project's home page")
    source_url: str = Field(description="Where its source code lives")
    version: str = Field(min_length=1, description="The pinned version the recipe installs")
    icon: str = Field(description="The icon's file name under catalog/icons/")
    recipe: InstallRecipe = Field(description="How it is installed")
    launch: LaunchCommand = Field(description="How it is started")
    resources: ResourceEstimate = Field(description="What it costs")

    @field_validator("slug")
    @classmethod
    def _slug_is_a_program_name(cls, value: str) -> str:
        if SLUG_PATTERN.match(value) is None:
            raise CatalogEntryError(
                f"slug {value!r} must be 2-31 lowercase letters and digits, starting with a letter"
            )
        return value

    @field_validator("homepage_url", "source_url")
    @classmethod
    def _url_is_https(cls, value: str) -> str:
        if _URL_PATTERN.match(value) is None:
            raise CatalogEntryError(f"{value!r} must be an https URL")
        return value

    @model_validator(mode="after")
    def _version_matches_recipe(self) -> "CatalogApp":
        if self.recipe.pinned_version != self.version:
            raise CatalogEntryError(
                f"version {self.version!r} differs from the recipe's pin {self.recipe.pinned_version!r}"
            )
        return self


class AppCatalog(FrozenModel):
    """A set of apps the store offers: the shipped catalog, or that catalog with the apps the user added."""

    categories: tuple[AppCategory, ...] = Field(description="The store's filters, in display order")
    apps: tuple[CatalogApp, ...] = Field(description="Every app offered, in display order")

    @model_validator(mode="after")
    def _slugs_are_unique(self) -> "AppCatalog":
        slugs = [app.slug for app in self.apps]
        duplicates = sorted({slug for slug in slugs if slugs.count(slug) > 1})
        if duplicates:
            raise CatalogEntryError(f"duplicate slugs: {duplicates}")
        return self

    @model_validator(mode="after")
    def _tags_name_categories(self) -> "AppCatalog":
        keys = [category.key for category in self.categories]
        if len(set(keys)) != len(keys):
            raise CatalogEntryError(f"duplicate category keys: {keys}")
        for app in self.apps:
            unknown = sorted(set(app.tags) - set(keys))
            if unknown:
                raise CatalogEntryError(f"{app.slug} is tagged {unknown}, which no category declares")
        empty = [key for key in keys if not any(key in app.tags for app in self.apps)]
        if empty:
            raise CatalogEntryError(f"categories with no apps: {empty}")
        return self

    def find(self, slug: str) -> CatalogApp | None:
        for app in self.apps:
            if app.slug == slug:
                return app
        return None


def parse_app_catalog(text: str, icons_directory: Path, source: str) -> AppCatalog:
    """The catalog in ``text``; raises CatalogError when it is not valid TOML, an entry does not validate, or an
    entry's icon is missing."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CatalogError(f"the app catalog {source} is not valid TOML: {e}") from e
    try:
        catalog = AppCatalog.model_validate(document)
    except ValidationError as e:
        raise CatalogError(f"the app catalog {source} does not validate: {describe_validation_error(e)}") from e
    for app in catalog.apps:
        # The shipped catalog is held to more than an app a user adds: a category and measured figures.
        if not app.tags:
            raise CatalogError(f"the app catalog {source} gives {app.slug} no tags")
        if not app.resources.is_complete:
            raise CatalogError(f"the app catalog {source} gives {app.slug} incomplete resource figures")
        icon_path = icons_directory / app.icon
        if Path(app.icon).name != app.icon or icon_path.suffix != ".svg" or not icon_path.is_file():
            raise CatalogError(
                f"the app catalog {source} names icon {app.icon!r} for {app.slug}, which is not an SVG in {icons_directory}"
            )
    return catalog


def with_added_apps(curated: AppCatalog, added: tuple[CatalogApp, ...]) -> AppCatalog:
    """The shipped catalog followed by the apps the user added, validated as one set (so a slug cannot repeat)."""
    return AppCatalog(categories=curated.categories, apps=(*curated.apps, *added))


def load_app_catalog(path: Path = CATALOG_PATH, icons_directory: Path = ICONS_DIRECTORY) -> AppCatalog:
    """The shipped catalog, validated."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CatalogError(f"cannot read the app catalog {path}: {e}") from e
    return parse_app_catalog(text, icons_directory, str(path))


@pure
def render_argv(argv: tuple[str, ...], port: int, data_dir: Path, env_dir: Path, origin_label: str = "") -> list[str]:
    """The argv with its placeholders filled in (``origin_label`` only at launch; an init argv cannot name it)."""
    values = {
        PORT_PLACEHOLDER: str(port),
        DATA_DIR_PLACEHOLDER: str(data_dir),
        ENV_DIR_PLACEHOLDER: str(env_dir),
        ORIGIN_LABEL_PLACEHOLDER: origin_label,
    }
    return [argument.format_map(values) for argument in argv]
