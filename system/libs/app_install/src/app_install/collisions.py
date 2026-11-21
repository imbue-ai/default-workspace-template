"""What an app would collide with in the workspace it is being added to.

An app is refused, before anything is changed, when it would take a name something else already
holds: the app's own name (or another app's name with a hyphenated suffix, the names of that app's
background programs), a supervisord program, a console script, a skill, a routine, or an
environment unit. Each is one namespace across the whole workspace, so a second holder would
silently replace the first (a registry row, a program) or be shadowed by it (a skill, a script).
"""

import re
import tomllib
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from loguru import logger

from app_install.derived import app_skill_names
from app_install.derived import existing_derived_paths
from app_install.derived import program_names
from app_install.paths import APP_ENVIRONMENT_UNITS_DIRNAME
from app_install.paths import APP_ROUTINES_DIRNAME
from app_install.paths import APPS_DIRECTORY
from app_install.paths import ENVIRONMENT_UNITS_DIRECTORY
from app_install.paths import MANIFEST_FILENAME
from app_install.paths import PYPROJECT_FILENAME
from app_install.paths import ROUTINES_DIRECTORY
from app_install.paths import SKILLS_DIRECTORY
from app_install.paths import app_directory
from app_install.paths import supervisord_conf_files
from app_manifest.manifest import AppManifest

_PROGRAM_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[(?:program|eventlistener):([^\]]+)\]", re.MULTILINE)
_ROUTINE_SUFFIX: Final[str] = ".toml"
_ENVIRONMENT_UNIT_SUFFIX: Final[str] = ".sh"


def _read_toml_or_empty(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        # Another app's unreadable file is that app's problem, reported by the checks on it; it names nothing here.
        logger.debug("Reading no names from {}, which cannot be read: {}", path, e)
        return {}


def _console_scripts(package_directory: Path) -> set[str]:
    project = _read_toml_or_empty(package_directory / PYPROJECT_FILENAME).get("project", {})
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    return {name for name in scripts if isinstance(name, str)} if isinstance(scripts, dict) else set()


def _app_directories(repo_root: Path) -> list[Path]:
    apps_directory = repo_root / APPS_DIRECTORY
    if not apps_directory.is_dir():
        return []
    return [path for path in sorted(apps_directory.iterdir()) if path.is_dir()]


def _other_app_directories(repo_root: Path, package: str) -> list[Path]:
    return [path for path in _app_directories(repo_root) if path.name != package]


def workspace_app_names(repo_root: Path) -> frozenset[str]:
    """The name of every app in the workspace, as its manifest gives it: the apps the workspace ships, which
    have no record, as well as the ones built or installed in it."""
    names = (
        _read_toml_or_empty(directory / MANIFEST_FILENAME).get("name") for directory in _app_directories(repo_root)
    )
    return frozenset(name for name in names if isinstance(name, str))


def _names_in(directory: Path, suffix: str) -> set[str]:
    if not directory.is_dir():
        return set()
    return {path.name for path in directory.iterdir() if path.is_file() and path.name.endswith(suffix)}


def _declared_programs_outside(repo_root: Path, own_derived_paths: set[str]) -> dict[str, str]:
    """Every program and event listener the workspace declares in a file not generated for this app, by name,
    with the file that declares it: supervisord holds both kinds in one namespace."""
    declared: dict[str, str] = {}
    for path in supervisord_conf_files(repo_root):
        relative_path = path.relative_to(repo_root).as_posix()
        if relative_path in own_derived_paths:
            continue
        for match in _PROGRAM_SECTION_PATTERN.finditer(path.read_text(encoding="utf-8")):
            declared[match.group(1)] = relative_path
    return declared


def find_skill_collisions(repo_root: Path, package: str, own_derived_paths: AbstractSet[str]) -> tuple[str, ...]:
    """The skills the app carries whose entry in the skills directory something else holds: a skill of the
    workspace's own, or the link to another app's skill. ``own_derived_paths`` are the entries that are the
    app's already."""
    problems: list[str] = []
    for skill in app_skill_names(repo_root, package):
        link = repo_root / SKILLS_DIRECTORY / skill
        relative_link = link.relative_to(repo_root).as_posix()
        if (link.exists() or link.is_symlink()) and relative_link not in own_derived_paths:
            problems.append(f"the skill {skill!r} already exists at {relative_link}")
    return tuple(problems)


def find_collisions(
    repo_root: Path, package: str, manifest: AppManifest, own_paths: AbstractSet[str]
) -> tuple[str, ...]:
    """Everything already in the workspace that the app at ``system/apps/<package>`` would collide with; empty
    when it can be added.

    ``own_paths`` are repo-relative paths that are the app's although nothing generated them: the program file an
    app built before program files were generated was given by hand.
    """
    problems: list[str] = []
    own_directory = app_directory(repo_root, package)
    name = str(manifest.name)

    for other in _other_app_directories(repo_root, package):
        other_name = _read_toml_or_empty(other / MANIFEST_FILENAME).get("name")
        if other_name == name:
            problems.append(f"the name {name!r} is already the app at {other.relative_to(repo_root).as_posix()}")
        elif isinstance(other_name, str) and (name.startswith(f"{other_name}-") or other_name.startswith(f"{name}-")):
            # An app's background programs are named <app name>-<role>, and the footprint finds them by that
            # prefix, so one of the two apps would be taken for a program of the other.
            problems.append(
                f"the name {name!r} and the name {other_name!r} of the app at "
                f"{other.relative_to(repo_root).as_posix()} differ only by a hyphenated suffix, which is how an "
                "app's background programs are named"
            )

    own_derived_paths = set(existing_derived_paths(repo_root, package)) | set(own_paths)
    declared_programs = _declared_programs_outside(repo_root, own_derived_paths)
    for program in program_names(manifest):
        if program in declared_programs:
            problems.append(f"the program {program!r} is already declared in {declared_programs[program]}")

    own_scripts = _console_scripts(own_directory)
    for other in _other_app_directories(repo_root, package):
        for script in sorted(own_scripts & _console_scripts(other)):
            problems.append(
                f"the console script {script!r} is already declared by {other.relative_to(repo_root).as_posix()}"
            )

    problems.extend(find_skill_collisions(repo_root, package, own_derived_paths))

    own_routines = _names_in(own_directory / APP_ROUTINES_DIRNAME, _ROUTINE_SUFFIX)
    own_units = _names_in(own_directory / APP_ENVIRONMENT_UNITS_DIRNAME, _ENVIRONMENT_UNIT_SUFFIX)
    routine_holders = [repo_root / ROUTINES_DIRECTORY]
    unit_holders = [repo_root / ENVIRONMENT_UNITS_DIRECTORY]
    for other in _other_app_directories(repo_root, package):
        routine_holders.append(other / APP_ROUTINES_DIRNAME)
        unit_holders.append(other / APP_ENVIRONMENT_UNITS_DIRNAME)
    for holder in routine_holders:
        for routine in sorted(own_routines & _names_in(holder, _ROUTINE_SUFFIX)):
            problems.append(
                f"the routine {routine.removesuffix(_ROUTINE_SUFFIX)!r} already exists at "
                f"{(holder / routine).relative_to(repo_root).as_posix()}"
            )
    for holder in unit_holders:
        for unit in sorted(own_units & _names_in(holder, _ENVIRONMENT_UNIT_SUFFIX)):
            problems.append(
                f"the environment unit {unit!r} already exists at {(holder / unit).relative_to(repo_root).as_posix()}"
            )
    return tuple(problems)
