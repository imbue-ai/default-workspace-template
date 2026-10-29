"""Which Python packages under ``system/`` are the user's, and whether each is standalone.

The built-in libraries, services and apps are members of the root uv workspace, named
one by one in the root ``pyproject.toml``'s ``[tool.uv.workspace] members``; they share
its lock and venv. Every other directory under ``system/apps``, ``system/services`` or
``system/libs`` with a ``pyproject.toml`` is a user package, and a user package is a
standalone uv project: its own ``uv.lock`` and ``.venv``, no ``{ workspace = true }``
source, and a supervisord program (if it has one) that syncs its own project with
``--locked`` before it execs a script from its own ``.venv/bin``. That way a dependency
one of them adds never moves the versions the built-ins, or another user package, run on
(``docs/system/blueprint/app-environments/plan-app-environments.md``).

The update apply and its migration use this too, and update-self is staged and run as a
self-contained unit, so this file exists twice, byte for byte:

    system/scripts/workspace_bare_scripts/user_packages.py
    .agents/skills/update-self/python/update_self_skill/user_packages.py

``system/scripts/update_self_mirrors_sync_test.py`` fails if they differ.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple

PACKAGE_PARENTS = ("system/apps", "system/services", "system/libs")
SUPERVISORD_DROPIN_DIR = "system/supervisord.conf.d"
MIGRATION_REFERENCE = (
    ".agents/skills/update-self/references/python-packaging-migration.md"
)

_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_STEP_SEPARATOR = re.compile(r"&&|\|\||;|\n")


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def built_in_members(repo_root: Path) -> list[str]:
    """The root workspace's ``members``, as written (a glob stays a glob)."""
    workspace = _read_toml(repo_root / "pyproject.toml").get("tool", {}).get("uv", {})
    members = workspace.get("workspace", {}).get("members", [])
    return [member for member in members if isinstance(member, str)]


def _is_member(relative: str, members: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, member.rstrip("/")) for member in members)


def package_directories(repo_root: Path) -> list[str]:
    """Every ``system/{apps,services,libs}/*`` directory with a ``pyproject.toml``, repo-relative."""
    found: list[str] = []
    for parent in PACKAGE_PARENTS:
        directory = repo_root / parent
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_dir() and (child / "pyproject.toml").is_file():
                found.append(f"{parent}/{child.name}")
    return found


def user_packages(repo_root: Path) -> list[str]:
    """The packages under ``system/`` that the root ``members`` does not name, repo-relative."""
    members = built_in_members(repo_root)
    return [
        relative
        for relative in package_directories(repo_root)
        if not _is_member(relative, members)
    ]


class ProgramLine(NamedTuple):
    program: str
    command: str


def program_commands(repo_root: Path) -> list[ProgramLine]:
    """Every supervisord ``[program:*]`` command in the drop-in directory."""
    lines: list[ProgramLine] = []
    dropins = repo_root / SUPERVISORD_DROPIN_DIR
    if not dropins.is_dir():
        return lines
    for conf in sorted(dropins.glob("*.conf")):
        parser = configparser.RawConfigParser(strict=False)
        try:
            parser.read(conf)
        except configparser.Error:
            continue
        for section in parser.sections():
            if section.startswith("program:") and parser.has_option(section, "command"):
                lines.append(
                    ProgramLine(
                        section.removeprefix("program:"), parser.get(section, "command")
                    )
                )
    return lines


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def command_steps(command: str) -> list[list[str]]:
    """The words of each step a program command runs, wrappers unwrapped.

    A ``bash -c "<script>"`` anywhere in the words (after an OOM-tag prefix, say) is
    replaced by its script's steps, split on ``&&``, ``||``, ``;`` and newlines.
    """
    words = _split(command)
    for index, word in enumerate(words[:-2]):
        if PurePosixPath(word).name in _SHELLS and words[index + 1] in ("-c", "-lc"):
            steps = [words[:index]] if index else []
            for part in _STEP_SEPARATOR.split(words[index + 2]):
                part_words = _split(part.strip())
                if part_words:
                    steps.append(part_words)
            return steps
    return [words]


def console_scripts(package_dir: Path) -> list[str]:
    scripts = (
        _read_toml(package_dir / "pyproject.toml").get("project", {}).get("scripts", {})
    )
    return (
        [name for name in scripts if isinstance(name, str)]
        if isinstance(scripts, dict)
        else []
    )


def _runs_script(word: str, script: str) -> bool:
    path = PurePosixPath(word)
    return word == script or (
        path.name == script
        and path.parent.name == "bin"
        and path.parent.parent.name == ".venv"
    )


def _own_entry_point(word: str, relative: str, script: str) -> bool:
    return word.endswith(f"{relative}/.venv/bin/{script}")


def _option_value(words: Sequence[str], option: str) -> str | None:
    for index, word in enumerate(words):
        if word == option and index + 1 < len(words):
            return words[index + 1]
        if word.startswith(option + "="):
            return word.split("=", 1)[1]
    return None


def _syncs_own_project_locked(words: Sequence[str], relative: str) -> bool:
    if words[:2] != ["uv", "sync"] or "--locked" not in words:
        return False
    target = _option_value(words, "--project") or _option_value(words, "--directory")
    return target is not None and target.rstrip("/").endswith(relative)


def program_problems(command: str, relative: str, scripts: Sequence[str]) -> list[str]:
    """What keeps one program line from running a user package standalone; empty when it
    syncs the package's own project with ``--locked`` and then execs one of its scripts
    from the package's own ``.venv/bin``. ``[]`` too for a line that runs none of them."""
    steps = command_steps(command)
    for exec_index, step in enumerate(steps):
        script = next(
            (s for s in scripts if any(_runs_script(word, s) for word in step)), None
        )
        if script is None:
            continue
        problems: list[str] = []
        if not any(_own_entry_point(word, relative, script) for word in step):
            problems.append(
                f"it runs `{script}` from somewhere other than {relative}/.venv/bin"
            )
        if not any(
            _syncs_own_project_locked(earlier, relative)
            for earlier in steps[:exec_index]
        ):
            problems.append(
                f"it does not run `uv sync --locked --project {relative}` before starting it"
            )
        return problems
    return []


def standalone_problems(repo_root: Path, relative: str) -> list[str]:
    """Why the user package at ``relative`` is not standalone; empty when it is."""
    package_dir = repo_root / relative
    problems: list[str] = []
    if not (package_dir / "uv.lock").is_file():
        problems.append("it has no uv.lock of its own")
    sources = _read_toml(package_dir / "pyproject.toml").get("tool", {}).get("uv", {})
    for name, source in (sources.get("sources") or {}).items():
        if isinstance(source, dict) and source.get("workspace") is True:
            problems.append(f"its source for `{name}` is `{{ workspace = true }}`")
    scripts = console_scripts(package_dir)
    for line in program_commands(repo_root):
        for problem in program_problems(line.command, relative, scripts):
            problems.append(f"the `{line.program}` program: {problem}")
    return problems


def lock_check(repo_root: Path, relative: str) -> str | None:
    """``uv lock --check`` for one user package: ``None`` when its lock is current, else why not."""
    result = subprocess.run(
        ["uv", "lock", "--check", "--project", relative],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"


def sync_all(repo_root: Path) -> list[str]:
    """``uv sync --locked`` every user package; returns the ones whose sync failed.

    Each failure is reported and the rest carry on: a broken user package stops only
    itself. Their venv links must exist first (``venv_links.py ensure``).
    """
    failed: list[str] = []
    for relative in user_packages(repo_root):
        result = subprocess.run(
            ["uv", "sync", "--locked", "--project", relative],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            failed.append(relative)
            sys.stderr.write(
                f"warning: `uv sync --locked --project {relative}` failed "
                f"(exit {result.returncode}); its program will fail to start until it is "
                f"fixed:\n{(result.stderr or result.stdout).strip()}\n"
            )
    return failed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the user packages under system/ and check they are standalone."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "list", help="print each user package's directory, repo-relative"
    )
    commands.add_parser(
        "sync", help="`uv sync --locked` every user package, reporting failures"
    )
    check = commands.add_parser(
        "check", help="report every user package that is not standalone"
    )
    check.add_argument(
        "--lock", action="store_true", help="also run `uv lock --check` on each"
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    packages = user_packages(repo_root)
    if args.command == "list":
        for relative in packages:
            print(relative)
        return 0
    if args.command == "sync":
        return 1 if sync_all(repo_root) else 0
    failed = False
    for relative in packages:
        problems = standalone_problems(repo_root, relative)
        if args.lock and not problems:
            stale = lock_check(repo_root, relative)
            if stale is not None:
                problems.append(f"its uv.lock is out of date: {stale}")
        for problem in problems:
            failed = True
            print(f"{relative}: {problem}")
    if failed:
        print(
            f"See {MIGRATION_REFERENCE}, 'A user app, service or lib'.", file=sys.stderr
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
