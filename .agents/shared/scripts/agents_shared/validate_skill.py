"""Validate a skill directory against the agentskills.io spec.

Static structural checks (in order, short-circuit on first failure per check):

- Directory exists.
- `SKILL.md` exists.
- SKILL.md has valid YAML frontmatter (delimited by `---` lines).
- Frontmatter has `name` matching the directory basename.
- Frontmatter has `description`, 1-1024 characters.
- SKILL.md body (after frontmatter) is at most 500 lines.
- If the skill has Python (entry files in `scripts/`, or a `python/` dir --
  optional even for crystallized skills: a skill may be pure SKILL.md prose, and
  a `scripts/` dir of shell scripts needs nothing), `python/pyproject.toml`
  exists and names the project `<name>-skill` and its one package
  `<name_with_underscores>_skill` (a directory beside it with an `__init__.py`).

Runnability checks (only when the static checks pass and the skill has Python):

- `uv lock --check` passes: the workspace lock includes the skill's
  dependencies, so `uv sync --all-packages` installs them.
- `uv run --no-sync <entry> --help` exits 0 for every entry file at the top of
  `scripts/`, so a broken import or a missing dependency is caught here rather
  than only at scenario time. `--help` is a shallow import check: it exercises
  the top-level imports and argparse wiring, not imports done lazily inside
  subcommand bodies -- those are left to scenario testing.

Exits 0 and prints `ok` when valid; exits 1 with a human-readable error to
stderr otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

_MAX_BODY_LINES = 500
_MIN_DESC_LEN = 1
_MAX_DESC_LEN = 1024
_MIN_NAME_LEN = 1
_MAX_NAME_LEN = 64
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Generous enough to cover a cold `uv lock --check` (resolving git sources);
# warm-cache runs are near-instant.
_UV_TIMEOUT_SECONDS = 180

# A callable that runs one command in a directory and reports the completed
# process. Injectable so tests can exercise the result-handling without a real
# `uv` run.
CommandRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse leading ``---`` YAML frontmatter; return (fm_dict, body_lines).

    Raises ``ValueError`` if the frontmatter is missing, malformed, or not a
    mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with `---` frontmatter delimiter")
    try:
        end_idx = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not terminated with `---`") from exc
    fm_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    body_lines = lines[end_idx + 1 :]
    return parsed, body_lines


def _package_name(skill_name: str) -> str:
    return skill_name.replace("-", "_") + "_skill"


def _is_test_file(path: Path) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def _entry_files(scripts_dir: Path) -> list[Path]:
    """The entry points: every ``.py`` at the top of ``scripts/`` except tests."""
    return sorted(path for path in scripts_dir.glob("*.py") if not _is_test_file(path))


def _validate_python_project(skill_dir: Path, skill_name: str) -> str | None:
    """If the skill has Python, require its ``python/`` uv project to be named for the skill.

    A skill with no entry files in ``scripts/`` and no ``python/`` dir is OK.
    """
    project_dir = skill_dir / "python"
    entries = (
        _entry_files(skill_dir / "scripts") if (skill_dir / "scripts").is_dir() else []
    )
    if not entries and not project_dir.is_dir():
        return None
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return (
            f"{pyproject} is missing: a skill's Python lives in its python/ uv project, "
            "a workspace member that needs one (see "
            ".agents/shared/references/spec-summary.md, Packaging)"
        )
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return f"{pyproject} is not valid TOML: {exc}"
    project_name = config.get("project", {}).get("name")
    expected_project = f"{skill_name}-skill"
    if project_name != expected_project:
        return f"{pyproject} names the project {project_name!r}; it must be {expected_project!r}"
    expected_package = _package_name(skill_name)
    packages = (
        config.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages")
    )
    if packages != [expected_package]:
        return (
            f"{pyproject} builds the packages {packages!r}; it must build exactly "
            f"[{expected_package!r}] (`[tool.hatch.build.targets.wheel] packages`)"
        )
    if not (project_dir / expected_package / "__init__.py").is_file():
        return f"{project_dir / expected_package / '__init__.py'} is missing"
    return None


def _run_via_uv(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_UV_TIMEOUT_SECONDS,
        check=False,
    )


def _run_checked(
    argv: Sequence[str], cwd: Path, runner: CommandRunner, what: str
) -> str | None:
    """Run ``argv`` and return an error naming ``what`` when it fails, else None."""
    try:
        result = runner(argv, cwd)
    except subprocess.TimeoutExpired:
        return f"{what} did not finish within {_UV_TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        return f"`uv` was not found on PATH; cannot run {what}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return f"{what} failed (exit {result.returncode}):\n{detail}"
    return None


def check_runnable(skill_dir: Path, runner: CommandRunner = _run_via_uv) -> str | None:
    """Confirm the skill's dependencies are locked and every entry imports and builds its CLI.

    Runs ``uv lock --check`` and then ``uv run --no-sync <entry> --help`` for each
    entry file in ``scripts/``, via ``runner`` (injectable for testing). Returns an
    error message on the first failure, otherwise ``None``. A skill with no
    ``python/`` project is trivially runnable.
    """
    project_dir = (skill_dir / "python").resolve()
    if not project_dir.is_dir():
        return None
    scripts_dir = (skill_dir / "scripts").resolve()
    error = _run_checked(
        ["uv", "lock", "--check"],
        project_dir,
        runner,
        "`uv lock --check` (the workspace lock must include the skill's "
        "dependencies; run `uv lock`, then `uv sync --all-packages`)",
    )
    if error is not None:
        return error
    for entry in _entry_files(scripts_dir) if scripts_dir.is_dir() else []:
        error = _run_checked(
            ["uv", "run", "--no-sync", str(entry), "--help"],
            scripts_dir,
            runner,
            f"`uv run --no-sync {entry} --help` (its imports or dependencies may be broken; "
            "a ModuleNotFoundError for the skill's own package means it is not installed yet: "
            "run `uv sync --all-packages`)",
        )
        if error is not None:
            return error
    return None


def validate(skill_dir: Path) -> str | None:
    """Return an error message if the skill is invalid; otherwise ``None``."""
    if not skill_dir.is_dir():
        return f"skill directory not found: {skill_dir}"
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return f"SKILL.md not found at {skill_md}"

    try:
        frontmatter, body_lines = _split_frontmatter(
            skill_md.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        return str(exc)

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        return "frontmatter.name is missing or empty"
    if not (_MIN_NAME_LEN <= len(name) <= _MAX_NAME_LEN):
        return (
            f"frontmatter.name length must be {_MIN_NAME_LEN}-{_MAX_NAME_LEN} "
            f"characters, got {len(name)}"
        )
    if not _NAME_PATTERN.fullmatch(name):
        return (
            f"frontmatter.name ({name!r}) must match "
            f"^[a-z0-9]+(?:-[a-z0-9]+)*$ -- lowercase letters/digits "
            "separated by single hyphens, no leading/trailing or consecutive hyphens"
        )
    if name != skill_dir.name:
        return (
            f"frontmatter.name ({name!r}) does not match parent directory "
            f"({skill_dir.name!r})"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str):
        return "frontmatter.description is missing or not a string"
    if not (_MIN_DESC_LEN <= len(description) <= _MAX_DESC_LEN):
        return (
            f"frontmatter.description length must be "
            f"{_MIN_DESC_LEN}-{_MAX_DESC_LEN}, got {len(description)}"
        )

    if len(body_lines) > _MAX_BODY_LINES:
        return (
            f"SKILL.md body is {len(body_lines)} lines; spec recommends "
            f"<= {_MAX_BODY_LINES} (use references/ for overflow)"
        )

    return _validate_python_project(skill_dir, name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill_dir",
        type=Path,
        help="Path to the skill directory to validate (e.g. .agents/skills/my-skill)",
    )
    args = parser.parse_args()

    error = validate(args.skill_dir)
    if error is None:
        error = check_runnable(args.skill_dir)
    if error is None:
        print("ok")
        return 0
    print(f"invalid skill: {error}", file=sys.stderr)
    return 1
