"""Convert a user package from the shared workspace to a standalone uv project.

CLEANUP: remove this module, its test and the "A user app, service or lib" section of
``references/python-packaging-migration.md`` once no old template or workspace still has a
user package under ``system/`` that is a root workspace member (the layout before
``docs/system/blueprint/app-environments/``).

A user package of the old layout is a member of the root workspace: it has no lock of its
own, resolves shared code through ``{ workspace = true }`` sources (its own or the root's),
and its program runs it from the root venv. ``migrate`` turns it into what the scaffold
writes today, keeping the versions it runs on:

1. its ``uv.lock`` is seeded from the shared lock it ran on (``--shared-lock``, the root
   ``uv.lock`` from before the update's merge; else the current one), so ``uv lock`` keeps
   every version it can;
2. every source the root workspace gave it -- a ``{ workspace = true }`` one of its own, or
   a workspace member it depends on through the root's sources -- becomes an editable path
   source, so the shared code stays live;
3. it gets a dev dependency its tests run with (``pytest``, or ``imbue-common[testing]``
   for tests that use the ratchet helpers) and its own pytest root, when it has tests;
4. it leaves the root ``pyproject.toml``'s dependencies and sources;
5. its program line links its venv, syncs its own project ``--locked`` and execs from its
   own ``.venv/bin``, and each one-off of its commands written in a tracked agent-facing
   file names its project (``uv run --no-sync --project <package> <command>``);
6. it is locked, every package the root sources from git that it declares directly is held
   to the root's pin, and its venv is linked and synced.

A step that fails puts every file the migration wrote back as it was, so the package can be
migrated again once the cause is fixed. ``--dry-run`` prints what it would change and
changes nothing. ``relock`` re-runs step 6's
lock and pin alignment, for a package whose shared libraries or mngr pin moved.

Run from the repo root as update-self's ``user-package-migration`` command:
``uv run --no-sync user-package-migration migrate system/apps/<package> --dry-run``.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from update_self_skill import user_packages, venv_links

# What a package's tests need in its own venv: pytest, or, for tests that use the
# workspace's ratchet helpers (as build-app's scaffold writes them), imbue-common's testing
# extra, which carries pytest, inline-snapshot and import-linter.
PYTEST_DEPENDENCY = "pytest"
RATCHET_TEST_DEPENDENCY = "imbue-common[testing]"
_RATCHET_IMPORTS = ("imbue.imbue_common.ratchet_testing", "inline_snapshot")
_AGENT_FACING_SUFFIXES = (".md", ".toml", ".sh", ".conf", ".py")
_TABLE_HEADER = re.compile(r"^\s*\[")
# What separates the steps of a program's `bash -c` script, as user_packages splits them.
_SCRIPT_SEPARATOR = re.compile(r"(&&|\|\||;|\n)")
_DIRECTORY_CHANGES = frozenset({"cd", "pushd"})
# A shell variable assignment, which sets the variable for the command after it.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


class MigrationError(Exception):
    """The package cannot be converted as it stands; the message says why."""


class Completed(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Run = Callable[[Sequence[str], Path], Completed]


def run_subprocess(command: Sequence[str], cwd: Path) -> Completed:
    result = subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )
    return Completed(result.returncode, result.stdout, result.stderr)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return _normalize(match.group(1)) if match else ""


def _parse_toml(text: str, path: Path) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MigrationError(f"{path} does not parse: {exc}") from exc


def _read_toml(path: Path) -> dict:
    return _parse_toml(path.read_text(encoding="utf-8"), path)


def _uv_sources(pyproject: dict) -> dict:
    return pyproject.get("tool", {}).get("uv", {}).get("sources", {})


def _declared_requirements(pyproject: dict) -> list[str]:
    project = pyproject.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in pyproject.get("dependency-groups", {}).values():
        # A group can also hold `{ include-group = "..." }` tables.
        requirements.extend(entry for entry in group if isinstance(entry, str))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    return requirements


def _package_directories(repo_root: Path) -> dict[str, str]:
    """Each package a workspace source could have named, by distribution name, mapped to its
    repo-relative directory: the root workspace's members, and the user packages, which the
    old layout's globs made members too (a user app on a user lib, say)."""
    directories = [
        directory
        for pattern in user_packages.built_in_members(repo_root)
        for directory in sorted(repo_root.glob(pattern))
    ]
    directories.extend(
        repo_root / relative for relative in user_packages.user_packages(repo_root)
    )
    found: dict[str, str] = {}
    for directory in directories:
        pyproject_path = directory / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        name = _read_toml(pyproject_path).get("project", {}).get("name")
        if name is not None:
            found[_normalize(name)] = directory.relative_to(repo_root).as_posix()
    return found


def _requirement_extras(requirement: str) -> frozenset[str]:
    match = re.match(r"\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]*)\]", requirement)
    if match is None:
        return frozenset()
    return frozenset(
        _normalize(extra.strip())
        for extra in match.group(1).split(",")
        if extra.strip()
    )


def _root_sources(repo_root: Path) -> dict[str, dict]:
    # A source that is a list (one per marker) is never a workspace or git pin to carry over.
    return {
        _normalize(name): value
        for name, value in _uv_sources(_read_toml(repo_root / "pyproject.toml")).items()
        if isinstance(value, dict)
    }


def _inline_table(value: dict) -> str:
    parts = []
    for key, item in value.items():
        if isinstance(item, bool):
            rendered = "true" if item else "false"
        elif isinstance(item, str):
            rendered = '"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            raise MigrationError(f"cannot write the source value {key} = {item!r}")
        parts.append(f"{key} = {rendered}")
    return "{ " + ", ".join(parts) + " }"


def _table_span(lines: list[str], header: str) -> tuple[int, int] | None:
    """The ``[start, end)`` line span of the table ``header`` (its header line included)."""
    for index, line in enumerate(lines):
        if line.strip() == header:
            end = index + 1
            while end < len(lines) and not _TABLE_HEADER.match(lines[end]):
                end += 1
            return index, end
    return None


def _set_sources(text: str, sources: dict[str, dict], changed: set[str]) -> str:
    """``text`` with ``[tool.uv.sources]`` holding ``sources``: the ``changed`` names rewritten
    (or added), every other line of the table kept as it was."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, "[tool.uv.sources]")
    if span is None:
        body = [
            f"{name} = {_inline_table(sources[name])}\n" for name in sorted(changed)
        ]
        separator = (
            "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        )
        return text + separator + "[tool.uv.sources]\n" + "".join(body)
    start, end = span
    kept: list[str] = []
    for line in lines[start + 1 : end]:
        key = line.split("=", 1)[0].strip()
        if "=" in line and _normalize(key) in changed:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    added = [f"{name} = {_inline_table(sources[name])}\n" for name in sorted(changed)]
    return (
        "".join(lines[: start + 1] + kept + added + ["\n"] + lines[end:]).rstrip("\n")
        + "\n"
    )


def _add_dev_requirement(text: str, requirement: str) -> str:
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, "[dependency-groups]")
    if span is None:
        separator = "" if text.endswith("\n\n") else "\n"
        return text + separator + f'[dependency-groups]\ndev = ["{requirement}"]\n'
    start, end = span
    for index in range(start + 1, end):
        if re.match(r"^\s*dev\s*=\s*\[", lines[index]):
            lines[index] = re.sub(
                r"\[", f'[\n    "{requirement}",', lines[index], count=1
            )
            return "".join(lines)
    lines.insert(end, f'dev = ["{requirement}"]\n')
    return "".join(lines)


def _add_pytest_root(text: str) -> str:
    separator = "" if text.endswith("\n\n") else "\n"
    return text + separator + '[tool.pytest.ini_options]\ntestpaths = ["."]\n'


def _remove_from_root(text: str, name: str) -> str:
    """The root pyproject without ``name`` among its dependencies or sources."""
    pattern = re.compile(
        rf'^\s*"{re.escape(name)}(\[[^\]]*\])?"\s*,?\s*(#.*)?\n'
        rf"|^{re.escape(name)}\s*=\s*\{{[^}}]*\}}\s*(#.*)?\n",
        re.MULTILINE,
    )
    return pattern.sub("", text)


_MINIMUM_PYTHON = re.compile(r"^\s*>=\s*(\d+)\.(\d+)\s*$")


def _raise_python_floor(text: str, pyproject: dict, root: dict) -> str | None:
    """``text`` with ``requires-python`` raised to the root's floor, or ``None`` when it
    already meets it. A standalone package resolves the workspace's libraries on its own,
    and they require the root's floor (an older scaffold wrote a lower one, which no longer
    locks); the package already ran on the root's interpreter, so nothing it runs changes."""
    own = pyproject.get("project", {}).get("requires-python")
    floor = root.get("project", {}).get("requires-python")
    own_match = _MINIMUM_PYTHON.match(own) if isinstance(own, str) else None
    floor_match = _MINIMUM_PYTHON.match(floor) if isinstance(floor, str) else None
    if own_match is None or floor_match is None or not isinstance(floor, str):
        return None
    if tuple(map(int, own_match.groups())) >= tuple(map(int, floor_match.groups())):
        return None
    return re.sub(
        r'^(\s*requires-python\s*=\s*)"[^"]*"',
        lambda match: f'{match.group(1)}"{floor}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _test_files(package_dir: Path) -> list[Path]:
    return [
        path
        for path in package_dir.rglob("*.py")
        if ".venv" not in path.parts
        and (path.name.endswith("_test.py") or path.name.startswith("test_"))
    ]


def _test_dependency(test_files: Sequence[Path]) -> str:
    uses_ratchets = any(
        marker in path.read_text(errors="replace")
        for path in test_files
        for marker in _RATCHET_IMPORTS
    )
    return RATCHET_TEST_DEPENDENCY if uses_ratchets else PYTEST_DEPENDENCY


class Plan(NamedTuple):
    relative: str
    seed: Path
    package_text: str
    root_text: str
    program_files: dict[Path, str]
    caller_files: dict[Path, str]
    notes: list[str]


def _program_rewrite(command: str, relative: str, scripts: Sequence[str]) -> str | None:
    """The standalone form of a program command that runs one of ``scripts``, or ``None``:
    its prefix (an OOM tag, say) kept, then the shell it ran under (``bash -c`` when it
    had none) linking and syncing the package's own project before the steps it ran, with
    the script run (exec'd, when nothing runs after it) from the package's venv."""
    words = user_packages.split_words(command)
    shell_at = next(
        (
            index
            for index, word in enumerate(words[:-2])
            if Path(word).name in user_packages.SHELLS
            and words[index + 1] in ("-c", "-lc")
        ),
        None,
    )
    shell = ["bash", "-c"]
    if shell_at is not None:
        prefix = words[:shell_at]
        shell = words[shell_at : shell_at + 2]
        rewritten = _rewrite_script(words[shell_at + 2], relative, scripts)
    else:
        start = next(
            (
                index
                for index, word in enumerate(words)
                if word == "uv"
                or any(user_packages.runs_script(word, s) for s in scripts)
            ),
            None,
        )
        if start is None:
            return None
        prefix, step = words[:start], words[start:]
        script = _script_in(step, scripts)
        # No shell ran these words before, so each is quoted to stay one literal word.
        rewritten = (
            None
            if script is None
            else " ".join(
                map(_quote, _with_exec(_own_script_words(step, script, relative)))
            )
        )
    if rewritten is None:
        return None
    script_text = " && ".join(
        [
            f"python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project {relative}",
            f"uv sync --locked --project {relative}",
            rewritten,
        ]
    )
    return " ".join(
        [
            *map(_quote, [*prefix, *shell]),
            '"' + script_text.replace("\\", "\\\\").replace('"', '\\"') + '"',
        ]
    )


def _script_in(words: Sequence[str], scripts: Sequence[str]) -> str | None:
    return next(
        (
            s
            for s in scripts
            if any(user_packages.runs_script(_unquoted(w), s) for w in words)
        ),
        None,
    )


def _unquoted(word: str) -> str:
    return word.strip("'\"")


def _own_script_words(step: Sequence[str], script: str, relative: str) -> list[str]:
    """``step`` running ``script`` from the package's own venv rather than through
    ``uv run`` or another install."""
    words = list(step)
    script_at = next(
        index
        for index, word in enumerate(words)
        if user_packages.runs_script(_unquoted(word), script)
    )
    uv_run_at = next(
        (
            index
            for index in range(script_at - 1)
            if words[index : index + 2] == ["uv", "run"]
        ),
        None,
    )
    if uv_run_at is not None:
        # Cut from `uv run` to the script itself, so no option of uv run's, valued or not,
        # survives, and whatever wrapped it (`exec`, `nice -n 5`) still does.
        words = [*words[:uv_run_at], *words[script_at:]]
    own = f"{relative}/.venv/bin/{script}"
    return [
        own if user_packages.runs_script(_unquoted(word), script) else word
        for word in words
    ]


def _with_exec(words: Sequence[str]) -> list[str]:
    """``words`` run by ``exec``, so the script becomes the process supervisord started."""
    words = list(words)
    # `PORT=1 exec cmd` passes PORT to cmd; `exec PORT=1 cmd` execs a command named `PORT=1`.
    assignments = 0
    while assignments < len(words) and _ASSIGNMENT.match(words[assignments]):
        assignments += 1
    if words[assignments] == "exec":
        return words
    return [*words[:assignments], "exec", *words[assignments:]]


def _rewrite_script(script: str, relative: str, scripts: Sequence[str]) -> str | None:
    """A ``bash -c`` script with its ``uv sync`` steps dropped and each step running one of
    ``scripts`` running it from the package's venv; ``None`` when no step runs one. That
    step is exec'd when it is the script's last, and otherwise left to return, so the steps
    after it (a backoff, a cleanup) still run.

    Every other step is kept as written, so its quoting, expansions and redirections
    survive. A dropped sync counts as having succeeded (the new up-front sync replaces it):
    ``X && sync SEP Y`` becomes ``X SEP Y`` and any other ``X ? sync ? Y`` becomes
    ``X; Y``. Steps joined by anything but ``&&`` are grouped, so the up-front sync gates
    the whole script and not only its first step.

    ``None`` too when a step before the script's changes directory: the venv path the
    exec names is relative to the repo root, so the line is left for a hand rewrite.
    """
    pieces = _SCRIPT_SEPARATOR.split(script)
    kept: list[tuple[str, str]] = []
    pending: str | None = None
    last_script: tuple[int, list[str]] | None = None
    changes_directory = False
    for index in range(0, len(pieces), 2):
        if kept:
            separator = ";" if pieces[index - 1] == "\n" else pieces[index - 1]
            pending = separator if pending is None or pending == "&&" else ";"
        text = pieces[index].strip()
        words = user_packages.split_words(text)
        if not words or words[:2] == ["uv", "sync"]:
            continue
        found_script = (
            None
            if user_packages.is_registration_step(words)
            else _script_in(words, scripts)
        )
        if found_script is not None:
            if changes_directory:
                return None
            own_words = _own_script_words(_raw_words(text), found_script, relative)
            last_script = (len(kept), own_words)
            text = " ".join(own_words)
        elif words[0] in _DIRECTORY_CHANGES:
            changes_directory = True
        kept.append((pending or "", text))
        pending = None
    if last_script is None:
        return None
    script_index, own_words = last_script
    if script_index == len(kept) - 1:
        kept[script_index] = (kept[script_index][0], " ".join(_with_exec(own_words)))
    body = kept[0][1] + "".join(
        ("; " if separator == ";" else f" {separator} ") + text
        for separator, text in kept[1:]
    )
    if any(separator != "&&" for separator, _ in kept[1:]):
        return f"{{ {body}; }}"
    return body


def _raw_words(text: str) -> list[str]:
    """``text``'s words as written, quotes kept, so re-joining them changes nothing a
    shell would read differently."""
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _quote(word: str) -> str:
    return (
        word
        if re.fullmatch(r"[A-Za-z0-9_./:=,@%+-]+", word)
        else "'" + word.replace("'", "'\"'\"'") + "'"
    )


def _rewrite_programs(
    repo_root: Path, relative: str, scripts: Sequence[str]
) -> dict[Path, str]:
    rewritten: dict[Path, str] = {}
    dropins = repo_root / user_packages.SUPERVISORD_DROPIN_DIR
    for conf in sorted(dropins.glob("*.conf")) if dropins.is_dir() else []:
        text = conf.read_text()
        out: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            match = re.match(r"^(\s*command\s*=\s*)(.*?)(\s*)$", line, re.DOTALL)
            if match and user_packages.program_problems(
                match.group(2), relative, scripts
            ):
                new = _program_rewrite(match.group(2), relative, scripts)
                if new is not None:
                    out.append(f"{match.group(1)}{new}\n")
                    changed = True
                    continue
            out.append(line)
        if changed:
            rewritten[conf] = "".join(out)
    return rewritten


def _tracked_files(repo_root: Path, run: Run) -> list[Path]:
    result = run(["git", "ls-files", "-z"], repo_root)
    return [repo_root / name for name in result.stdout.split("\0") if name]


def _rewrite_callers(
    repo_root: Path,
    relative: str,
    scripts: Sequence[str],
    run: Run,
    programs: dict[Path, str],
) -> dict[Path, str]:
    """Each tracked agent-facing file with its one-offs of ``scripts`` naming the project.

    A file ``programs`` already rewrites is read as rewritten, so a drop-in's comments get
    this rewrite on top of its standalone program line rather than in place of it.
    """
    pattern = re.compile(
        r"\buv run((?:\s+--(?!project|directory)[\w-]+)*)\s+("
        + "|".join(map(re.escape, scripts))
        + r")(?![\w-])"
    )
    rewritten: dict[Path, str] = {}
    if not scripts:
        return rewritten
    for path in _tracked_files(repo_root, run):
        relative_path = path.relative_to(repo_root).as_posix()
        if relative_path.startswith(relative + "/") or not relative_path.endswith(
            _AGENT_FACING_SUFFIXES
        ):
            continue
        if "changelog" in path.parts or not path.is_file():
            continue
        text = programs.get(path) or path.read_text(errors="replace")
        new = pattern.sub(
            lambda m: f"uv run{m.group(1) or ' --no-sync'} --project {relative} {m.group(2)}",
            text,
        )
        if new != text:
            rewritten[path] = new
    return rewritten


def plan_migration(
    repo_root: Path, relative: str, shared_lock: Path | None, run: Run
) -> Plan:
    package_dir = repo_root / relative
    if not (package_dir / "pyproject.toml").is_file():
        raise MigrationError(f"{relative} has no pyproject.toml")
    if relative not in user_packages.user_packages(repo_root):
        raise MigrationError(
            f"{relative} is a root workspace member, not a user package; the root members name "
            "only built-in packages"
        )
    package_text = (package_dir / "pyproject.toml").read_text()
    pyproject = _parse_toml(package_text, package_dir / "pyproject.toml")
    name = pyproject.get("project", {}).get("name")
    if name is None:
        raise MigrationError(f"{relative}/pyproject.toml names no project")
    packages = _package_directories(repo_root)
    root_sources = _root_sources(repo_root)
    own_sources = {
        _normalize(key): value
        for key, value in _uv_sources(pyproject).items()
        if isinstance(value, dict)
    }
    notes: list[str] = []
    changed: dict[str, dict] = {}
    for requirement in _declared_requirements(pyproject):
        dependency = _requirement_name(requirement)
        own = own_sources.get(dependency)
        through_root = (
            own is None and root_sources.get(dependency, {}).get("workspace") is True
        )
        if (own is not None and own.get("workspace") is True) or through_root:
            if dependency not in packages:
                raise MigrationError(
                    f"{relative} takes `{dependency}` from the workspace, but no package under the "
                    f"workspace is named `{dependency}`"
                )
            changed[dependency] = {
                "path": os.path.relpath(repo_root / packages[dependency], package_dir),
                "editable": True,
            }
    raised = _raise_python_floor(
        package_text, pyproject, _read_toml(repo_root / "pyproject.toml")
    )
    if raised is not None:
        package_text = raised
        notes.append(
            "raises its requires-python to the workspace's floor, which its libraries need"
        )
    test_files = _test_files(package_dir)
    if test_files:
        dependency = _test_dependency(test_files)
        # Declared with every extra it needs: a plain `imbue-common` lacks the testing extra.
        needed = _requirement_extras(dependency)
        is_declared = any(
            _requirement_name(requirement) == _requirement_name(dependency)
            and needed <= _requirement_extras(requirement)
            for requirement in _declared_requirements(pyproject)
        )
        if not is_declared:
            package_text = _add_dev_requirement(package_text, dependency)
            notes.append(
                f"adds `{dependency}` to its dev group, so its tests run from its own venv"
            )
        if not pyproject.get("tool", {}).get("pytest", {}).get("ini_options"):
            package_text = _add_pytest_root(package_text)
            notes.append("makes its directory its tests' own pytest root")
    if changed:
        package_text = _set_sources(
            package_text, {**own_sources, **changed}, set(changed)
        )
        notes.extend(
            f"`{dependency}` becomes an editable path source ({source['path']})"
            for dependency, source in sorted(changed.items())
        )
    _verify_package(package_text, relative, set(changed))
    root_before = (repo_root / "pyproject.toml").read_text()
    root_text = _remove_from_root(root_before, _normalize(name))
    if root_text != root_before:
        notes.append(f"`{name}` leaves the root pyproject's dependencies and sources")
    _verify_root(root_text, _normalize(name))
    scripts = user_packages.console_scripts(package_dir)
    programs = _rewrite_programs(repo_root, relative, scripts)
    callers = _rewrite_callers(repo_root, relative, scripts, run, programs)
    seed = shared_lock if shared_lock is not None else repo_root / "uv.lock"
    if not seed.is_file():
        raise MigrationError(f"the shared lock to seed from, {seed}, does not exist")
    return Plan(relative, seed, package_text, root_text, programs, callers, notes)


def _refuse_unconverted_user_dependencies(repo_root: Path, relative: str) -> None:
    """Refuse, before anything is written, a package that depends on a user package not
    converted yet: uv cannot lock a path dependency that still takes code from the
    workspace, through ``{ workspace = true }`` sources of its own or the root's sources it
    is no longer a member to receive. Either way it has no ``uv.lock`` of its own, or such a
    source. A dry run is not refused, so one over a lib and the package using it plans both."""
    pyproject = _read_toml(repo_root / relative / "pyproject.toml")
    packages = _package_directories(repo_root)
    others = set(user_packages.user_packages(repo_root)) - {relative}
    for dependency in sorted(
        {_requirement_name(entry) for entry in _declared_requirements(pyproject)}
    ):
        directory = packages.get(dependency)
        if directory not in others:
            continue
        sources = _uv_sources(_read_toml(repo_root / directory / "pyproject.toml"))
        if not (repo_root / directory / "uv.lock").is_file() or any(
            isinstance(value, dict) and value.get("workspace") is True
            for value in sources.values()
        ):
            raise MigrationError(
                f"{relative} depends on the user package {directory}, which is not standalone "
                f"yet; migrate {directory} first, then {relative}"
            )


def _verify_package(text: str, relative: str, converted: set[str]) -> None:
    try:
        sources = _uv_sources(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise MigrationError(
            f"rewriting {relative}/pyproject.toml would not parse: {exc}"
        ) from exc
    for name, value in sources.items():
        if isinstance(value, dict) and value.get("workspace") is True:
            raise MigrationError(
                f"{relative}/pyproject.toml would still take `{name}` from the workspace"
            )
    missing = converted - {_normalize(name) for name in sources}
    if missing:
        raise MigrationError(
            f"{relative}/pyproject.toml would lose the sources for {sorted(missing)}"
        )


def _verify_root(text: str, name: str) -> None:
    try:
        root = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MigrationError(
            f"rewriting the root pyproject.toml would not parse: {exc}"
        ) from exc
    dependencies = root.get("project", {}).get("dependencies", [])
    if any(_requirement_name(entry) == name for entry in dependencies):
        raise MigrationError(
            f"`{name}` is still a root dependency in a form this cannot remove"
        )
    sources = _uv_sources(root)
    if name in {_normalize(key) for key in sources}:
        raise MigrationError(
            f"`{name}` still has a root source in a form this cannot remove"
        )


def describe(plan: Plan, repo_root: Path) -> str:
    lines = [f"{plan.relative}:", f"  seeds its uv.lock from {plan.seed}"]
    lines.extend(f"  {note}" for note in plan.notes)
    for path in sorted({*plan.program_files, *plan.caller_files}):
        lines.append(f"  rewrites {path.relative_to(repo_root)}")
    lines.append(
        "  then locks it (holding root-pinned packages to the root's pin) and syncs its venv"
    )
    return "\n".join(lines)


def align_pins(repo_root: Path, relative: str, run: Run) -> list[str]:
    """Hold each package the root sources from git, and that ``relative`` declares directly,
    to the root's source: a package with none of its own and none through a shared library
    would otherwise resolve from the index. Returns the packages it pinned."""
    package_dir = repo_root / relative
    pyproject = _read_toml(package_dir / "pyproject.toml")
    lock = _read_toml(package_dir / "uv.lock")
    locked = {
        _normalize(entry["name"]): entry.get("source", {})
        for entry in lock.get("package", [])
    }
    pinned: dict[str, dict] = {}
    for dependency in {
        _requirement_name(requirement)
        for requirement in _declared_requirements(pyproject)
    }:
        root_source = _root_sources(repo_root).get(dependency)
        if root_source is None or "git" not in root_source:
            continue
        locked_git = str(locked.get(dependency, {}).get("git", ""))
        if (
            not locked_git.startswith(root_source["git"])
            or root_source.get("rev", "") not in locked_git
        ):
            pinned[dependency] = root_source
    if pinned:
        text = (package_dir / "pyproject.toml").read_text()
        own = _uv_sources(pyproject)
        text = _set_sources(
            text, {**{_normalize(k): v for k, v in own.items()}, **pinned}, set(pinned)
        )
        (package_dir / "pyproject.toml").write_text(text)
        _lock(repo_root, relative, run)
    return sorted(pinned)


def _link_runner(run: Run) -> venv_links.Run:
    def link_run(command: Sequence[str], /, *, cwd: Path) -> venv_links.Completed:
        return run(command, cwd)

    return link_run


def _lock(repo_root: Path, relative: str, run: Run) -> None:
    result = run(["uv", "lock", "--project", relative], repo_root)
    if result.returncode != 0:
        raise MigrationError(
            f"`uv lock --project {relative}` failed:\n{(result.stderr or result.stdout).strip()}"
        )


def _read_if_present(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _put_back(originals: dict[Path, bytes | None]) -> None:
    for path, content in originals.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)


def apply(plan: Plan, repo_root: Path, run: Run) -> list[str]:
    """Carry out ``plan``, all or nothing: when a step fails, every file it wrote is put
    back as it was, so a later ``migrate`` plans the package afresh."""
    _refuse_unconverted_user_dependencies(repo_root, plan.relative)
    package_dir = repo_root / plan.relative
    written = [
        package_dir / "pyproject.toml",
        package_dir / "uv.lock",
        repo_root / "pyproject.toml",
        repo_root / "uv.lock",
        *plan.program_files,
        *plan.caller_files,
    ]
    originals = {path: _read_if_present(path) for path in written}
    try:
        return _apply_steps(plan, repo_root, package_dir, run)
    except BaseException:
        _put_back(originals)
        raise


def _apply_steps(plan: Plan, repo_root: Path, package_dir: Path, run: Run) -> list[str]:
    (package_dir / "pyproject.toml").write_text(plan.package_text)
    shutil.copyfile(plan.seed, package_dir / "uv.lock")
    root_changed = plan.root_text != (repo_root / "pyproject.toml").read_text()
    (repo_root / "pyproject.toml").write_text(plan.root_text)
    for path, text in {**plan.program_files, **plan.caller_files}.items():
        path.write_text(text)
    _lock(repo_root, plan.relative, run)
    pinned = align_pins(repo_root, plan.relative, run)
    if root_changed:
        result = run(["uv", "lock"], repo_root)
        if result.returncode != 0:
            raise MigrationError(
                f"`uv lock` at the root failed:\n{(result.stderr or result.stdout).strip()}"
            )
    venv_links.ensure(None, [package_dir], run=_link_runner(run))
    result = run(["uv", "sync", "--locked", "--project", plan.relative], repo_root)
    if result.returncode != 0:
        raise MigrationError(
            f"`uv sync --locked --project {plan.relative}` failed:\n{(result.stderr or result.stdout).strip()}"
        )
    return pinned


def main(argv: Sequence[str] | None = None, run: Run = run_subprocess) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a user package to a standalone uv project."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate", help="convert one user package")
    migrate_parser.add_argument(
        "package", help="its directory, e.g. system/apps/<package>"
    )
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="print the changes; make none"
    )
    migrate_parser.add_argument(
        "--shared-lock",
        type=Path,
        default=None,
        help="the root uv.lock the package ran on (from before the update's merge) to seed its lock",
    )
    relock = commands.add_parser(
        "relock", help="relock a standalone user package and align its pins"
    )
    relock.add_argument("package")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    relative = args.package.rstrip("/")
    if args.command == "relock":
        try:
            _lock(repo_root, relative, run)
            pinned = align_pins(repo_root, relative, run)
        except MigrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"{relative}: relocked"
            + (f"; pinned {', '.join(pinned)} to the root's" if pinned else "")
        )
        return 0
    return migrate(repo_root, relative, args.shared_lock, args.dry_run, run)


def migrate(
    repo_root: Path,
    relative: str,
    shared_lock: Path | None,
    is_dry_run: bool,
    run: Run = run_subprocess,
) -> int:
    """Convert the user package at ``relative``, printing what it changes (and, for a dry
    run, changing nothing); 0 once it is standalone or already was, 1 when it is not."""
    if relative in user_packages.user_packages(repo_root) and not (
        user_packages.standalone_problems(repo_root, relative)
    ):
        print(f"{relative}: already a standalone project; nothing to do")
        return 0
    try:
        plan = plan_migration(repo_root, relative, shared_lock, run)
        print(describe(plan, repo_root))
        if is_dry_run:
            return 0
        pinned = apply(plan, repo_root, run)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if pinned:
        print(f"  pinned {', '.join(pinned)} to the root's source")
    problems = user_packages.standalone_problems(repo_root, relative)
    for problem in problems:
        print(f"  still not standalone: {problem}")
    return 1 if problems else 0
