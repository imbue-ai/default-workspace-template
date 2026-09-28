"""Which test suites a set of changed paths calls for, and the commands that run them.

Selection reads only what the workspace declares: a changed path inside a package or skill
runs that unit's suite, plus the suites of the workspace members that depend on the package
(``pyproject.toml``), the npm packages that depend on it (``package.json``), and the apps whose
manifests reference it (``[[references]]``). A supervisord block runs the app whose program
it holds and the check that every block names its OOM band. ``uv.lock`` selects the members that depend on what it upgraded. A small always-run
set guards the repo-wide invariants any edit can break; agent prose outside every skill selects
only that set and the apps whose manifests reference it. Any other path belongs to no declared
unit, so it runs the full root suite.
"""

import json
import shlex
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set
from enum import auto
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from app_manifest.errors import ScopeComputationError
from app_manifest.errors import SuiteSelectionError
from app_manifest.manifest import app_package_directory
from app_manifest.primitives import RepoRelativePath
from app_manifest.primitives import is_path_covered_by
from app_manifest.scope import LoadedManifest
from app_manifest.scope import find_wiring_sections
from app_manifest.scope import list_changed_files
from app_manifest.scope import list_tracked_files
from app_manifest.scope import list_uncommitted_paths
from app_manifest.scope import load_app_manifests
from app_manifest.scope import match_referencing_manifests
from app_manifest.scope import read_file_at_revision
from app_manifest.scope import resolve_commit
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
from app_manifest.workspace_graph import read_python_members
from app_manifest.workspace_graph import read_root_ignored_directories

_PACKAGE_PARENT_DIRECTORIES: Final[tuple[str, ...]] = (
    "system/libs",
    "system/services",
    "system/apps",
)
_SKILLS_DIRECTORY: Final[str] = ".agents/skills"
# Agent prose, which only the always-run prose checks and the apps referencing it read.
_AGENT_PROSE_DIRECTORY: Final[str] = ".agents"

# Root files every root-collected test reads: the pytest and workspace configuration.
_ROOT_CONFIG_FILES: Final[frozenset[str]] = frozenset({"pyproject.toml", "conftest.py"})
_LOCKFILE: Final[RepoRelativePath] = RepoRelativePath("uv.lock")
# ``system/*.py``: the repo-wide invariants, part of the always-run set.
_GUARD_DIRECTORY: Final[str] = "system"
_SUPERVISORD_CONF: Final[str] = "system/supervisord.conf"
_SUPERVISORD_DROPIN_DIRECTORY: Final[str] = "system/supervisord.conf.d"
# The checks outside the always-run set that read every real supervisord program block.
_WIRING_GUARDS: Final[tuple[str, ...]] = (
    "system/services/oom_priority/bin/oom_tag_service_test.py",
)
# The cross-cutting checks run beside system/*.py for every change: the system/scripts guards,
# and the checks that read every skill's prose, which a change to any one skill can break.
ALWAYS_RUN_GUARDS: Final[tuple[str, ...]] = (
    ".agents/shared/scripts/test_skill_mngr_references.py",
    ".agents/skills/launch-task/scripts/dispatch_contract_test.py",
    "system/scripts/agent_hook_wiring_test.py",
    "system/scripts/agent_guard_tool_scope_test.py",
    "system/scripts/provision_guard_test.py",
    "system/scripts/stdlib_only_scripts_test.py",
    "system/scripts/tool_env_sync_test.py",
    "system/scripts/claude_memory_settings_test.py",
)

# The npm workspace root, and the files there that every package's build and checks read.
_NPM_ROOT_CONFIG_FILES: Final[frozenset[str]] = frozenset(
    {
        NPM_ROOT_MANIFEST,
        "system/package-lock.json",
        "system/eslint.config.js",
        "system/tsconfig.base.json",
        "system/.prettierrc",
        # The npm root's prebuild and pretest, and what they source: every bundle compiles
        # in the assets they fetch.
        "system/scripts/fetch_mngr_assets.sh",
        "system/scripts/_mngr_git_auth.sh",
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
_BROWSER_TEST_MARKERS: Final[tuple[str, ...]] = ("from playwright", "import playwright")

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
# How many of a command's reasons its comment line spells out before summarizing the rest.
_MAX_REASONS_SHOWN: Final[int] = 3


class ChangedPathClass(LowerCaseStrEnum):
    """Why a changed path selected what it did."""

    PACKAGE = auto()
    SKILL = auto()
    TEST_FILE = auto()
    GUARD = auto()
    LOCKFILE = auto()
    FRONTEND_PACKAGE = auto()
    MANIFEST_REFERENCE = auto()
    WIRING = auto()
    ROOT_CONFIG = auto()
    DOCS = auto()
    UNOWNED = auto()


class SuiteKind(LowerCaseStrEnum):
    """What a selected command does."""

    FRONTEND_INSTALL = auto()
    FRONTEND_BUILD = auto()
    FRONTEND_CHECK = auto()
    ALWAYS_RUN = auto()
    FULL_ROOT = auto()
    PYTEST = auto()
    TYPE_CHECK = auto()


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
    classes: tuple[ChangedPathClass, ...] = Field(description="Its classes")


class SuiteSelection(FrozenModel):
    """The commands a set of changed paths calls for, in the order to run them."""

    commands: tuple[SuiteCommand, ...] = Field(description="What to run, in order")
    paths: tuple[ClassifiedPath, ...] = Field(description="Every changed path and its classes")
    is_full_root: bool = Field(
        description="Whether the full root suite replaces the root-collected commands"
    )
    notes: tuple[str, ...] = Field(
        description="Anything the selection could not settle, for the reader"
    )


class RepoLayout(FrozenModel):
    """Everything the selection reads off the tree, read once."""

    repo_root: Path = Field(description="The absolute repo root")
    tracked_files: frozenset[str] = Field(description="Every file git tracks")
    test_files: tuple[str, ...] = Field(
        description="Every tracked test file some suite collects, in path order"
    )
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


class _PytestRequest(FrozenModel):
    """A suite, or one of its files, that a changed path calls for."""

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
    """What one changed path selects; the empty outcome selects nothing."""

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


class _SelectionContext(FrozenModel):
    """What every per-path selection reads, computed once per selection."""

    layout: RepoLayout = Field(description="The tree")
    python_consumers: Mapping[str, tuple[str, ...]] = Field(description="Each member's consumers")
    npm_consumers: Mapping[str, tuple[str, ...]] = Field(description="Each npm package's consumers")
    lockfile: LockfileChange | None = Field(
        description="The lockfile change, when it could be read"
    )


@pure
def is_test_file_name(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith("_test.py") or (name.startswith("test_") and name.endswith(".py"))


@pure
def is_docs_path(path: str) -> bool:
    """Whether a path is documentation, which selects no tests: a README or a changelog entry
    anywhere, and any other markdown outside the directories where markdown is agent-run prose."""
    pure_path = PurePosixPath(path)
    if pure_path.name == "README.md" or (
        pure_path.parent.name == "changelog" and pure_path.suffix == _MARKDOWN_SUFFIX
    ):
        return True
    if path.startswith(_RUNTIME_PREFIXES):
        return False
    return pure_path.suffix == _MARKDOWN_SUFFIX


@pure
def find_owning_unit(path: str) -> str | None:
    """The package or skill directory a path belongs to, or None when it has neither."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 4 and "/".join(parts[:2]) in (*_PACKAGE_PARENT_DIRECTORIES, _SKILLS_DIRECTORY):
        return "/".join(parts[:3])
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise SuiteSelectionError(f"cannot read {path}: {e}") from e


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
    manifests = load_app_manifests(repo_root)
    return RepoLayout(
        repo_root=repo_root,
        tracked_files=tracked_files,
        test_files=test_files,
        browser_test_files=frozenset(
            path
            for path in test_files
            if any(is_path_covered_by(unit, path) for unit in own_root_units)
            and (repo_root / path).is_file()
            and any(marker in _read_text(repo_root / path) for marker in _BROWSER_TEST_MARKERS)
        ),
        own_root_units=own_root_units,
        coverage_measured_units=read_coverage_measured_units(repo_root, own_root_units),
        python_members=python_members,
        npm_packages=read_npm_packages(repo_root),
        manifests=manifests,
        wiring_owners=_wiring_owners(repo_root, manifests),
    )


def _own_root_for(layout: RepoLayout, path: str) -> str:
    for unit in layout.own_root_units:
        if is_path_covered_by(unit, path):
            return unit
    return ROOT_DIRECTORY


def _unit_has_tests(layout: RepoLayout, directory: str) -> bool:
    return any(is_path_covered_by(directory, test_file) for test_file in layout.test_files)


def _file_request(
    layout: RepoLayout, test_file: str, reason: SelectionReason
) -> _PytestRequest | None:
    if not (layout.repo_root / test_file).is_file():
        return None
    own_root = _own_root_for(layout, test_file)
    return _PytestRequest(
        root=own_root,
        group=own_root if own_root != ROOT_DIRECTORY else PurePosixPath(test_file).parent.as_posix(),
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


def _present(requests: Iterable[_PytestRequest | None]) -> tuple[_PytestRequest, ...]:
    return tuple(request for request in requests if request is not None)


def _reason(path: str, path_class: ChangedPathClass, detail: str) -> SelectionReason:
    return SelectionReason(
        path=RepoRelativePath(path), path_class=path_class, detail=NonEmptyStr(detail)
    )


def _owning_npm_package(layout: RepoLayout, path: str) -> NpmPackage | None:
    covering = [
        package for package in layout.npm_packages if is_path_covered_by(package.directory, path)
    ]
    return max(covering, key=lambda package: len(package.directory)) if covering else None


def _referenced_directory_requests(
    layout: RepoLayout, path: str, app_directory: str
) -> list[_PytestRequest]:
    """The tests beneath each directory the app's manifest references."""
    requests: list[_PytestRequest] = []
    for loaded in layout.manifests:
        owner = app_package_directory(layout.repo_root, loaded.manifest_path)
        if owner is None or owner.rstrip("/") != app_directory:
            continue
        for reference in loaded.manifest.references:
            directory = reference.path.rstrip("/")
            reason = _reason(path, ChangedPathClass.PACKAGE, f"{app_directory} references {directory}")
            requests.extend(
                _present([_whole_request(layout, directory, is_browser_included=False, reason=reason)])
            )
    return requests


def _select_for_unit(context: _SelectionContext, path: str, unit: str) -> list[_PytestRequest]:
    """A package's or skill's own suite; for a package's non-test file, also the suites of the
    members that depend on it and of the directories an app's manifest references."""
    layout = context.layout
    requests = list(
        _present(
            [
                _whole_request(
                    layout,
                    unit,
                    is_browser_included=unit in layout.own_root_units,
                    reason=_reason(path, ChangedPathClass.PACKAGE, f"changed in {unit}"),
                )
            ]
        )
    )
    if is_test_file_name(path) or unit.startswith(f"{_SKILLS_DIRECTORY}/"):
        return requests
    requests.extend(_referenced_directory_requests(layout, path, unit))
    # An app's frontend is built into the app's own bundle, which no Python package that
    # depends on the app loads; its npm consumers are the frontend selection's.
    if _owning_npm_package(layout, path) is not None:
        return requests
    for consumer in context.python_consumers.get(unit, ()):
        reason = _reason(path, ChangedPathClass.PACKAGE, f"{consumer} depends on {unit}")
        requests.extend(
            _present([_whole_request(layout, consumer, is_browser_included=False, reason=reason)])
        )
    return requests


def _frontend_requests(
    layout: RepoLayout,
    consumers: Mapping[str, tuple[str, ...]],
    package_directories: Sequence[str],
    reason: SelectionReason,
) -> tuple[list[_FrontendRequest], list[_PytestRequest]]:
    """The npm checks for some packages and their consumers, and the browser tests of every
    app whose frontend is among them."""
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
                browser.extend(_present([_file_request(layout, test_file, reason)]))
    return frontend, browser


def _select_for_path(context: _SelectionContext, path: str) -> _PathOutcome:
    """Everything one changed (non-documentation) path calls for."""
    layout = context.layout
    if path in _ROOT_CONFIG_FILES:
        return _PathOutcome(classes=(ChangedPathClass.ROOT_CONFIG,), is_full_root=True)
    if path == _LOCKFILE:
        return _select_for_lockfile(context, path)
    if (
        path in ALWAYS_RUN_GUARDS
        or (PurePosixPath(path).parent.as_posix() == _GUARD_DIRECTORY and path.endswith(".py"))
        or (
            path.startswith(f"{_AGENT_PROSE_DIRECTORY}/")
            and path.endswith(_MARKDOWN_SUFFIX)
            and find_owning_unit(path) is None
        )
    ):
        references = _select_for_manifest_references(layout, path)
        return _PathOutcome(
            classes=(ChangedPathClass.GUARD, *references.classes),
            pytest_requests=references.pytest_requests,
        )

    classes: list[ChangedPathClass] = []
    pytest_requests: list[_PytestRequest] = []
    frontend_requests: list[_FrontendRequest] = []
    is_owned = False

    if path == _SUPERVISORD_CONF or PurePosixPath(path).parent.as_posix() == _SUPERVISORD_DROPIN_DIRECTORY:
        # The always-run set checks the layout, and the wiring guards every block's OOM band;
        # the apps whose blocks the file holds run too.
        classes.append(ChangedPathClass.WIRING)
        pytest_requests.extend(
            _present(
                _file_request(
                    layout, guard, _reason(path, ChangedPathClass.WIRING, "checks every program block")
                )
                for guard in _WIRING_GUARDS
            )
        )
        pytest_requests.extend(
            _present(
                _whole_request(
                    layout,
                    owner,
                    is_browser_included=False,
                    reason=_reason(path, ChangedPathClass.WIRING, f"runs {owner}"),
                )
                for owner in layout.wiring_owners.get(path, ())
            )
        )
        is_owned = True

    npm_package = _owning_npm_package(layout, path)
    if path in _NPM_ROOT_CONFIG_FILES or npm_package is not None:
        directories = (
            [package.directory for package in layout.npm_packages]
            if npm_package is None
            else [npm_package.directory]
        )
        frontend, browser = _frontend_requests(
            layout,
            context.npm_consumers,
            directories,
            _reason(path, ChangedPathClass.FRONTEND_PACKAGE, "frontend changed"),
        )
        classes.append(ChangedPathClass.FRONTEND_PACKAGE)
        frontend_requests.extend(frontend)
        pytest_requests.extend(browser)
        is_owned = True

    unit = find_owning_unit(path)
    if unit is not None:
        classes.append(
            ChangedPathClass.SKILL if unit.startswith(f"{_SKILLS_DIRECTORY}/") else ChangedPathClass.PACKAGE
        )
        pytest_requests.extend(_select_for_unit(context, path, unit))
        is_owned = True
    elif path in layout.test_files:
        classes.append(ChangedPathClass.TEST_FILE)
        pytest_requests.extend(
            _present([_file_request(layout, path, _reason(path, ChangedPathClass.TEST_FILE, "changed"))])
        )
        is_owned = True

    references = _select_for_manifest_references(layout, path)
    classes.extend(references.classes)
    pytest_requests.extend(references.pytest_requests)

    if not is_owned:
        classes.append(ChangedPathClass.UNOWNED)
    return _PathOutcome(
        classes=tuple(dict.fromkeys(classes)),
        pytest_requests=tuple(pytest_requests),
        frontend_requests=tuple(frontend_requests),
        is_full_root=not is_owned,
    )


def _select_for_manifest_references(layout: RepoLayout, path: str) -> _PathOutcome:
    """The apps whose manifests reference the path."""
    classes: list[ChangedPathClass] = []
    requests: list[_PytestRequest] = []
    for match in match_referencing_manifests(layout.manifests, RepoRelativePath(path)):
        owner = app_package_directory(layout.repo_root, match.manifest_path)
        if owner is None:
            continue
        classes = [ChangedPathClass.MANIFEST_REFERENCE]
        reason = _reason(
            path, ChangedPathClass.MANIFEST_REFERENCE, f"referenced by {match.manifest.name}"
        )
        requests.extend(
            _present([_whole_request(layout, owner.rstrip("/"), is_browser_included=False, reason=reason)])
        )
    return _PathOutcome(classes=tuple(classes), pytest_requests=tuple(requests))


def _select_for_lockfile(context: _SelectionContext, path: str) -> _PathOutcome:
    """The members that depend on what the lock upgraded; the full root suite when the change
    could not be read, or when the root project depends on an upgrade directly."""
    lockfile = context.lockfile
    if lockfile is None:
        return _PathOutcome(classes=(ChangedPathClass.LOCKFILE,), is_full_root=True)
    upgraded = ", ".join(change.name for change in lockfile.upgraded)
    requests = _present(
        _whole_request(
            context.layout,
            member,
            is_browser_included=False,
            reason=_reason(path, ChangedPathClass.LOCKFILE, f"{member} depends on upgraded {upgraded}"),
        )
        for member in lockfile.dependent_members
    )
    return _PathOutcome(
        classes=(ChangedPathClass.LOCKFILE,),
        pytest_requests=requests,
        is_full_root=lockfile.is_root_dependent,
    )


def _always_run_files(layout: RepoLayout) -> tuple[str, ...]:
    guards = [
        path
        for path in layout.test_files
        if PurePosixPath(path).parent.as_posix() == _GUARD_DIRECTORY
    ]
    for guard in ALWAYS_RUN_GUARDS:
        if guard not in layout.tracked_files:
            raise SuiteSelectionError(f"the always-run guard {guard!r} is not tracked by git")
    return tuple(sorted({*guards, *ALWAYS_RUN_GUARDS}))


@pure
def _relative_to_root(root: str, path: str) -> str:
    return path if root == ROOT_DIRECTORY else PurePosixPath(path).relative_to(root).as_posix()


@pure
def _unique_reasons(
    requests: Iterable[_PytestRequest | _FrontendRequest],
) -> tuple[SelectionReason, ...]:
    return tuple(dict.fromkeys(request.reason for request in requests))


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
                    _command(SuiteKind.PYTEST, root, ("uv", "run", "pytest", *targets), reasons)
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
            _command(SuiteKind.PYTEST, root, ("uv", "run", "pytest", *markers, *deselect), reasons)
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
        _command(SuiteKind.FRONTEND_BUILD, NPM_ROOT, ("npm", "run", _NPM_BUILD_SCRIPT), reasons),
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
                SuiteKind.FRONTEND_CHECK, NPM_ROOT, ("npm", "run", _NPM_TYPECHECK_SCRIPT, *unbuilt), reasons
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
            is_full_root=False,
            notes=(),
        )

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
    context = _SelectionContext(
        layout=layout,
        python_consumers=python_consumers(layout.python_members),
        npm_consumers=npm_consumers(layout.npm_packages),
        lockfile=lockfile,
    )

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

    # Frontends first (the browser tests need their bundles), then the root-collected tests,
    # then the own-root suites
    always_run = _always_run_files(layout)
    commands = _frontend_commands(
        layout, frontend_requests, _browser_run_reasons(layout, pytest_requests)
    )
    if is_full_root:
        full_root_reasons = tuple(
            SelectionReason(
                path=entry.path, path_class=entry.classes[-1], detail=NonEmptyStr("full root suite")
            )
            for entry in classified
            if {ChangedPathClass.UNOWNED, ChangedPathClass.ROOT_CONFIG, ChangedPathClass.LOCKFILE}
            & set(entry.classes)
        )
        commands.append(
            _command(SuiteKind.FULL_ROOT, ROOT_DIRECTORY, ("uv", "run", "pytest"), full_root_reasons)
        )
    else:
        root_reasons = tuple(
            SelectionReason(
                path=entry.path, path_class=entry.classes[0], detail=NonEmptyStr("every change")
            )
            for entry in classified
            if ChangedPathClass.DOCS not in entry.classes
        )
        commands.append(
            _command(
                SuiteKind.ALWAYS_RUN, ROOT_DIRECTORY, ("uv", "run", "pytest", *always_run), root_reasons
            )
        )
    commands.extend(_pytest_commands(layout, pytest_requests, set(always_run), is_full_root))
    return SuiteSelection(
        commands=tuple(commands),
        paths=tuple(classified),
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
    """The selection for what ``diff_ref`` changed since it forked from ``diff_base``.

    Raises SuiteSelectionError when ``diff_ref`` is the checked-out commit and the working tree
    holds changes it does not: the tests run against the working tree, so a selection read
    from commits alone would leave those changes untested.
    """
    repo_root = repo_root.resolve()
    changed = list_changed_files(repo_root, diff_base, diff_ref)
    if changed.ref == resolve_commit(repo_root, "HEAD"):
        uncommitted = list_uncommitted_paths(repo_root)
        if uncommitted:
            raise SuiteSelectionError(
                "the working tree has changes the diff does not include; commit them, or remove "
                "any that are not part of the change, then run select-tests again: "
                + ", ".join(uncommitted)
            )
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
    return "\n".join(lines) + "\n"


@pure
def render_selection_json(selection: SuiteSelection) -> str:
    return f"{json.dumps(selection.model_dump(mode='json'), indent=2)}\n"
