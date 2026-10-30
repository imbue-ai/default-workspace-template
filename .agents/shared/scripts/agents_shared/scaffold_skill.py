"""Start a new skill in the layout spec-summary.md describes, ready to validate.

Writes ``.agents/skills/<name>/SKILL.md`` and, with ``--python``, the skill's uv project
(``python/pyproject.toml`` and the ``<name_with_underscores>_skill`` package with a ``run.py``
entry point). It then runs ``uv lock`` and ``uv sync --all-packages`` so the package is
installed in the root venv, and finishes with ``validate_skill``'s checks.

Run from the repo root:

    uv run --no-sync .agents/shared/scripts/agents_shared/scaffold_skill.py <name> \\
        --description "<what it does and when to use it>" [--python]

The new skill is marked ``metadata.crystallized: true``, made in this workspace, so the
workspace holds it to its own checks.

Exits 0 and prints the skill's directory when every step passed. Exits 1 with the failing
step on stderr; a failure after the files were written leaves them in place to fix.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from agents_shared.validate_skill import (
    NAME_PATTERN,
    CommandRunner,
    check_runnable,
    package_name,
    validate,
)

# A cold `uv sync --all-packages` builds every workspace member.
_UV_TIMEOUT_SECONDS = 600

_RUN_PY = '''"""The {name} skill's entry point: one subcommand per step, and ``all`` to chain them."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description={description!r})
    # Add a subcommand per step, and an ``all`` that chains them in-process
    # (.agents/shared/references/spec-summary.md).
    parser.add_subparsers(dest="command", required=True)
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_PYPROJECT = """[project]
name = "{name}-skill"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]
"""


class ScaffoldError(Exception):
    """The skill could not be started, or a step after writing it failed."""


def skill_dir(repo_root: Path, name: str) -> Path:
    return repo_root / ".agents" / "skills" / name


def entry_point(repo_root: Path, name: str) -> Path:
    return skill_dir(repo_root, name) / "python" / package_name(name) / "run.py"


def _skill_md(name: str, description: str, *, has_python: bool) -> str:
    frontmatter = [
        "---",
        f"name: {name}",
        f"description: {_yaml_string(description)}",
        "metadata:",
        "  crystallized: true",
        "---",
    ]
    body = ["", f"# {name}", "", description, ""]
    if has_python:
        body += [
            "## Running it",
            "",
            "```bash",
            f"uv run --no-sync .agents/skills/{name}/python/{package_name(name)}/run.py --help",
            "```",
            "",
        ]
    return "\n".join(frontmatter + body)


def _yaml_string(text: str) -> str:
    """``text`` as a YAML double-quoted scalar, which reads back as exactly ``text``."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def write_skill(
    repo_root: Path,
    name: str,
    description: str,
    *,
    has_python: bool,
) -> Path:
    """Write the new skill's files; refuse a bad name or a skill that already exists."""
    if not NAME_PATTERN.fullmatch(name):
        raise ScaffoldError(
            f"{name!r} is not a skill name: lowercase letters and digits separated by single hyphens"
        )
    directory = skill_dir(repo_root, name)
    if directory.exists():
        raise ScaffoldError(f"{directory} already exists")
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        _skill_md(name, description, has_python=has_python)
    )
    if has_python:
        package = package_name(name)
        package_dir = directory / "python" / package
        package_dir.mkdir(parents=True)
        (directory / "python" / "pyproject.toml").write_text(
            _PYPROJECT.format(name=name, package=package)
        )
        (package_dir / "__init__.py").write_text("")
        (package_dir / "run.py").write_text(
            _RUN_PY.format(name=name, description=description)
        )
    return directory


def _run_uv(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_UV_TIMEOUT_SECONDS,
        check=False,
    )


def install(repo_root: Path, runner: CommandRunner) -> None:
    """Add the new project to the workspace lock and install it in the root venv."""
    for argv in (["uv", "lock"], ["uv", "sync", "--all-packages"]):
        try:
            result = runner(argv, repo_root)
        except subprocess.TimeoutExpired as exc:
            raise ScaffoldError(
                f"`{' '.join(argv)}` did not finish within {_UV_TIMEOUT_SECONDS}s"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ScaffoldError(
                f"`{' '.join(argv)}` failed (exit {result.returncode}):\n{detail}"
            )


def scaffold(
    repo_root: Path,
    name: str,
    description: str,
    *,
    has_python: bool,
    runner: CommandRunner = _run_uv,
) -> Path:
    """Write the skill, install its package when it has one, and validate it."""
    directory = write_skill(repo_root, name, description, has_python=has_python)
    error = validate(directory)
    if error is not None:
        # Nothing has been installed yet, so the arguments are what is wrong.
        shutil.rmtree(directory)
        raise ScaffoldError(f"the new skill would not validate: {error}")
    if has_python:
        install(repo_root, runner)
    error = check_runnable(directory, runner)
    if error is not None:
        raise ScaffoldError(f"the new skill does not run: {error}")
    return directory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start a new skill, ready to validate."
    )
    parser.add_argument(
        "name", help="The skill's name: lowercase letters and digits, single hyphens."
    )
    parser.add_argument(
        "--description",
        required=True,
        help="What the skill does and when to use it (1-1024 chars).",
    )
    parser.add_argument(
        "--python",
        action="store_true",
        help="Give the skill a python/ uv project with a run.py entry point.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="The repository root (default: current directory).",
    )
    args = parser.parse_args(argv)
    try:
        directory = scaffold(
            Path(args.repo_root).resolve(),
            args.name,
            args.description,
            has_python=args.python,
        )
    except ScaffoldError as exc:
        sys.stderr.write(f"scaffold_skill: {exc}\n")
        return 1
    print(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
