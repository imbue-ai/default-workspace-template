"""The names an update retires that a script can list: commands and skills.

A workspace's own creations call the template by name: a command a ``pyproject.toml``
installs, a skill a routine runs. When a release drops or renames one, nothing in the
workspace fails until the creation next runs, so the worker's impact analysis searches the
user's creations for each retired name. This module reads the two kinds of name the tree
declares: ``[project.scripts]`` entry points and the directories under ``.agents/skills/``.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

SKILLS_DIR = ".agents/skills"
_PYPROJECT_NAME = "pyproject.toml"


@dataclass(frozen=True)
class EntryPoint:
    name: str
    target: str
    pyproject: str


class UnreadablePyprojectError(Exception):
    pass


def is_pyproject(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == _PYPROJECT_NAME


def entry_points(pyproject: str, text: str) -> list[EntryPoint]:
    """The ``[project.scripts]`` entries of one ``pyproject.toml``."""
    try:
        scripts = tomllib.loads(text).get("project", {}).get("scripts", {})
    except tomllib.TOMLDecodeError as e:
        raise UnreadablePyprojectError(f"{pyproject}: {e}") from e
    return [
        EntryPoint(name=name, target=target, pyproject=pyproject)
        for name, target in scripts.items()
        if isinstance(target, str)
    ]


def removed_entry_points(
    base: Sequence[EntryPoint], target: Sequence[EntryPoint]
) -> list[dict[str, object]]:
    """Every command the base installs that the target installs from no project.

    ``same_target`` lists the commands the target installs that run what the removed one
    ran: a rename that kept the code keeps its target.
    """
    target_names = {entry.name for entry in target}
    removed = sorted(
        (entry for entry in base if entry.name not in target_names),
        key=lambda entry: (entry.name, entry.pyproject),
    )
    return [
        {
            "name": entry.name,
            "pyproject": entry.pyproject,
            "target": entry.target,
            "same_target": sorted(
                {other.name for other in target if other.target == entry.target}
            ),
        }
        for entry in removed
    ]


def skill_of(path: str) -> str | None:
    """The skill whose directory holds ``path``, or ``None`` outside ``.agents/skills/``."""
    prefix = f"{SKILLS_DIR}/"
    if not path.startswith(prefix):
        return None
    name, separator, _ = path[len(prefix) :].partition("/")
    return name if separator else None


def retired_skills(
    base_skills: Sequence[str],
    target_skills: Sequence[str],
    renamed_files: Sequence[tuple[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    """The skills the target dropped, and the ones it renamed with their new names.

    A skill is renamed when its directory is gone and most of its files that git traced
    went to one directory the base did not have. ``renamed_files`` holds each file git
    reports as renamed, as ``(old path, new path)``.
    """
    gone = sorted(set(base_skills) - set(target_skills))
    arrived = set(target_skills) - set(base_skills)
    destinations: dict[str, Counter[str]] = {name: Counter() for name in gone}
    for old_path, new_path in renamed_files:
        old_skill, new_skill = skill_of(old_path), skill_of(new_path)
        if old_skill is None or new_skill is None:
            continue
        if old_skill in destinations and new_skill in arrived:
            destinations[old_skill][new_skill] += 1
    removed: list[str] = []
    renamed: list[dict[str, str]] = []
    for name in gone:
        if destinations[name]:
            [(successor, _)] = destinations[name].most_common(1)
            renamed.append({"from": name, "to": successor})
        else:
            removed.append(name)
    return removed, renamed
