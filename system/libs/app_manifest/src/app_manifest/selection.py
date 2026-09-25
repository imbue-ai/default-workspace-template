"""Which test suites a set of changed paths calls for, and the commands that run them.

A test runs when its subject changed: the package, skill directory, or script it sits beside,
or something that subject consumes. Consumers come from what the workspace already declares --
``pyproject.toml`` and ``package.json`` workspace dependencies, ``uv.lock``'s dependency edges,
app manifests' ``[[references]]`` and supervisord wiring -- plus the test files that name a
changed file, the workspace modules the unpackaged scripts import, and the override file in
``system/config/`` for the consumers none of those can see. A small always-run set guards the
repo-wide invariants any edit can break. A path nothing classifies falls back to the full root
suite, so a gap in the mapping costs time rather than coverage.
"""

import ast
import json
import re
import shlex
import tomllib
from collections import defaultdict
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set
from enum import auto
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final
from typing import assert_never

import pathspec
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from app_manifest.errors import ScopeComputationError
from app_manifest.errors import SuiteSelectionError
from app_manifest.manifest import app_package_directory
from app_manifest.primitives import RepoRelativePath
from app_manifest.primitives import is_path_covered_by
from app_manifest.scope import LoadedManifest
from app_manifest.scope import find_wiring_sections
from app_manifest.scope import list_changed_files
from app_manifest.scope import list_tracked_files
from app_manifest.scope import load_app_manifests
from app_manifest.scope import match_referencing_manifests
from app_manifest.scope import read_file_at_revision
from app_manifest.workspace_graph import NPM_ROOT
from app_manifest.workspace_graph import NPM_ROOT_MANIFEST
from app_manifest.workspace_graph import ROOT_DIRECTORY
from app_manifest.workspace_graph import LockfileChange
from app_manifest.workspace_graph import NpmPackage
from app_manifest.workspace_graph import PythonMember
from app_manifest.workspace_graph import classify_lockfile_change
from app_manifest.workspace_graph import npm_consumers
from app_manifest.workspace_graph import python_consumers
from app_manifest.workspace_graph import read_coverage_measured_units
from app_manifest.workspace_graph import read_npm_packages
from app_manifest.workspace_graph import read_own_root_units
from app_manifest.workspace_graph import read_root_ignored_directories
from app_manifest.workspace_graph import read_python_members

OVERRIDES_PATH: Final[RepoRelativePath] = RepoRelativePath(
    "system/config/test_selection_overrides.toml"
)


_PACKAGE_PARENT_DIRECTORIES: Final[tuple[str, ...]] = (
    "system/libs",
    "system/services",
    "system/apps",
)
_SKILLS_DIRECTORY: Final[str] = ".agents/skills"
# Directories of standalone scripts with no package around them, where a test pairs with the
# script it covers by filename.
_FLAT_SCRIPT_DIRECTORIES: Final[tuple[str, ...]] = (
    "system/scripts",
    ".agents/shared/scripts",
)
_SCRIPT_SUFFIXES: Final[tuple[str, ...]] = (".py", ".sh")
_CONFTEST_FILENAME: Final[str] = "conftest.py"

# Root files every root-collected test reads: the pytest and workspace configuration.
_ROOT_CONFIG_FILES: Final[frozenset[str]] = frozenset({"pyproject.toml", _CONFTEST_FILENAME})
_LOCKFILE: Final[RepoRelativePath] = RepoRelativePath("uv.lock")
# ``system/*.py``: the repo-wide invariants, part of the always-run set.
_GUARD_DIRECTORY: Final[str] = "system"
_SUPERVISORD_CONF: Final[str] = "system/supervisord.conf"
_SUPERVISORD_DROPIN_DIRECTORY: Final[str] = "system/supervisord.conf.d"

# The npm workspace root, and the files there that every package's build and checks read.
_NPM_ROOT_CONFIG_FILES: Final[frozenset[str]] = frozenset(
    {
        NPM_ROOT_MANIFEST,
        "system/package-lock.json",
        "system/eslint.config.js",
        "system/tsconfig.base.json",
        "system/.prettierrc",
        # The npm root's prebuild: every bundle compiles in the assets it fetches.
        "system/scripts/fetch_mngr_assets.sh",
    }
)
_NPM_CHECK_SCRIPTS: Final[tuple[str, ...]] = ("test", "lint", "format:check")
_NPM_TYPECHECK_SCRIPT: Final[str] = "typecheck"
_NPM_BUILD_SCRIPT: Final[str] = "build"

# An own-root suite whose type check runs as its own command rather than inside pytest, so
# ty's memory peak and pytest's do not stack: the suite path, and the ratchet test to deselect.
_SPLIT_TYPE_CHECKS: Final[Mapping[str, str]] = {
    "system/apps/chat": "imbue/chat/test_ratchets.py::test_no_type_errors",
}
_ALL_MARKERS_EXPRESSION: Final[str] = ""
# A run of only some of a coverage-measured suite's tests would fail its coverage floor, which
# only the whole suite can reach.
_NO_COVERAGE_FLAG: Final[str] = "--no-cov"
# A test file that drives a browser imports the library it drives it with.
_BROWSER_TEST_LIBRARY: Final[str] = "playwright"

# Where a markdown file is agent-run prose rather than documentation: mirrors the update-self
# change classes (``.agents/skills/update-self/scripts/update_classification.py``).
_RUNTIME_PREFIXES: Final[tuple[str, ...]] = (
    ".agents/",
    "system/scripts/",
    "system/libs/",
    "system/services/",
    "system/apps/",
)
_MARKDOWN_SUFFIX: Final[str] = ".md"

# A run of characters a file path is written with; a token naming a file carries a '.' or '/'.
_PATH_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.\-/]+")
_PATH_GLOB_STYLE: Final[str] = "gitignore"
# How many of a command's reasons its comment line spells out before summarizing the rest.
_MAX_REASONS_SHOWN: Final[int] = 3


class ChangedPathClass(LowerCaseStrEnum):
    """Why a changed path selected what it did."""

    PACKAGE = auto()
    SKILL = auto()
    PAIRED_SCRIPT = auto()
    GUARD = auto()
    LOCKFILE = auto()
    FRONTEND_PACKAGE = auto()
    MANIFEST_REFERENCE = auto()
    WIRING = auto()
    NAMED_BY_TEST = auto()
    OVERRIDE = auto()
    ROOT_CONFIG = auto()
    DOCS = auto()
    UNCLASSIFIED = auto()


class SuiteKind(LowerCaseStrEnum):
    """What a selected command does."""

    FRONTEND_INSTALL = auto()
    FRONTEND_BUILD = auto()
    FRONTEND_CHECK = auto()
    ALWAYS_RUN = auto()
    FULL_ROOT = auto()
    PYTEST = auto()
    TYPE_CHECK = auto()


class UnitKind(LowerCaseStrEnum):
    """What sort of directory owns a changed path."""

    PACKAGE = auto()
    SKILL = auto()
    FLAT_SCRIPTS = auto()


class SelectionReason(FrozenModel):
    """One changed path's part in selecting a command."""

    path: RepoRelativePath = Field(description="The changed path")
    path_class: ChangedPathClass = Field(description="How that path was classified")
    detail: NonEmptyStr = Field(description="How the path reaches the command, in a few words")


class SuiteCommand(FrozenModel):
    """One command to run, from its working directory, and the changed paths that call for it."""

    kind: SuiteKind = Field(description="What the command does")
    working_directory: NonEmptyStr = Field(description="Repo-root-relative; '.' for the root")
    argv: tuple[str, ...] = Field(
        description="The command and its arguments; '-m' may take an empty expression"
    )
    reasons: tuple[SelectionReason, ...] = Field(description="Why it was selected")


class ClassifiedPath(FrozenModel):
    """A changed path and every class it fell into."""

    path: RepoRelativePath = Field(description="The changed path")
    classes: tuple[ChangedPathClass, ...] = Field(
        description="Its classes; UNCLASSIFIED alone when none"
    )


class SuiteSelection(FrozenModel):
    """The commands a set of changed paths calls for, in the order to run them."""

    commands: tuple[SuiteCommand, ...] = Field(description="What to run, in order")
    paths: tuple[ClassifiedPath, ...] = Field(description="Every changed path and its classes")
    unclassified: tuple[RepoRelativePath, ...] = Field(
        description="The paths nothing classified, which bring in the full root suite"
    )
    is_full_root: bool = Field(
        description="Whether the full root suite replaces the root-collected commands"
    )
    notes: tuple[str, ...] = Field(
        description="Anything the selection could not settle, for the reader"
    )


class OwningUnit(FrozenModel):
    """The directory whose tests own a changed path."""

    kind: UnitKind = Field(description="What sort of directory it is")
    directory: RepoRelativePath = Field(description="The directory")


class ConsumerOverride(FrozenModel):
    """A consumer the workspace declarations cannot see: paths whose change selects suites."""

    paths: tuple[NonEmptyStr, ...] = Field(
        description="gitignore-style globs of the paths it covers"
    )
    suites: tuple[RepoRelativePath, ...] = Field(
        description="Suite directories or test files to run; empty when no suite beyond the always-run set can observe the paths"
    )
    note: NonEmptyStr = Field(description="Why these paths reach these suites")


class IntegrationOverride(FrozenModel):
    """A test that drives a real installed tool, and the paths whose change it can observe."""

    test: RepoRelativePath = Field(description="The test file")
    paths: tuple[NonEmptyStr, ...] = Field(
        description="gitignore-style globs of the paths that select it"
    )
    note: NonEmptyStr = Field(description="What real tool it drives")


class SelectionOverrides(FrozenModel):
    """The hand-kept part of the mapping; a table the file leaves out is empty."""

    always_run: tuple[RepoRelativePath, ...] = Field(
        default=(), description="Cross-cutting guards run for every diff, beside system/*.py"
    )
    consumer: tuple[ConsumerOverride, ...] = Field(default=(), description="Non-import consumers")
    integration: tuple[IntegrationOverride, ...] = Field(
        default=(), description="Real-tool integration tests"
    )


class RepoLayout(FrozenModel):
    """Everything the selection reads off the tree, read once."""

    repo_root: Path = Field(description="The absolute repo root")
    tracked_files: frozenset[str] = Field(description="Every file git tracks")
    test_files: tuple[str, ...] = Field(
        description="Every tracked test file some suite collects, in path order"
    )
    test_texts: Mapping[str, str] = Field(description="Each test file's text")
    browser_test_files: frozenset[str] = Field(
        description="The own-root test files that drive a browser"
    )
    own_root_units: tuple[str, ...] = Field(
        description="Suites with their own pytest configuration"
    )
    coverage_measured_units: frozenset[str] = Field(
        description="The own-root suites whose pytest configuration measures coverage"
    )
    python_members: tuple[PythonMember, ...] = Field(description="The uv workspace members")
    npm_packages: tuple[NpmPackage, ...] = Field(description="The npm workspace packages")
    manifests: tuple[LoadedManifest, ...] = Field(description="The app manifests that load")
    wiring_owners: Mapping[str, tuple[str, ...]] = Field(
        description="Each supervisord config file and the app directories with blocks in it"
    )
    overrides: SelectionOverrides = Field(description="The override file")


class _PytestRequest(FrozenModel):
    """A suite, or some of its files, that a changed path calls for."""

    root: str = Field(description="The pytest root the command runs from")
    group: str = Field(description="The unit the files belong to; one command per group")
    test_files: tuple[str, ...] | None = Field(
        description="The files to run; None for the group's whole suite"
    )
    is_browser_included: bool = Field(
        description="Whether a whole own-root suite includes its browser tests"
    )
    reason: SelectionReason = Field(description="Why")


class _FrontendRequest(FrozenModel):
    """An npm package whose checks a changed path calls for."""

    package_directory: str = Field(description="The npm package")
    reason: SelectionReason = Field(description="Why")


class _PathOutcome(FrozenModel):
    """What one changed path selects, or one part of it; the empty outcome selects nothing."""

    classes: tuple[ChangedPathClass, ...] = Field(default=(), description="Its classes")
    pytest_requests: tuple[_PytestRequest, ...] = Field(
        default=(), description="The pytest runs it calls for"
    )
    frontend_requests: tuple[_FrontendRequest, ...] = Field(
        default=(), description="The npm checks it calls for"
    )
    is_full_root: bool = Field(
        default=False, description="Whether it brings in the full root suite"
    )


def load_overrides(path: Path) -> SelectionOverrides:
    """The override file, validated; a missing or malformed file raises SuiteSelectionError."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SuiteSelectionError(f"cannot read {path}: {e}") from e
    try:
        return SelectionOverrides.model_validate(raw)
    except ValidationError as e:
        raise SuiteSelectionError(f"{path} is not a valid override file: {e}") from e


@pure
def is_test_file_name(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith("_test.py") or (name.startswith("test_") and name.endswith(".py"))


@pure
def is_docs_path(path: str) -> bool:
    """Whether a path is documentation, which selects no tests: a README or a changelog entry
    anywhere, and any other markdown outside the directories where markdown is agent-run prose.
    The always-run prose ratchets read some of it (AGENTS.md, docs/), so a documentation-only
    change they would fail is caught by the next run of them, not by its own selection."""
    pure_path = PurePosixPath(path)
    if pure_path.name == "README.md" or (
        pure_path.parent.name == "changelog" and pure_path.suffix == _MARKDOWN_SUFFIX
    ):
        return True
    if path.startswith(_RUNTIME_PREFIXES):
        return False
    return pure_path.suffix == _MARKDOWN_SUFFIX


@pure
def find_owning_unit(path: str) -> OwningUnit | None:
    """The package, skill, or flat script directory a path belongs to, or None when it has none."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 4 and "/".join(parts[:2]) in _PACKAGE_PARENT_DIRECTORIES:
        return OwningUnit(kind=UnitKind.PACKAGE, directory=RepoRelativePath("/".join(parts[:3])))
    if len(parts) >= 4 and "/".join(parts[:2]) == _SKILLS_DIRECTORY:
        return OwningUnit(kind=UnitKind.SKILL, directory=RepoRelativePath("/".join(parts[:3])))
    for directory in _FLAT_SCRIPT_DIRECTORIES:
        if path.startswith(f"{directory}/"):
            return OwningUnit(kind=UnitKind.FLAT_SCRIPTS, directory=RepoRelativePath(directory))
    return None


@pure
def find_paired_tests(path: str, flat_directory: str, test_files: Iterable[str]) -> tuple[str, ...]:
    """The tests a flat script directory pairs with a path by filename.

    ``<stem>.py``, ``<stem>.sh``, and anything under ``<stem>/`` pair with ``<stem>_test.py``
    and ``test_<stem>*.py``, beside the script or under ``<stem>/``. A test file pairs with
    itself.
    """
    relative_parts = PurePosixPath(path).relative_to(flat_directory).parts
    first = relative_parts[0]
    if is_test_file_name(path):
        return (path,)
    stem = first
    if len(relative_parts) == 1:
        for suffix in _SCRIPT_SUFFIXES:
            if first.endswith(suffix):
                stem = first.removesuffix(suffix)
    paired: list[str] = []
    for test_file in test_files:
        test_path = PurePosixPath(test_file)
        parent = test_path.parent.as_posix()
        if parent not in (flat_directory, f"{flat_directory}/{stem}"):
            continue
        name = test_path.name
        if name == f"{stem}_test.py" or (name.startswith(f"test_{stem}") and name.endswith(".py")):
            paired.append(test_file)
    return tuple(sorted(paired))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise SuiteSelectionError(f"cannot read {path}: {e}") from e


@pure
def _drives_a_browser(test_text: str) -> bool:
    return any(
        module.split(".")[0] == _BROWSER_TEST_LIBRARY
        for module in _imported_modules_in_text(test_text)
    )


def _wiring_owners(
    repo_root: Path, manifests: Sequence[LoadedManifest]
) -> dict[str, tuple[str, ...]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for loaded in manifests:
        package_directory = app_package_directory(repo_root, loaded.manifest_path)
        if package_directory is None:
            continue
        try:
            sections = find_wiring_sections(repo_root, loaded.manifest)
        except ScopeComputationError as e:
            logger.warning("Skipping the wiring of {}: {}", loaded.manifest_path, e)
            continue
        for section in sections:
            owners[section.path].add(package_directory.rstrip("/"))
    return {path: tuple(sorted(directories)) for path, directories in owners.items()}


def load_repo_layout(repo_root: Path) -> RepoLayout:
    """Read everything the selection needs off the tree at ``repo_root``."""
    repo_root = repo_root.resolve()
    tracked_files = frozenset(str(path) for path in list_tracked_files(repo_root))
    python_members = read_python_members(repo_root)
    own_root_units = read_own_root_units(repo_root, python_members)
    # No suite collects the tests of a directory the root run ignores and that is not an own
    # root, and naming one to the root run would override its --ignore.
    uncollected_directories = [
        directory
        for directory in read_root_ignored_directories(repo_root)
        if directory not in own_root_units
    ]
    test_files = tuple(
        sorted(
            path
            for path in tracked_files
            if is_test_file_name(path)
            and not any(is_path_covered_by(directory, path) for directory in uncollected_directories)
        )
    )
    test_texts = {
        path: _read_text(repo_root / path) for path in test_files if (repo_root / path).is_file()
    }
    manifests = load_app_manifests(repo_root)
    return RepoLayout(
        repo_root=repo_root,
        tracked_files=tracked_files,
        test_files=test_files,
        test_texts=test_texts,
        browser_test_files=frozenset(
            path
            for path, text in test_texts.items()
            if any(is_path_covered_by(unit, path) for unit in own_root_units)
            and _drives_a_browser(text)
        ),
        own_root_units=own_root_units,
        coverage_measured_units=read_coverage_measured_units(repo_root, own_root_units),
        python_members=python_members,
        npm_packages=read_npm_packages(repo_root),
        manifests=manifests,
        wiring_owners=_wiring_owners(repo_root, manifests),
        overrides=load_overrides(repo_root / OVERRIDES_PATH),
    )


@pure
def build_name_index(test_texts: Mapping[str, str]) -> dict[str, frozenset[str]]:
    """Every file-like token the test files write or import, and the test files that do.

    A token is indexed whole and by each of its trailing path components, so a test that
    writes ``system/scripts/layout.py`` is found by ``layout.py`` too; an imported module
    ``a.b`` counts as writing ``a/b.py``.
    """
    index: dict[str, set[str]] = defaultdict(set)
    for test_file, text in test_texts.items():
        written = {
            token.removeprefix("./").rstrip("./") for token in _PATH_TOKEN_PATTERN.findall(text)
        }
        imported = {f"{module.replace('.', '/')}.py" for module in _imported_modules_in_text(text)}
        for token in written | imported:
            if "." not in token and "/" not in token:
                continue
            parts = token.split("/")
            for start in range(len(parts)):
                suffix = "/".join(parts[start:])
                if suffix:
                    index[suffix].add(test_file)
    return {token: frozenset(files) for token, files in index.items()}


@pure
def unique_path_suffixes(tracked_files: Iterable[str]) -> frozenset[str]:
    """The trailing path components (a basename, a parent and a basename, ...) exactly one
    tracked file ends with, so a test naming one of them names that file."""
    counts: dict[str, int] = defaultdict(int)
    for path in tracked_files:
        parts = path.split("/")
        for start in range(1, len(parts)):
            counts["/".join(parts[start:])] += 1
    return frozenset(suffix for suffix, count in counts.items() if count == 1)


@pure
def naming_tokens(path: str, unique_suffixes: Set[str]) -> tuple[str, ...]:
    """What a test would write to name ``path``: the path itself, and its shortest trailing
    components that no other tracked file shares."""
    parts = path.split("/")
    for start in range(len(parts) - 1, 0, -1):
        suffix = "/".join(parts[start:])
        if suffix in unique_suffixes:
            return (path, suffix)
    return (path,)


def _imported_modules(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("Not reading the imports of {}, which cannot be read: {}", path, e)
        return set()
    return _imported_modules_in_text(text)


@pure
def _imported_modules_in_text(text: str) -> set[str]:
    """The absolute imports a Python source names; none when it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            modules.add(node.module)
    return modules


def build_import_index(layout: RepoLayout) -> dict[str, tuple[str, ...]]:
    """Each member directory, and the unpackaged Python files (skills, flat scripts, repo-root
    guards) that import one of its modules -- consumers no ``pyproject.toml`` declares."""
    member_by_module = {
        module: member.directory
        for member in layout.python_members
        for module in member.module_names
    }
    member_directories = tuple(member.directory for member in layout.python_members)
    collected_tests = frozenset(layout.test_files)
    importers: dict[str, set[str]] = defaultdict(set)
    for path in sorted(layout.tracked_files):
        if (
            not path.endswith(".py")
            or any(is_path_covered_by(directory, path) for directory in member_directories)
            or (is_test_file_name(path) and path not in collected_tests)
        ):
            continue
        for module in _imported_modules(layout.repo_root / path):
            parts = module.split(".")
            for length in (2, 1):
                member_directory = member_by_module.get(".".join(parts[:length]))
                if member_directory is not None:
                    importers[member_directory].add(path)
                    break
    return {directory: tuple(sorted(paths)) for directory, paths in importers.items()}


def _own_root_for(layout: RepoLayout, path: str) -> str:
    for unit in layout.own_root_units:
        if is_path_covered_by(unit, path):
            return unit
    return ROOT_DIRECTORY


def _unit_has_tests(layout: RepoLayout, directory: str) -> bool:
    return any(is_path_covered_by(directory, test_file) for test_file in layout.test_files)


def _invocation_group(layout: RepoLayout, test_file: str) -> str:
    own_root = _own_root_for(layout, test_file)
    if own_root != ROOT_DIRECTORY:
        return own_root
    unit = find_owning_unit(test_file)
    if unit is not None:
        return unit.directory
    return PurePosixPath(test_file).parent.as_posix()


def _file_request(
    layout: RepoLayout, test_file: str, reason: SelectionReason
) -> _PytestRequest | None:
    if not (layout.repo_root / test_file).is_file():
        return None
    return _PytestRequest(
        root=_own_root_for(layout, test_file),
        group=_invocation_group(layout, test_file),
        test_files=(test_file,),
        is_browser_included=True,
        reason=reason,
    )


def _whole_request(
    layout: RepoLayout,
    directory: str,
    is_browser_included: bool,
    reason: SelectionReason,
) -> _PytestRequest | None:
    if not (layout.repo_root / directory).is_dir() or not _unit_has_tests(layout, directory):
        return None
    own_root = _own_root_for(layout, directory)
    return _PytestRequest(
        root=own_root,
        group=own_root if own_root != ROOT_DIRECTORY else directory,
        test_files=None,
        is_browser_included=is_browser_included and own_root != ROOT_DIRECTORY,
        reason=reason,
    )


def _suite_request(
    layout: RepoLayout, suite: str, reason: SelectionReason
) -> _PytestRequest | None:
    """The request for a suite the override file names: a test file, or a suite directory."""
    if (layout.repo_root / suite).is_file():
        return _file_request(layout, suite, reason)
    if (layout.repo_root / suite).is_dir():
        return _whole_request(layout, suite, is_browser_included=False, reason=reason)
    raise SuiteSelectionError(
        f"{OVERRIDES_PATH} names {suite!r}, which does not exist; fix the entry that names it"
    )


def _own_unit_requests(
    layout: RepoLayout, path: str, reason: SelectionReason
) -> list[_PytestRequest]:
    """The tests a path's own unit runs for it: the whole package or skill, or the flat
    script directory's tests paired with it by filename (all of them for its conftest)."""
    unit = find_owning_unit(path)
    if unit is None:
        return (
            [request for request in [_file_request(layout, path, reason)] if request is not None]
            if is_test_file_name(path)
            else []
        )
    match unit.kind:
        case UnitKind.FLAT_SCRIPTS:
            if PurePosixPath(path).name == _CONFTEST_FILENAME:
                whole = _whole_request(
                    layout, unit.directory, is_browser_included=False, reason=reason
                )
                return [whole] if whole is not None else []
            paired = find_paired_tests(path, unit.directory, layout.test_files)
            return [
                request
                for request in (_file_request(layout, test, reason) for test in paired)
                if request is not None
            ]
        case UnitKind.PACKAGE | UnitKind.SKILL:
            whole = _whole_request(
                layout,
                unit.directory,
                is_browser_included=unit.directory in layout.own_root_units,
                reason=reason,
            )
            return [whole] if whole is not None else []
        case _ as unreachable:
            assert_never(unreachable)


def _owning_npm_package(layout: RepoLayout, path: str) -> NpmPackage | None:
    covering = [
        package for package in layout.npm_packages if is_path_covered_by(package.directory, path)
    ]
    return max(covering, key=lambda package: len(package.directory)) if covering else None


def _frontend_requests(
    layout: RepoLayout,
    consumers: Mapping[str, tuple[str, ...]],
    package_directories: Sequence[str],
    reason: SelectionReason,
) -> tuple[list[_FrontendRequest], list[_PytestRequest]]:
    """The npm checks for some packages and their consumers, and the browser tests of every
    app whose frontend is among them (not the rest of that app's suite, which cannot observe
    a frontend it does not build)."""
    selected = sorted(
        {
            *package_directories,
            *(c for d in package_directories for c in consumers.get(d, ())),
        }
    )
    frontend = [
        _FrontendRequest(package_directory=directory, reason=reason) for directory in selected
    ]
    browser: list[_PytestRequest] = []
    for directory in selected:
        own_root = _own_root_for(layout, directory)
        if own_root == ROOT_DIRECTORY:
            continue
        for test_file in sorted(layout.browser_test_files):
            if is_path_covered_by(own_root, test_file):
                request = _file_request(layout, test_file, reason)
                if request is not None:
                    browser.append(request)
    return frontend, browser


def _matches_any(globs: Sequence[str], path: str) -> bool:
    return pathspec.PathSpec.from_lines(_PATH_GLOB_STYLE, globs).match_file(path)


class _SelectionContext(FrozenModel):
    """What every per-path selection reads, computed once per selection."""

    layout: RepoLayout = Field(description="The tree")
    name_index: Mapping[str, frozenset[str]] = Field(
        description="Test files by the file names they write"
    )
    unique_suffixes: frozenset[str] = Field(description="Path suffixes that name one tracked file")
    import_index: Mapping[str, tuple[str, ...]] = Field(
        description="Unpackaged importers by member"
    )
    python_consumers: Mapping[str, tuple[str, ...]] = Field(description="Each member's consumers")
    npm_consumers: Mapping[str, tuple[str, ...]] = Field(description="Each npm package's consumers")
    lockfile: LockfileChange | None = Field(
        description="The lockfile change, when it could be read"
    )


def _reason(path: str, path_class: ChangedPathClass, detail: str) -> SelectionReason:
    return SelectionReason(
        path=RepoRelativePath(path), path_class=path_class, detail=NonEmptyStr(detail)
    )


def _present(requests: Iterable[_PytestRequest | None]) -> tuple[_PytestRequest, ...]:
    return tuple(request for request in requests if request is not None)


def _select_for_root_config(context: _SelectionContext, path: str) -> _PathOutcome:
    if path not in _ROOT_CONFIG_FILES:
        return _PathOutcome()
    return _PathOutcome(classes=(ChangedPathClass.ROOT_CONFIG,), is_full_root=True)


def _select_for_lockfile(context: _SelectionContext, path: str) -> _PathOutcome:
    """The members that depend on what the lock upgraded; the full root suite when the change
    could not be read, or when the root project depends on an upgrade directly."""
    if path != _LOCKFILE:
        return _PathOutcome()
    lockfile = context.lockfile
    if lockfile is None:
        return _PathOutcome(classes=(ChangedPathClass.LOCKFILE,), is_full_root=True)
    upgraded = ", ".join(change.name for change in lockfile.upgraded)
    requests: list[_PytestRequest] = []
    for member in lockfile.dependent_members:
        reason = _reason(
            path, ChangedPathClass.LOCKFILE, f"{member} depends on upgraded {upgraded}"
        )
        requests.extend(
            _present([_whole_request(context.layout, member, is_browser_included=False, reason=reason)])
        )
        requests.extend(_importer_requests(context, path, ChangedPathClass.LOCKFILE, member))
    return _PathOutcome(
        classes=(ChangedPathClass.LOCKFILE,),
        pytest_requests=tuple(requests),
        is_full_root=lockfile.is_root_dependent,
    )


def _select_for_guard(context: _SelectionContext, path: str) -> _PathOutcome:
    is_guard = path in context.layout.overrides.always_run or (
        PurePosixPath(path).parent.as_posix() == _GUARD_DIRECTORY and path.endswith(".py")
    )
    return _PathOutcome(classes=(ChangedPathClass.GUARD,)) if is_guard else _PathOutcome()


def _select_for_frontend(context: _SelectionContext, path: str) -> _PathOutcome:
    """The npm package's checks and its consumers', and the browser tests of the apps among
    them; a change to the npm root's own configuration reaches every package."""
    layout = context.layout
    npm_package = _owning_npm_package(layout, path)
    if path in _NPM_ROOT_CONFIG_FILES:
        directories = [package.directory for package in layout.npm_packages]
    elif npm_package is not None:
        directories = [npm_package.directory]
    else:
        return _PathOutcome()
    frontend, browser = _frontend_requests(
        layout,
        context.npm_consumers,
        directories,
        _reason(path, ChangedPathClass.FRONTEND_PACKAGE, "frontend changed"),
    )
    return _PathOutcome(
        classes=(ChangedPathClass.FRONTEND_PACKAGE,),
        pytest_requests=tuple(browser),
        frontend_requests=tuple(frontend),
    )


def _select_for_owning_unit(context: _SelectionContext, path: str) -> _PathOutcome:
    """A package's own suite with its consumers', a skill's suite, or a flat script's paired tests."""
    layout = context.layout
    unit = find_owning_unit(path)
    if unit is None:
        return _PathOutcome()
    match unit.kind:
        case UnitKind.PACKAGE:
            return _PathOutcome(
                classes=(ChangedPathClass.PACKAGE,),
                pytest_requests=tuple(_select_for_package(context, path, unit)),
            )
        case UnitKind.SKILL:
            reason = _reason(path, ChangedPathClass.SKILL, f"changed in {unit.directory}")
            return _PathOutcome(
                classes=(ChangedPathClass.SKILL,),
                pytest_requests=tuple(_own_unit_requests(layout, path, reason)),
            )
        case UnitKind.FLAT_SCRIPTS:
            paired = _own_unit_requests(
                layout, path, _reason(path, ChangedPathClass.PAIRED_SCRIPT, "paired by filename")
            )
            # A deleted script leaves nothing to pair with or map; the tests that name it and
            # the always-run guards are what can observe that it is gone.
            if not paired and (layout.repo_root / path).exists():
                return _PathOutcome()
            return _PathOutcome(
                classes=(ChangedPathClass.PAIRED_SCRIPT,), pytest_requests=tuple(paired)
            )
        case _ as unreachable:
            assert_never(unreachable)


def _select_for_package(
    context: _SelectionContext, path: str, unit: OwningUnit
) -> list[_PytestRequest]:
    """The package's own suite, the suites of the members that depend on it, the tests of the
    unpackaged scripts that import it or one of those members, and, for an app, the tests of
    the directories its manifest references. A test file reaches only its own suite, since
    nothing that depends on the package runs its tests."""
    layout = context.layout
    requests = _own_unit_requests(
        layout, path, _reason(path, ChangedPathClass.PACKAGE, f"changed in {unit.directory}")
    )
    if is_test_file_name(path):
        return requests
    requests.extend(_referenced_directory_requests(layout, path, unit.directory))
    requests.extend(_importer_requests(context, path, ChangedPathClass.PACKAGE, unit.directory))
    for consumer in context.python_consumers.get(unit.directory, ()):
        reason = _reason(path, ChangedPathClass.PACKAGE, f"{consumer} depends on {unit.directory}")
        requests.extend(
            _present([_whole_request(layout, consumer, is_browser_included=False, reason=reason)])
        )
        requests.extend(_importer_requests(context, path, ChangedPathClass.PACKAGE, consumer))
    return requests


def _referenced_directory_requests(
    layout: RepoLayout, path: str, app_directory: str
) -> list[_PytestRequest]:
    """The tests beneath each directory the app's manifest references (a referenced skill
    drives the app's surface); a referenced file, and a directory with no tests, select
    nothing."""
    requests: list[_PytestRequest] = []
    for loaded in layout.manifests:
        owner = app_package_directory(layout.repo_root, loaded.manifest_path)
        if owner is None or owner.rstrip("/") != app_directory:
            continue
        for reference in loaded.manifest.references:
            directory = reference.path.rstrip("/")
            reason = _reason(
                path, ChangedPathClass.PACKAGE, f"{app_directory} references {directory}"
            )
            requests.extend(
                _present(
                    [_whole_request(layout, directory, is_browser_included=False, reason=reason)]
                )
            )
    return requests


def _importer_requests(
    context: _SelectionContext, path: str, path_class: ChangedPathClass, member: str
) -> list[_PytestRequest]:
    """The tests of the unpackaged scripts that import one of ``member``'s modules."""
    requests: list[_PytestRequest] = []
    for importer in context.import_index.get(member, ()):
        reason = _reason(path, path_class, f"{importer} imports {member}")
        requests.extend(_own_unit_requests(context.layout, importer, reason))
    return requests


def _select_for_wiring(context: _SelectionContext, path: str) -> _PathOutcome:
    """The apps whose supervisord blocks the file holds (the always-run set checks the layout)."""
    if (
        path != _SUPERVISORD_CONF
        and PurePosixPath(path).parent.as_posix() != _SUPERVISORD_DROPIN_DIRECTORY
    ):
        return _PathOutcome()
    requests = _present(
        _whole_request(
            context.layout,
            owner,
            is_browser_included=False,
            reason=_reason(path, ChangedPathClass.WIRING, f"runs {owner}"),
        )
        for owner in context.layout.wiring_owners.get(path, ())
    )
    return _PathOutcome(classes=(ChangedPathClass.WIRING,), pytest_requests=requests)


def _select_for_manifest_references(context: _SelectionContext, path: str) -> _PathOutcome:
    layout = context.layout
    matches = match_referencing_manifests(layout.manifests, RepoRelativePath(path))
    if not matches:
        return _PathOutcome()
    requests: list[_PytestRequest | None] = []
    for match in matches:
        owner = app_package_directory(layout.repo_root, match.manifest_path)
        if owner is not None:
            reason = _reason(
                path, ChangedPathClass.MANIFEST_REFERENCE, f"referenced by {match.manifest.name}"
            )
            requests.append(
                _whole_request(layout, owner.rstrip("/"), is_browser_included=False, reason=reason)
            )
    return _PathOutcome(
        classes=(ChangedPathClass.MANIFEST_REFERENCE,), pytest_requests=_present(requests)
    )


def _select_for_naming_tests(context: _SelectionContext, path: str) -> _PathOutcome:
    """The test files that write the path's name. The root configuration and the lockfile are
    read by every test, so their names select nothing here."""
    if path in _ROOT_CONFIG_FILES or path == _LOCKFILE:
        return _PathOutcome()
    tokens = naming_tokens(path, context.unique_suffixes)
    naming_tests = sorted(
        {test for token in tokens for test in context.name_index.get(token, ())} - {path}
    )
    requests = _present(
        _file_request(
            context.layout,
            test_file,
            _reason(path, ChangedPathClass.NAMED_BY_TEST, f"named by {test_file}"),
        )
        for test_file in naming_tests
    )
    if not requests:
        return _PathOutcome()
    return _PathOutcome(classes=(ChangedPathClass.NAMED_BY_TEST,), pytest_requests=requests)


def _select_for_overrides(context: _SelectionContext, path: str) -> _PathOutcome:
    layout = context.layout
    requests: list[_PytestRequest | None] = []
    is_matched = False
    for consumer in layout.overrides.consumer:
        if _matches_any(consumer.paths, path):
            is_matched = True
            reason = _reason(path, ChangedPathClass.OVERRIDE, consumer.note)
            requests.extend(_suite_request(layout, suite, reason) for suite in consumer.suites)
    for integration in layout.overrides.integration:
        if _matches_any(integration.paths, path):
            is_matched = True
            reason = _reason(path, ChangedPathClass.OVERRIDE, integration.note)
            requests.append(_suite_request(layout, integration.test, reason))
    if not is_matched:
        return _PathOutcome()
    return _PathOutcome(classes=(ChangedPathClass.OVERRIDE,), pytest_requests=_present(requests))


_PATH_SELECTORS: Final[tuple[Callable[[_SelectionContext, str], _PathOutcome], ...]] = (
    _select_for_root_config,
    _select_for_lockfile,
    _select_for_guard,
    _select_for_frontend,
    _select_for_owning_unit,
    _select_for_wiring,
    _select_for_manifest_references,
    _select_for_naming_tests,
    _select_for_overrides,
)


def _select_for_path(context: _SelectionContext, path: str) -> _PathOutcome:
    """Everything one changed (non-documentation) path calls for; a path no selector
    classifies brings in the full root suite."""
    outcomes = [selector(context, path) for selector in _PATH_SELECTORS]
    classes = tuple(
        dict.fromkeys(path_class for outcome in outcomes for path_class in outcome.classes)
    )
    if not classes:
        return _PathOutcome(classes=(ChangedPathClass.UNCLASSIFIED,), is_full_root=True)
    return _PathOutcome(
        classes=classes,
        pytest_requests=tuple(
            request for outcome in outcomes for request in outcome.pytest_requests
        ),
        frontend_requests=tuple(
            request for outcome in outcomes for request in outcome.frontend_requests
        ),
        is_full_root=any(outcome.is_full_root for outcome in outcomes),
    )


def _always_run_files(layout: RepoLayout) -> tuple[str, ...]:
    guards = [
        path
        for path in layout.test_files
        if PurePosixPath(path).parent.as_posix() == _GUARD_DIRECTORY
    ]
    for guard in layout.overrides.always_run:
        if guard not in layout.tracked_files:
            raise SuiteSelectionError(
                f"{OVERRIDES_PATH} lists {guard!r} in always_run, which git does not track"
            )
    return tuple(sorted({*guards, *layout.overrides.always_run}))


@pure
def _relative_to_root(root: str, path: str) -> str:
    return path if root == ROOT_DIRECTORY else PurePosixPath(path).relative_to(root).as_posix()


@pure
def _unique_reasons(
    requests: Iterable[_PytestRequest | _FrontendRequest],
) -> tuple[SelectionReason, ...]:
    return tuple(dict.fromkeys(request.reason for request in requests))


def _pytest_commands(
    layout: RepoLayout,
    requests: Sequence[_PytestRequest],
    always_run: Set[str],
    is_full_root: bool,
) -> list[SuiteCommand]:
    """One command per group of requests, root groups first, each own-root suite followed by
    the type check it runs apart from pytest."""
    by_group: dict[tuple[str, str], list[_PytestRequest]] = defaultdict(list)
    for request in requests:
        if request.root == ROOT_DIRECTORY and is_full_root:
            continue
        by_group[(request.root, request.group)].append(request)
    root_commands: list[SuiteCommand] = []
    own_root_commands: list[SuiteCommand] = []
    for (root, group), group_requests in sorted(
        by_group.items(), key=lambda item: (item[0][0] != ROOT_DIRECTORY, item[0])
    ):
        reasons = _unique_reasons(group_requests)
        whole = [request for request in group_requests if request.test_files is None]
        files = sorted({file for request in group_requests for file in (request.test_files or ())})
        if root == ROOT_DIRECTORY:
            targets = [group] if whole else [file for file in files if file not in always_run]
            if targets:
                root_commands.append(
                    _command(
                        SuiteKind.PYTEST,
                        root,
                        ("uv", "run", "pytest", *targets),
                        reasons,
                    )
                )
            continue
        own_root_commands.extend(
            _own_root_commands(
                root,
                whole,
                files,
                layout.browser_test_files,
                root in layout.coverage_measured_units,
                reasons,
            )
        )
    return root_commands + own_root_commands


def _own_root_commands(
    root: str,
    whole: Sequence[_PytestRequest],
    files: Sequence[str],
    browser_test_files: Set[str],
    is_coverage_measured: bool,
    reasons: tuple[SelectionReason, ...],
) -> list[SuiteCommand]:
    """The run of an own-root suite: whole (with its browser tests when the app itself
    changed), or just the named files in full, without coverage; then its split-out type
    check."""
    commands: list[SuiteCommand] = []
    partial_run = ("uv", "run", "pytest", *((_NO_COVERAGE_FLAG,) if is_coverage_measured else ()))
    split_type_check = _SPLIT_TYPE_CHECKS.get(root)
    relative_files = [_relative_to_root(root, file) for file in files]
    runs_type_check_test = split_type_check is not None and (
        bool(whole) or split_type_check.split("::")[0] in relative_files
    )
    deselect = (
        ("--deselect", split_type_check)
        if split_type_check is not None and runs_type_check_test
        else ()
    )
    if whole:
        is_browser_included = any(request.is_browser_included for request in whole)
        markers = ("-m", _ALL_MARKERS_EXPRESSION) if is_browser_included else ()
        commands.append(
            _command(
                SuiteKind.PYTEST,
                root,
                ("uv", "run", "pytest", *markers, *deselect),
                reasons,
            )
        )
        named_browser_files = [
            _relative_to_root(root, file) for file in files if file in browser_test_files
        ]
        if named_browser_files and not is_browser_included:
            # The whole run skips the browser tests, which were asked for by name.
            commands.append(
                _command(
                    SuiteKind.PYTEST,
                    root,
                    (*partial_run, "-m", _ALL_MARKERS_EXPRESSION, *named_browser_files),
                    reasons,
                )
            )
    elif relative_files:
        commands.append(
            _command(
                SuiteKind.PYTEST,
                root,
                (*partial_run, "-m", _ALL_MARKERS_EXPRESSION, *deselect, *relative_files),
                reasons,
            )
        )
    if deselect:
        commands.append(_command(SuiteKind.TYPE_CHECK, root, ("uv", "run", "ty", "check"), reasons))
    return commands


def _command(
    kind: SuiteKind,
    working_directory: str,
    argv: Sequence[str],
    reasons: tuple[SelectionReason, ...],
) -> SuiteCommand:
    return SuiteCommand(
        kind=kind,
        working_directory=NonEmptyStr(working_directory),
        argv=tuple(argv),
        reasons=reasons,
    )


def _frontend_commands(
    layout: RepoLayout,
    requests: Sequence[_FrontendRequest],
    browser_reasons: tuple[SelectionReason, ...],
) -> list[SuiteCommand]:
    """The install and build every frontend check and browser test needs, then each check
    over every selected package at once."""
    if not requests and not browser_reasons:
        return []
    reasons = _unique_reasons(requests) or browser_reasons
    commands = [
        _command(SuiteKind.FRONTEND_INSTALL, NPM_ROOT, ("npm", "ci"), reasons),
        _command(
            SuiteKind.FRONTEND_BUILD,
            NPM_ROOT,
            ("npm", "run", _NPM_BUILD_SCRIPT),
            reasons,
        ),
    ]
    package_by_directory = {str(package.directory): package for package in layout.npm_packages}
    directories = sorted({request.package_directory for request in requests})
    for script in _NPM_CHECK_SCRIPTS:
        workspaces = [
            f"--workspace={_relative_to_root(NPM_ROOT, directory)}"
            for directory in directories
            if script in package_by_directory[directory].scripts
        ]
        if workspaces:
            argv = (
                ("npm", "test", *workspaces)
                if script == "test"
                else ("npm", "run", script, *workspaces)
            )
            commands.append(_command(SuiteKind.FRONTEND_CHECK, NPM_ROOT, argv, reasons))
    # A package with no build is not type-checked by the build, so its own typecheck runs.
    unbuilt = [
        f"--workspace={_relative_to_root(NPM_ROOT, directory)}"
        for directory in directories
        if _NPM_TYPECHECK_SCRIPT in package_by_directory[directory].scripts
        and _NPM_BUILD_SCRIPT not in package_by_directory[directory].scripts
    ]
    if unbuilt:
        commands.append(
            _command(
                SuiteKind.FRONTEND_CHECK,
                NPM_ROOT,
                ("npm", "run", _NPM_TYPECHECK_SCRIPT, *unbuilt),
                reasons,
            )
        )
    return commands


def _browser_run_reasons(
    layout: RepoLayout, requests: Sequence[_PytestRequest]
) -> tuple[SelectionReason, ...]:
    """The reasons of every request that runs a browser test, which needs the bundles built
    first (the browser tests skip, rather than fail, when they are missing)."""
    runs_browser = [
        request
        for request in requests
        if (request.test_files is None and request.is_browser_included)
        or any(file in layout.browser_test_files for file in (request.test_files or ()))
    ]
    return _unique_reasons(runs_browser)


def select_tests(
    layout: RepoLayout,
    changed_paths: Sequence[str],
    lockfile_texts: tuple[str, str] | None,
) -> SuiteSelection:
    """The commands a set of changed paths calls for.

    ``lockfile_texts`` is the ``uv.lock`` before and after the change, when the caller could
    read both; a changed lockfile without them brings in the full root suite. A set made
    entirely of documentation selects nothing.
    """
    paths = sorted(dict.fromkeys(changed_paths))
    if all(is_docs_path(path) for path in paths):
        return SuiteSelection(
            commands=(),
            paths=tuple(
                ClassifiedPath(path=RepoRelativePath(path), classes=(ChangedPathClass.DOCS,))
                for path in paths
            ),
            unclassified=(),
            is_full_root=False,
            notes=(),
        )

    # Read the lockfile change, if there is one
    lockfile: LockfileChange | None = None
    notes: list[str] = []
    if _LOCKFILE in paths:
        if lockfile_texts is None:
            notes.append(
                "uv.lock changed, but there is no base to compare it against, so the full root suite runs"
            )
        else:
            try:
                lockfile = classify_lockfile_change(*lockfile_texts)
            except SuiteSelectionError as e:
                notes.append(f"{e}; the full root suite runs")
    reaches_member = lockfile is not None or any(
        is_path_covered_by(member.directory, path)
        for member in layout.python_members
        for path in paths
    )
    context = _SelectionContext(
        layout=layout,
        name_index=build_name_index(layout.test_texts),
        unique_suffixes=unique_path_suffixes(layout.tracked_files),
        import_index=build_import_index(layout) if reaches_member else {},
        python_consumers=python_consumers(layout.python_members),
        npm_consumers=npm_consumers(layout.npm_packages),
        lockfile=lockfile,
    )

    # Classify every path
    classified: list[ClassifiedPath] = []
    pytest_requests: list[_PytestRequest] = []
    frontend_requests: list[_FrontendRequest] = []
    is_full_root = False
    for path in paths:
        if is_docs_path(path):
            classified.append(
                ClassifiedPath(path=RepoRelativePath(path), classes=(ChangedPathClass.DOCS,))
            )
            continue
        outcome = _select_for_path(context, path)
        classified.append(ClassifiedPath(path=RepoRelativePath(path), classes=outcome.classes))
        pytest_requests.extend(outcome.pytest_requests)
        frontend_requests.extend(outcome.frontend_requests)
        is_full_root = is_full_root or outcome.is_full_root

    # Assemble the commands: frontends first (the browser tests need their bundles), then the
    # root-collected tests, then the own-root suites
    always_run = _always_run_files(layout)
    root_reasons = tuple(
        SelectionReason(
            path=entry.path,
            path_class=entry.classes[0],
            detail=NonEmptyStr("every change"),
        )
        for entry in classified
        if ChangedPathClass.DOCS not in entry.classes
    )
    commands = _frontend_commands(
        layout, frontend_requests, _browser_run_reasons(layout, pytest_requests)
    )
    if is_full_root:
        full_root_reasons = tuple(
            SelectionReason(
                path=entry.path,
                path_class=entry.classes[0],
                detail=NonEmptyStr("full root suite"),
            )
            for entry in classified
            if {
                ChangedPathClass.UNCLASSIFIED,
                ChangedPathClass.ROOT_CONFIG,
                ChangedPathClass.LOCKFILE,
            }
            & set(entry.classes)
        )
        commands.append(
            _command(
                SuiteKind.FULL_ROOT,
                ROOT_DIRECTORY,
                ("uv", "run", "pytest"),
                full_root_reasons,
            )
        )
    else:
        commands.append(
            _command(
                SuiteKind.ALWAYS_RUN,
                ROOT_DIRECTORY,
                ("uv", "run", "pytest", *always_run),
                root_reasons,
            )
        )
    commands.extend(_pytest_commands(layout, pytest_requests, set(always_run), is_full_root))
    return SuiteSelection(
        commands=tuple(commands),
        paths=tuple(classified),
        unclassified=tuple(
            entry.path for entry in classified if ChangedPathClass.UNCLASSIFIED in entry.classes
        ),
        is_full_root=is_full_root,
        notes=tuple(notes),
    )


def read_lockfile_texts(
    repo_root: Path, base_revision: str | None, ref_revision: str | None
) -> tuple[str, str] | None:
    """``uv.lock`` before and after a change: at ``base_revision``, and at ``ref_revision`` or,
    when that is None, in the working tree. None when there is no base to read it at; a side
    with no lockfile reads as empty."""
    if base_revision is None:
        return None
    base_text = read_file_at_revision(repo_root, base_revision, _LOCKFILE) or ""
    if ref_revision is not None:
        return base_text, read_file_at_revision(repo_root, ref_revision, _LOCKFILE) or ""
    head_path = repo_root / _LOCKFILE
    return base_text, _read_text(head_path) if head_path.is_file() else ""


def select_tests_for_diff(repo_root: Path, diff_base: str, diff_ref: str) -> SuiteSelection:
    """The selection for what ``diff_ref`` changed since it forked from ``diff_base``."""
    repo_root = repo_root.resolve()
    changed = list_changed_files(repo_root, diff_base, diff_ref)
    return select_tests(
        load_repo_layout(repo_root),
        changed.files,
        read_lockfile_texts(repo_root, changed.merge_base, changed.ref),
    )


def select_tests_for_paths(
    repo_root: Path, paths: Sequence[str], lockfile_base: str | None
) -> SuiteSelection:
    """The selection for an explicit set of changed paths, as the working tree holds them;
    ``lockfile_base`` is the revision a changed ``uv.lock`` is compared against."""
    repo_root = repo_root.resolve()
    return select_tests(
        load_repo_layout(repo_root),
        [RepoRelativePath(path.removeprefix("./")) for path in paths],
        read_lockfile_texts(repo_root, lockfile_base, None),
    )


@pure
def render_command_line(command: SuiteCommand) -> str:
    """The command as one shell line runnable from the repo root."""
    joined = shlex.join(command.argv)
    if command.working_directory == ROOT_DIRECTORY:
        return joined
    return f"(cd {shlex.quote(command.working_directory)} && {joined})"


@pure
def _render_reasons(reasons: Sequence[SelectionReason]) -> str:
    shown = [f"{reason.path} ({reason.detail})" for reason in reasons[:_MAX_REASONS_SHOWN]]
    remaining = len(reasons) - len(shown)
    return "; ".join(shown) + (f"; and {remaining} more" if remaining > 0 else "")


@pure
def render_selection(selection: SuiteSelection) -> str:
    """The selection as shell lines, each command under a comment saying why it runs."""
    if not selection.paths:
        return "# nothing changed, so nothing to run\n"
    if not selection.commands:
        return "# every changed path is documentation, so nothing to run\n"
    lines: list[str] = []
    for command in selection.commands:
        lines.append(f"# {command.kind}: {_render_reasons(command.reasons)}")
        lines.append(render_command_line(command))
    lines.extend(f"# note: {note}" for note in selection.notes)
    if selection.unclassified:
        lines.append(
            f"# unclassified -- these brought in the full root suite; add a mapping for each to {OVERRIDES_PATH}:"
        )
        lines.extend(f"#   {path}" for path in selection.unclassified)
    return "\n".join(lines) + "\n"


@pure
def render_selection_json(selection: SuiteSelection) -> str:
    return f"{json.dumps(selection.model_dump(mode='json'), indent=2)}\n"
