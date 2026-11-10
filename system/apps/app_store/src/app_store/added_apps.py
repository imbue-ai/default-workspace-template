"""Apps the user added to the store, from a package registry or a GitHub repository: what a registry request asks for,
how it is checked, and where the added apps are kept.

An added app is an ordinary catalog entry (``CatalogApp``) built from the user's request, so it goes through exactly
the install, repair, uninstall and roll-back paths a curated app does. What makes it different is where it is kept --
``added-apps.json`` in the store's own data folder, never the shipped catalog or any tracked file -- and that nobody
reviewed it.

The rules a registry request is held to, each refused in plain words (an app from GitHub is held to the catalog's own
rules instead, through its install file, ``repo_install_file.py``):

- only a plain registry package: a PEP 508 project name on PyPI or a valid npm package name (scoped allowed), at one
  exact version (looked up and pinned when left empty); never a URL, a ``git+`` link, a file path, a package file, an
  extra, or a version range;
- no shell: the start command is split into arguments once and stored as a list; its program is a bare name, run
  from the app's own environment, and its arguments may use only ``{port}`` (required) and ``{data_dir}``;
- a name of its own: the app's internal name comes from its display name by the curated catalog's slug rules, and may
  not be one the store, the workspace's apps, its programs or the reserved names already use.

A repair or reinstall installs the same pin; only an update the person confirms moves an added app to another one.
"""

import re
import shlex
import threading
from pathlib import Path
from typing import Final
from typing import Literal

from loguru import logger
from packaging.version import InvalidVersion
from packaging.version import Version
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from app_manifest.primitives import RESERVED_APP_NAMES
from app_store.catalog import AppCatalog
from app_store.catalog import CatalogApp
from app_store.catalog import DATA_DIR_PLACEHOLDER
from app_store.catalog import LOOPBACK_HOST
from app_store.catalog import MAX_SETUP_FILES
from app_store.catalog import MAX_SETUP_FILE_CHARACTERS
from app_store.catalog import PORT_PLACEHOLDER
from app_store.catalog import SLUG_PATTERN
from app_store.catalog import SetupFile
from app_store.catalog import argv_placeholders
from app_store.catalog import setup_path_problem
from app_store.catalog import with_added_apps
from app_store.errors import AddAppRefusedError
from app_store.errors import StoreStateError
from app_store.repo_install_file import RepoInstallFile
from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

ADDED_APPS_FILENAME: Final[str] = "added-apps.json"
ADDED_APPS_FORMAT: Final[int] = 1
# The icon every added app wears, from the catalog's icons.
ADDED_APP_ICON: Final[str] = "added.svg"

PackageSource = Literal["pypi", "npm"]
# Where an added app came from: a package registry, or a GitHub repository a chat worked out how to install.
AddedSource = Literal["pypi", "npm", "github"]
SOURCE_NAMES: Final[dict[str, str]] = {"pypi": "PyPI", "npm": "npm", "github": "GitHub"}

# The placeholders a user's command may use: no {env_dir} (the program is found there for them) and no
# {origin_label} (a curated app's own business).
USER_PLACEHOLDERS: Final[frozenset[str]] = frozenset({PORT_PLACEHOLDER, DATA_DIR_PLACEHOLDER})

# PEP 508's project name, case-insensitively.
_PYPI_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
# validate-npm-package-name's rules for a new package: lowercase, URL-safe, no leading dot or underscore, optional
# scope, at most 214 characters.
_NPM_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
_NPM_NAME_MAX_LENGTH: Final[int] = 214
# An exact semver version: the only kind of npm version a pin can be.
_SEMVER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
# A program is a bare name the package installs into its environment's bin folder.
_PROGRAM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_PYTHON_VERSIONS: Final[tuple[str, ...]] = ("3.10", "3.11", "3.12", "3.13")
DEFAULT_PYTHON: Final[str] = "3.12"
_URL_MARKERS: Final[tuple[str, ...]] = ("://", "git+", "git:", "github:", "gitlab:", "bitbucket:", "file:", "http:")
_PACKAGE_FILE_SUFFIXES: Final[tuple[str, ...]] = (".tar.gz", ".tgz", ".zip", ".whl", ".tar.bz2", ".tar")
_VERSION_RANGE_CHARACTERS: Final[frozenset[str]] = frozenset("<>~^*!|, ")
_LINK_REFUSAL: Final[str] = (
    "Links aren't supported here: enter a package name from {registry}. (For an app from GitHub, choose GitHub "
    "instead.)"
)


class SetupFileText(FrozenModel):
    """A settings file as the dialog sends it, not yet checked."""

    path: str = Field(description="Where it goes, relative to the app's own data folder, e.g. config.yaml")
    content: str = Field(description="Its text; {port} and {data_dir} are filled in")


class AddAppRequest(FrozenModel):
    """What the "Add app" dialog sends."""

    source: PackageSource = Field(description="Where the package comes from: PyPI (Python) or npm (Node)")
    package: str = Field(description="The package's name on that registry")
    version: str = Field(default="", description="One exact version; empty to pin the latest release")
    display_name: str = Field(default="", description="What users see; empty to use the package name")
    command: str = Field(
        default="",
        description="The program and its arguments, e.g. 'streamlit hello --server.port {port}'; empty only when "
        "asking the store to work it out",
    )
    python: str = Field(default=DEFAULT_PYTHON, description="For a Python package: the Python version")
    setup_files: tuple[SetupFileText, ...] = Field(
        default=(), description="Settings files written into the app's own folder before its first start"
    )


class CheckedRequest(FrozenModel):
    """A request that passed every rule that needs no network, normalized."""

    source: PackageSource = Field(description="Where the package comes from")
    package: str = Field(description="The package's name, as the registry spells it in a request")
    version: str = Field(description="The exact version asked for; empty to pin the latest")
    title: str = Field(description="The display name")
    slug: str = Field(description="The app's internal name, from the display name")
    program: str = Field(description="The program the command runs, a bare name")
    arguments: tuple[str, ...] = Field(description="Its arguments, split once; placeholders {port}, {data_dir}")
    python: str | None = Field(description="The Python version, for a Python package")
    setup_files: tuple[SetupFile, ...] = Field(default=(), description="Its settings files, checked")

    @property
    def command(self) -> tuple[str, ...]:
        return (self.program, *self.arguments)

    @property
    def program_template(self) -> str:
        """Where the program is found in the app's environment once installed."""
        if self.source == "pypi":
            return f"{{env_dir}}/bin/{self.program}"
        return f"{{env_dir}}/node_modules/.bin/{self.program}"

    @property
    def launch_argv(self) -> tuple[str, ...]:
        return (self.program_template, *self.arguments)


@pure
def slug_from_display_name(display_name: str) -> str:
    """The internal name a display name gives, by the curated catalog's slug rules (lowercase letters and digits,
    starting with a letter): every other character is dropped, and any leading digits."""
    letters = re.sub(r"[^a-z0-9]", "", display_name.lower())
    return letters.lstrip("0123456789")[:31]


def free_slug(title: str, taken_names: set[str], rename_hint: str) -> str:
    """The internal name ``title`` gives an added app, or AddAppRefusedError when it makes no usable name or one
    ``taken_names`` (or the reserved names) already has; ``rename_hint`` says what to change."""
    slug = slug_from_display_name(title)
    if SLUG_PATTERN.match(slug) is None:
        raise _refuse(
            f"{title!r} doesn't make a usable name: use at least two letters or digits, starting with a letter."
        )
    if slug in taken_names or slug in RESERVED_APP_NAMES:
        raise _refuse(
            f"{title!r} would be called {slug!r} inside the workspace, and something already has that name. "
            f"{rename_hint}"
        )
    return slug


def _refuse(message: str) -> AddAppRefusedError:
    return AddAppRefusedError(message)


def _check_pypi_name(name: str) -> str:
    registry = SOURCE_NAMES["pypi"]
    if any(marker in name.lower() for marker in _URL_MARKERS):
        raise _refuse(_LINK_REFUSAL.format(registry=registry))
    if name.lower().endswith(_PACKAGE_FILE_SUFFIXES):
        raise _refuse("That looks like a package file. Enter the package's name on PyPI instead.")
    if "/" in name or "\\" in name or name.startswith((".", "~")):
        raise _refuse("That looks like a file path. Enter the package's name on PyPI instead.")
    if "[" in name or "]" in name:
        raise _refuse("Extras aren't supported. Enter just the package's name, like streamlit.")
    if "@" in name:
        raise _refuse("Direct references (name @ link) aren't supported. Enter just the package's name.")
    if any(character in name for character in "<>=!~,;* "):
        raise _refuse("Enter just the package's name here, and an exact version in the version field.")
    if _PYPI_NAME_PATTERN.match(name) is None:
        raise _refuse(f"{name!r} isn't a valid Python package name.")
    return name


def _check_pypi_version(version: str) -> str:
    text = version.strip().removeprefix("==").strip()
    if text == "":
        return ""
    if any(character in text for character in _VERSION_RANGE_CHARACTERS) or "=" in text:
        raise _refuse(
            f"{version!r} is a range. Enter one exact version, like 1.40.0, or leave it empty for the latest."
        )
    try:
        parsed = Version(text)
    except InvalidVersion as e:
        raise _refuse(f"{version!r} isn't a valid Python package version.") from e
    if parsed.local is not None:
        raise _refuse(f"{version!r} is a local version, which PyPI never serves.")
    return text


def _check_npm_name(name: str) -> str:
    registry = SOURCE_NAMES["npm"]
    if any(marker in name.lower() for marker in _URL_MARKERS):
        raise _refuse(_LINK_REFUSAL.format(registry=registry))
    if name.lower().endswith(_PACKAGE_FILE_SUFFIXES):
        raise _refuse("That looks like a package file. Enter the package's name on npm instead.")
    if name.startswith((".", "/", "~", "\\")):
        raise _refuse("That looks like a file path. Enter the package's name on npm instead.")
    if name.startswith("-"):
        # npm would read it as one of its own options, not a package.
        raise _refuse(f"{name!r} can't be installed: a package name may not start with a dash.")
    if "@" in name[1:]:
        raise _refuse("Enter just the package's name here, and an exact version in the version field.")
    if len(name) > _NPM_NAME_MAX_LENGTH or _NPM_NAME_PATTERN.match(name) is None:
        raise _refuse(
            f"{name!r} isn't a valid npm package name (lowercase letters, digits, - . _ ~, optional @scope/)."
        )
    return name


def _check_npm_version(version: str) -> str:
    text = version.strip().removeprefix("=").removeprefix("v").strip()
    if text == "":
        return ""
    if _SEMVER_PATTERN.match(text) is None:
        raise _refuse(f"{version!r} isn't one exact version. Enter one like 14.1.1, or leave it empty for the latest.")
    return text


def check_command(command: str) -> tuple[str, tuple[str, ...]]:
    """A start command split into its program and arguments, or AddAppRefusedError saying which rule it breaks."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError as e:
        raise _refuse(f"The start command can't be read: {e}. Check its quotes.") from e
    if not words:
        raise _refuse("Enter a start command: the program's name, then its arguments.")
    program, arguments = words[0], tuple(words[1:])
    if _PROGRAM_PATTERN.match(program) is None:
        raise _refuse(
            f"The command must start with the program's bare name, like streamlit, not {program!r}. It runs from the "
            "app's own environment."
        )
    try:
        placeholders = argv_placeholders(arguments)
    except ValueError as e:
        raise _refuse(
            f"The start command has a stray brace ({e}). Only {{port}} and {{data_dir}} are filled in; write any other "
            "brace twice ({{ or }}), as in JSON."
        ) from e
    unknown = sorted(placeholders - USER_PLACEHOLDERS)
    if unknown:
        named = ", ".join("{" + name + "}" for name in unknown)
        raise _refuse(
            f"The start command uses {named}; only {{port}} and {{data_dir}} can be filled in. Write any other brace "
            "twice ({{ or }}), as in JSON."
        )
    if PORT_PLACEHOLDER not in placeholders:
        raise _refuse("The start command must pass the app its port as {port}, so the store can reach it.")
    if not any(LOOPBACK_HOST in argument for argument in arguments):
        raise _refuse(
            f"The start command must have the app listen on {LOOPBACK_HOST}, so it is reachable only through your "
            "workspace (for example --address 127.0.0.1)."
        )
    return program, arguments


def check_request(request: AddAppRequest, taken_names: set[str]) -> CheckedRequest:
    """``request`` normalized, or AddAppRefusedError saying which rule it breaks. ``taken_names`` holds every name an
    added app may not take."""
    package_only = check_package_request(request, taken_names)
    program, arguments = check_command(request.command)
    setup_files = check_setup_files(request.setup_files)
    return package_only.model_copy(update={"program": program, "arguments": arguments, "setup_files": setup_files})


def check_setup_files(files: tuple[SetupFileText, ...]) -> tuple[SetupFile, ...]:
    """The settings files checked, or AddAppRefusedError saying which rule one breaks: at most a few, each a plain
    path inside the app's own folder, none twice, none too long."""
    if len(files) > MAX_SETUP_FILES:
        raise _refuse(f"An app can be started with at most {MAX_SETUP_FILES} settings files.")
    paths = [file.path.strip() for file in files]
    if len(set(paths)) != len(paths):
        raise _refuse("Two settings files have the same name.")
    for path, file in zip(paths, files, strict=True):
        problem = setup_path_problem(path)
        if problem is not None:
            raise _refuse(f"Settings file {problem}")
        if len(file.content) > MAX_SETUP_FILE_CHARACTERS:
            raise _refuse(f"Settings file {path} is too long (at most {MAX_SETUP_FILE_CHARACTERS} characters).")
    return tuple(SetupFile(path=path, content=file.content) for path, file in zip(paths, files, strict=True))


def check_package_request(request: AddAppRequest, taken_names: set[str]) -> CheckedRequest:
    """``request`` normalized like ``check_request`` but for its start command, which is left empty: what the store
    needs to look the package up and install it before anyone knows how to start it."""
    package = request.package.strip()
    if package == "":
        raise _refuse("Enter the package's name.")
    if request.source == "pypi":
        package = _check_pypi_name(package)
        version = _check_pypi_version(request.version)
        if request.python not in _PYTHON_VERSIONS:
            raise _refuse(f"Python {request.python} isn't offered; pick one of {', '.join(_PYTHON_VERSIONS)}.")
        python: str | None = request.python
    else:
        package = _check_npm_name(package)
        version = _check_npm_version(request.version)
        python = None
    title = request.display_name.strip() or package.rsplit("/", 1)[-1]
    if len(title) > 64:
        raise _refuse("Keep the display name to 64 characters.")
    slug = free_slug(title, taken_names, "Choose another display name.")
    return CheckedRequest(
        source=request.source,
        package=package,
        version=version,
        title=title,
        slug=slug,
        program="",
        arguments=(),
        python=python,
    )


class AddedApp(FrozenModel):
    """One app the user added, as kept: its catalog entry, and what it was added with."""

    app: CatalogApp = Field(description="Its catalog entry, which every install path runs")
    source: AddedSource = Field(description="The registry it comes from, or github for a repository")
    package: str = Field(description="The package's name; for a repository, its owner/name")
    command: tuple[str, ...] = Field(description="The command as the user gave it, split: program, then arguments")
    package_count: int | None = Field(description="How many packages the install pulls in; null when not resolved")
    registry_url: str = Field(
        description="The package's page on its registry, at the pinned version; for a repository, the commit's page"
    )
    added_at: str = Field(description="When it was added, ISO 8601 UTC")
    repo_file: RepoInstallFile | None = Field(
        default=None, description="For a repository: the install file it was added from, verbatim"
    )
    chat_id: str = Field(default="", description="For a repository: the chat that worked out how to install it")


class _AddedAppsDocument(FrozenModel):
    format: int = Field(description="The file's format")
    apps: list[object] = Field(default_factory=list, description="The entries, each validated on its own")


class AddedAppStore(MutableModel):
    """The added apps, kept in ``added-apps.json`` (written atomically, with a format number). An entry that does
    not validate is skipped with a warning, so a bad entry never takes the store down. Thread-safe."""

    path: Path = Field(frozen=True, description="The added-apps.json file")

    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    def read(self) -> tuple[AddedApp, ...]:
        with self._lock:
            return tuple(self._load())

    def find(self, slug: str) -> AddedApp | None:
        return next((added for added in self.read() if added.app.slug == slug), None)

    def add(self, added: AddedApp) -> None:
        with self._lock:
            current = [entry for entry in self._load() if entry.app.slug != added.app.slug]
            self._save([*current, added])

    def remove(self, slug: str) -> None:
        with self._lock:
            self._save([entry for entry in self._load() if entry.app.slug != slug])

    def _load(self) -> list[AddedApp]:
        raw = read_json_object(self.path)
        if raw is None:
            return []
        try:
            document = _AddedAppsDocument.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored the added apps in {}: {}", self.path, e)
            return []
        if document.format != ADDED_APPS_FORMAT:
            logger.warning(
                "Ignored the added apps in {}: format {} is not {}", self.path, document.format, ADDED_APPS_FORMAT
            )
            return []
        entries: list[AddedApp] = []
        for index, item in enumerate(document.apps):
            try:
                entries.append(AddedApp.model_validate(item))
            except ValidationError as e:
                logger.warning("Skipped added app {} in {}: {}", index, self.path, e.errors()[0]["msg"])
        return entries

    def _save(self, entries: list[AddedApp]) -> None:
        document = {"format": ADDED_APPS_FORMAT, "apps": [entry.model_dump(mode="json") for entry in entries]}
        try:
            write_json_atomic(self.path, document)
        except StateFileError as e:
            raise StoreStateError(f"cannot save the added apps: {e}") from e


def offered_catalog(curated: AppCatalog, entries: tuple[AddedApp, ...]) -> AppCatalog:
    """Every app the store offers: the shipped catalog, then the apps the user added. An added app whose name a
    shipped one now has (the shipped catalog grew), or an earlier added app already has (a hand-edited file), is left
    out with a warning, so one clash never takes the store or the launch of its other apps down."""
    seen = {app.slug for app in curated.apps}
    added: list[CatalogApp] = []
    for entry in entries:
        if entry.app.slug in seen:
            logger.warning("Left out added app {}: another app in the store already has that name", entry.app.slug)
            continue
        seen.add(entry.app.slug)
        added.append(entry.app)
    return with_added_apps(curated, tuple(added))


def taken_app_names(catalog_slugs: set[str], registry_names: set[str], dropin_dir: Path) -> set[str]:
    """Every name an added app may not take: the store's apps (curated and added), every app the workspace has
    registered, and every supervisord program it runs."""
    programs = {path.stem for path in dropin_dir.glob("*.conf")} if dropin_dir.is_dir() else set()
    return catalog_slugs | registry_names | programs
