"""What the workspace declares about its own packages: the uv workspace members and the npm
workspace packages with what each depends on, the suites that run as their own pytest root,
and what a ``uv.lock`` change upgraded and who depends on it."""

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from pydantic import Field

from app_manifest.errors import SuiteSelectionError
from app_manifest.primitives import RepoRelativePath

# The working directory of a command run from the repo root, and how uv.lock records the root
# project's own directory.
ROOT_DIRECTORY: Final[str] = "."

# The npm workspace root.
NPM_ROOT: Final[str] = "system"
NPM_ROOT_MANIFEST: Final[str] = "system/package.json"

_REQUIREMENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NAME_SEPARATOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[-_.]+")
_NAMESPACE_PACKAGE: Final[str] = "imbue"
_SOURCE_DIRECTORY: Final[str] = "src"


class PythonMember(FrozenModel):
    """A uv workspace member."""

    directory: RepoRelativePath = Field(description="The member's directory")
    name: NonEmptyStr = Field(description="Its normalized distribution name")
    dependencies: tuple[str, ...] = Field(
        description="Normalized names of everything it depends on"
    )
    module_names: tuple[str, ...] = Field(description="The top-level modules it installs")


class NpmPackage(FrozenModel):
    """A package of the npm workspace."""

    directory: RepoRelativePath = Field(description="The package's directory")
    name: NonEmptyStr = Field(description="Its package name")
    dependencies: tuple[str, ...] = Field(description="Names of everything it depends on")
    scripts: tuple[str, ...] = Field(description="The npm scripts it declares")


class LockedPackageChange(FrozenModel):
    """A package ``uv.lock`` pins differently after the change."""

    name: NonEmptyStr = Field(description="The package")
    before: tuple[str, ...] = Field(description="The versions it was locked at")
    after: tuple[str, ...] = Field(description="The versions it is locked at")


class LockfileChange(FrozenModel):
    """What a ``uv.lock`` change did, and who depends on what it upgraded."""

    added: tuple[NonEmptyStr, ...] = Field(description="Packages only the new lock has")
    upgraded: tuple[LockedPackageChange, ...] = Field(
        description="Packages locked at another version or source"
    )
    removed: tuple[NonEmptyStr, ...] = Field(description="Packages only the old lock had")
    dependent_members: tuple[RepoRelativePath, ...] = Field(
        description="Workspace members that depend on an upgraded package, directly or transitively"
    )
    is_root_dependent: bool = Field(
        description="Whether the root project depends directly on an upgraded package"
    )


@pure
def normalize_distribution_name(name: str) -> str:
    return _NAME_SEPARATOR_PATTERN.sub("-", name).lower()


@pure
def _requirement_names(requirements: Iterable[object]) -> tuple[str, ...]:
    """The distribution names a list of requirement strings names; an include-group table and
    anything else that is not a string is skipped."""
    names: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        match = _REQUIREMENT_NAME_PATTERN.match(requirement)
        if match is not None:
            names.append(normalize_distribution_name(match.group(1)))
    return tuple(names)


def _read_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SuiteSelectionError(f"cannot read {path}: {e}") from e


def _read_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SuiteSelectionError(f"cannot read {path}: {e}") from e
    if not isinstance(loaded, dict):
        raise SuiteSelectionError(f"{path} is not a JSON object")
    return loaded


def _table(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _member_module_names(
    member_directory: Path, pyproject: Mapping[str, object]
) -> tuple[str, ...]:
    """The top-level modules a member installs: its wheel's packages, with ``src/`` dropped and
    the ``imbue`` namespace expanded to the subpackages the member puts in it."""
    wheel = _table(
        _table(_table(_table(pyproject.get("tool")).get("hatch")).get("build")).get("targets")
    )
    packages = _string_list(_table(wheel.get("wheel")).get("packages"))
    if not packages:
        source = member_directory / _SOURCE_DIRECTORY
        packages = (
            [
                f"{_SOURCE_DIRECTORY}/{child.name}"
                for child in sorted(source.iterdir())
                if child.is_dir()
            ]
            if source.is_dir()
            else []
        )
    names: list[str] = []
    for package in packages:
        package_path = PurePosixPath(package)
        if package_path.name == _NAMESPACE_PACKAGE:
            namespace_directory = member_directory / package
            if namespace_directory.is_dir():
                names.extend(
                    f"{_NAMESPACE_PACKAGE}.{child.name}"
                    for child in sorted(namespace_directory.iterdir())
                    if child.is_dir() and not child.name.startswith(("_", "."))
                )
        else:
            names.append(package_path.name)
    return tuple(names)


def read_python_members(repo_root: Path) -> tuple[PythonMember, ...]:
    """The uv workspace members the root ``pyproject.toml`` declares, with what each depends on."""
    root_pyproject = _read_toml(repo_root / "pyproject.toml")
    workspace = _table(_table(_table(root_pyproject.get("tool")).get("uv")).get("workspace"))
    excluded = {repo_root / entry for entry in _string_list(workspace.get("exclude"))}
    members: list[PythonMember] = []
    for pattern in _string_list(workspace.get("members")):
        for member_directory in sorted(repo_root.glob(pattern)):
            pyproject_path = member_directory / "pyproject.toml"
            if member_directory in excluded or not pyproject_path.is_file():
                continue
            pyproject = _read_toml(pyproject_path)
            project = _table(pyproject.get("project"))
            name = project.get("name")
            if not isinstance(name, str) or not name:
                continue
            requirements: list[object] = list(_string_list(project.get("dependencies")))
            for extra in _table(project.get("optional-dependencies")).values():
                requirements.extend(_string_list(extra))
            for group in _table(pyproject.get("dependency-groups")).values():
                requirements.extend(_string_list(group))
            members.append(
                PythonMember(
                    directory=RepoRelativePath(member_directory.relative_to(repo_root).as_posix()),
                    name=NonEmptyStr(normalize_distribution_name(name)),
                    dependencies=_requirement_names(requirements),
                    module_names=_member_module_names(member_directory, pyproject),
                )
            )
    return tuple(members)


@pure
def _reverse_transitive_closure(
    edges: Mapping[str, Iterable[str]],
) -> dict[str, tuple[str, ...]]:
    """For each node, every node that reaches it through ``edges`` (node -> what it depends on)."""
    dependents: dict[str, set[str]] = defaultdict(set)
    for node, targets in edges.items():
        for target in targets:
            dependents[target].add(node)
    closure: dict[str, tuple[str, ...]] = {}
    for node in edges:
        reached: set[str] = set()
        frontier = list(dependents[node])
        while frontier:
            current = frontier.pop()
            if current in reached or current == node:
                continue
            reached.add(current)
            frontier.extend(dependents[current])
        closure[node] = tuple(sorted(reached))
    return closure


@pure
def python_consumers(members: Sequence[PythonMember]) -> dict[str, tuple[str, ...]]:
    """Each member directory's consumers: the members that depend on it, directly or transitively."""
    directory_by_name = {str(member.name): str(member.directory) for member in members}
    edges = {
        str(member.directory): [
            directory_by_name[dependency]
            for dependency in member.dependencies
            if dependency in directory_by_name
            and directory_by_name[dependency] != str(member.directory)
        ]
        for member in members
    }
    return _reverse_transitive_closure(edges)


def read_npm_packages(repo_root: Path) -> tuple[NpmPackage, ...]:
    """The npm workspace's packages, as ``system/package.json`` lists them."""
    root_manifest_path = repo_root / NPM_ROOT_MANIFEST
    if not root_manifest_path.is_file():
        return ()
    packages: list[NpmPackage] = []
    for pattern in _string_list(_read_json(root_manifest_path).get("workspaces")):
        for package_directory in sorted((repo_root / NPM_ROOT).glob(pattern)):
            manifest_path = package_directory / "package.json"
            if not manifest_path.is_file():
                continue
            manifest = _read_json(manifest_path)
            name = manifest.get("name")
            if not isinstance(name, str) or not name:
                continue
            dependency_names = [
                *_table(manifest.get("dependencies")).keys(),
                *_table(manifest.get("devDependencies")).keys(),
            ]
            packages.append(
                NpmPackage(
                    directory=RepoRelativePath(package_directory.relative_to(repo_root).as_posix()),
                    name=NonEmptyStr(name),
                    dependencies=tuple(dependency_names),
                    scripts=tuple(_table(manifest.get("scripts")).keys()),
                )
            )
    return tuple(packages)


@pure
def npm_consumers(packages: Sequence[NpmPackage]) -> dict[str, tuple[str, ...]]:
    """Each npm package directory's consumers: the packages that depend on it, directly or transitively."""
    directory_by_name = {str(package.name): str(package.directory) for package in packages}
    edges = {
        str(package.directory): [
            directory_by_name[dependency]
            for dependency in package.dependencies
            if dependency in directory_by_name
            and directory_by_name[dependency] != str(package.directory)
        ]
        for package in packages
    }
    return _reverse_transitive_closure(edges)


def read_own_root_units(repo_root: Path, members: Sequence[PythonMember]) -> tuple[str, ...]:
    """The workspace members the root pytest configuration ignores because they run as their
    own pytest root: each has its own ``[tool.pytest.ini_options]``. An ignored directory
    that is not a member (a vendored subtree with its own tests) is nobody's suite here."""
    member_directories = {str(member.directory) for member in members}
    root_pyproject = _read_toml(repo_root / "pyproject.toml")
    pytest_options = _table(
        _table(_table(root_pyproject.get("tool")).get("pytest")).get("ini_options")
    )
    own_roots: list[str] = []
    for option in _string_list(pytest_options.get("addopts")):
        if not option.startswith("--ignore="):
            continue
        directory = option.removeprefix("--ignore=").rstrip("/")
        if directory not in member_directories:
            continue
        pyproject_path = repo_root / directory / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        if _table(_table(_read_toml(pyproject_path).get("tool")).get("pytest")).get("ini_options"):
            own_roots.append(directory)
    return tuple(sorted(own_roots))


def read_coverage_measured_units(repo_root: Path, own_root_units: Iterable[str]) -> frozenset[str]:
    """The own-root suites whose pytest configuration measures coverage (an ``addopts`` entry
    starting with ``--cov``), so a run of only part of one misses its coverage floor."""
    measured: set[str] = set()
    for unit in own_root_units:
        pytest_options = _table(
            _table(_table(_read_toml(repo_root / unit / "pyproject.toml").get("tool")).get("pytest")).get(
                "ini_options"
            )
        )
        if any(option.startswith("--cov") for option in _string_list(pytest_options.get("addopts"))):
            measured.add(unit)
    return frozenset(measured)


@pure
def _locked_dependency_names(package: Mapping[str, object]) -> set[str]:
    names: set[str] = set()
    dependencies = package.get("dependencies")
    entries: list[object] = list(dependencies) if isinstance(dependencies, list) else []
    for key in ("optional-dependencies", "dev-dependencies"):
        for group in _table(package.get(key)).values():
            entries.extend(group if isinstance(group, list) else [])
    for entry in entries:
        name = _table(entry).get("name")
        if isinstance(name, str):
            names.add(normalize_distribution_name(name))
    return names


def _parse_lock(text: str, label: str) -> list[dict[str, object]]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SuiteSelectionError(f"cannot parse the {label} uv.lock: {e}") from e
    packages = parsed.get("package", [])
    if not isinstance(packages, list):
        raise SuiteSelectionError(f"the {label} uv.lock has no [[package]] list")
    return [package for package in packages if isinstance(package, dict)]


@pure
def _locked_identities(packages: Sequence[Mapping[str, object]]) -> dict[str, set[str]]:
    """Each package name and every version-and-source it is locked at."""
    identities: dict[str, set[str]] = defaultdict(set)
    for package in packages:
        name = package.get("name")
        if not isinstance(name, str):
            continue
        version = package.get("version")
        source = json.dumps(package.get("source", {}), sort_keys=True)
        identities[normalize_distribution_name(name)].add(f"{version or ''} {source}")
    return identities


@pure
def _workspace_directory(package: Mapping[str, object]) -> str | None:
    """The directory of a workspace member the lock records, or None for a third-party package."""
    source = _table(package.get("source"))
    for key in ("editable", "virtual"):
        directory = source.get(key)
        if isinstance(directory, str):
            return directory
    return None


def classify_lockfile_change(base_lock: str, head_lock: str) -> LockfileChange:
    """What changed between two ``uv.lock`` texts, and which workspace members depend on it.

    A package only the new lock has is an addition, which selects nothing beyond the member
    that added it (its own ``pyproject.toml`` changed). A package locked at another version or
    source is an upgrade, which selects every workspace member that depends on it through the
    new lock's dependency edges; the root project counts only when it depends on it directly,
    since it depends on everything transitively. Raises SuiteSelectionError when either lock
    does not parse.
    """
    base_packages = _parse_lock(base_lock, "base")
    head_packages = _parse_lock(head_lock, "head")
    base_identities = _locked_identities(base_packages)
    head_identities = _locked_identities(head_packages)
    upgraded = tuple(
        LockedPackageChange(
            name=NonEmptyStr(name),
            before=tuple(sorted(base_identities[name])),
            after=tuple(sorted(head_identities[name])),
        )
        for name in sorted(base_identities.keys() & head_identities.keys())
        if base_identities[name] != head_identities[name]
    )
    upgraded_names = {change.name for change in upgraded}

    # Walk the new lock's edges backwards from what was upgraded.
    edges: dict[str, set[str]] = {}
    directory_by_name: dict[str, str] = {}
    for package in head_packages:
        name = package.get("name")
        if not isinstance(name, str):
            continue
        normalized = normalize_distribution_name(name)
        edges.setdefault(normalized, set()).update(_locked_dependency_names(package))
        directory = _workspace_directory(package)
        if directory is not None:
            directory_by_name[normalized] = directory
    dependents = _reverse_transitive_closure(edges)
    reached = {
        dependent
        for upgraded_name in upgraded_names
        for dependent in dependents.get(upgraded_name, ())
    }
    dependent_members = sorted(
        directory_by_name[name]
        for name in reached
        if name in directory_by_name and directory_by_name[name] != ROOT_DIRECTORY
    )
    root_names = [
        name for name, directory in directory_by_name.items() if directory == ROOT_DIRECTORY
    ]
    is_root_dependent = any(
        edges.get(root_name, set()) & upgraded_names for root_name in root_names
    )
    return LockfileChange(
        added=tuple(
            NonEmptyStr(name) for name in sorted(head_identities.keys() - base_identities.keys())
        ),
        upgraded=upgraded,
        removed=tuple(
            NonEmptyStr(name) for name in sorted(base_identities.keys() - head_identities.keys())
        ),
        dependent_members=tuple(RepoRelativePath(directory) for directory in dependent_members),
        is_root_dependent=is_root_dependent,
    )
