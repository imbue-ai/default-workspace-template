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
  a `scripts/` dir of shell scripts needs nothing), it is either:
  - a bare skill: one standard-library-only script, `scripts/<name_with_underscores>.py`
    (plus, optionally, its test `scripts/<name_with_underscores>_test.py`), with no
    PEP 723 header and no `python/` dir; or
  - a packaged skill: `python/pyproject.toml` exists and names the project
    `<name>-skill` and its one package `<name_with_underscores>_skill` (a directory
    beside it with an `__init__.py`).

Runnability checks (only when the static checks pass and the skill has Python):

- A bare skill's script: `python3 -S -s <script> --help` exits 0, run with no
  site-packages, the way the system `python3` runs it.
- A packaged skill: `uv lock --check` passes (the workspace lock includes the
  skill's dependencies, so `uv sync --all-packages` installs them), and
  `uv run --no-sync <entry> --help` exits 0 for every entry file -- each module of
  the package with a top-level `if __name__ == "__main__":` block, and any stub at
  the top of `scripts/` -- so a broken import or a missing dependency is caught here rather
  than only at scenario time. A package with a module run by its path holds no
  module named like a standard-library one, which that run would import instead.

`--help` is a shallow import check: it exercises the top-level imports and argparse
wiring, not imports done lazily inside subcommand bodies -- those are left to
scenario testing.

Exits 0 and prints `ok` when valid; exits 1 with a human-readable error to
stderr otherwise.
"""

from __future__ import annotations

import argparse
import ast
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
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

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


def _skill_metadata(skill_dir: Path) -> dict[str, Any]:
    """The ``metadata`` mapping of the skill's SKILL.md frontmatter; empty when the SKILL.md is
    missing or its frontmatter does not parse (``validate`` reports that)."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return {}
    try:
        frontmatter, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    metadata = frontmatter.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def is_built_in_skill(skill_dir: Path) -> bool:
    """Whether the skill ships with the template: its frontmatter has ``metadata.author: imbue``."""
    return _skill_metadata(skill_dir).get("author") == "imbue"


# Beside ``skills/`` in ``.agents``: the skills installed or copied in from elsewhere, by name,
# each with where it came from. The workspace keeps them as they came, so its checks leave
# them out.
THIRD_PARTY_SKILLS_FILE_NAME = "third-party-skills.toml"


def third_party_skills(agents_dir: Path) -> dict[str, str]:
    """The skills ``.agents/third-party-skills.toml`` lists, mapped to where each came from."""
    listing = agents_dir / THIRD_PARTY_SKILLS_FILE_NAME
    if not listing.is_file():
        return {}
    skills = tomllib.loads(listing.read_text(encoding="utf-8")).get("skills", {})
    if not isinstance(skills, dict) or not all(
        isinstance(source, str) and source for source in skills.values()
    ):
        raise ValueError(
            f"{listing}: [skills] must map each skill's name to where it came from, a "
            'non-empty string (pdf = "https://github.com/anthropics/skills")'
        )
    return skills


def is_third_party_skill(skill_dir: Path) -> bool:
    """Whether ``.agents/third-party-skills.toml`` lists the skill, so the checks leave it out."""
    return skill_dir.name in third_party_skills(skill_dir.parent.parent)


def package_name(skill_name: str) -> str:
    return skill_name.replace("-", "_") + "_skill"


def _is_test_file(path: Path) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def is_entry_module(path: Path) -> bool:
    """Whether ``path`` is run as a script: it has a top-level ``if __name__ == "__main__":``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.If) and _is_main_guard(node.test) for node in tree.body
    )


def package_entry_modules(project_dir: Path) -> list[Path]:
    """The modules of a uv project's packages that run as scripts, tests excluded."""
    return sorted(
        module
        for init in project_dir.glob("*/__init__.py")
        for module in init.parent.glob("*.py")
        if not _is_test_file(module) and is_entry_module(module)
    )


def _script_stubs(skill_dir: Path) -> list[Path]:
    """Every ``.py`` at the top of ``scripts/`` except tests: stubs for a path called from outside."""
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    return sorted(path for path in scripts_dir.glob("*.py") if not _is_test_file(path))


def stdlib_shadowing_modules(package_dir: Path) -> list[str]:
    """The package's modules named like a standard-library module.

    A module run by its own path has its package's directory first on ``sys.path``,
    so such a sibling would be imported in place of the standard-library module.
    """
    return sorted(
        module.stem
        for module in package_dir.glob("*.py")
        if module.stem in sys.stdlib_module_names
    )


def _entry_files(skill_dir: Path) -> list[Path]:
    """The entry points: the package's modules that run as scripts, then the stubs."""
    project_dir = skill_dir / "python"
    modules = package_entry_modules(project_dir) if project_dir.is_dir() else []
    return [*modules, *_script_stubs(skill_dir)]


def bare_skill_module(skill_dir: Path) -> Path | None:
    """The script of a bare skill: one ``scripts/<name_with_underscores>.py``, no ``python/``.

    Its own test, ``scripts/<name_with_underscores>_test.py``, may sit beside it; any other
    ``.py`` there makes the skill a packaged one.
    """
    scripts_dir = skill_dir / "scripts"
    if (skill_dir / "python").exists() or not scripts_dir.is_dir():
        return None
    module_name = skill_dir.name.replace("-", "_")
    python_files = {path.name for path in scripts_dir.glob("*.py")}
    if f"{module_name}.py" not in python_files or not python_files <= {
        f"{module_name}.py",
        f"{module_name}_test.py",
    }:
        return None
    return scripts_dir / f"{module_name}.py"


def _imported_top_level_names(source: str, filename: str) -> set[str]:
    """Every module a source imports, at any depth, by its top-level name.

    A relative import has no package to resolve against in a bare script, so it is
    reported as ``.``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add("." if node.level else (node.module or "").split(".")[0])
    return names


def _validate_bare_skill(module: Path) -> str | None:
    """A bare script imports only the standard library and carries no PEP 723 header."""
    if module.stem in sys.stdlib_module_names:
        return (
            f"{module} would shadow the standard-library module {module.stem!r} when it "
            "runs by its own path; rename the skill"
        )
    source = module.read_text(encoding="utf-8")
    if re.search(r"^# /// script\s*$", source, re.MULTILINE):
        return (
            f"{module} has a PEP 723 header: a bare skill's script runs under the system "
            "python3 with no environment of its own, so drop the header (a script that "
            "needs dependencies belongs in a python/ project instead)"
        )
    try:
        imported = _imported_top_level_names(source, str(module))
    except SyntaxError as exc:
        return f"{module} cannot be parsed: {exc}"
    outside = sorted(
        name
        for name in imported
        if name not in sys.stdlib_module_names and name != "__future__"
    )
    if outside:
        return (
            f"{module} imports outside the standard library ({', '.join(outside)}): a "
            "bare skill's script runs under the system python3 with no venv. Move the "
            "skill's Python into a python/ project (see "
            ".agents/shared/references/spec-summary.md, Packaging)"
        )
    return None


def _validate_python_project(skill_dir: Path, skill_name: str) -> str | None:
    """If the skill has Python, require it to be a bare skill or a ``python/`` uv project
    named for the skill.

    A skill with no entry files in ``scripts/`` and no ``python/`` dir is OK.
    """
    project_dir = skill_dir / "python"
    if not _script_stubs(skill_dir) and not project_dir.is_dir():
        return None
    bare_module = bare_skill_module(skill_dir)
    if bare_module is not None:
        return _validate_bare_skill(bare_module)
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        module_name = skill_name.replace("-", "_")
        return (
            f"{pyproject} is missing: a skill's Python lives in its python/ uv project, "
            "a workspace member that needs one, unless it is a single standard-library "
            f"script, scripts/{module_name}.py (see "
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
    expected_package = package_name(skill_name)
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
        return f"`{argv[0]}` was not found on PATH; cannot run {what}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return f"{what} failed (exit {result.returncode}):\n{detail}"
    return None


def check_runnable(skill_dir: Path, runner: CommandRunner = _run_via_uv) -> str | None:
    """Confirm the skill's dependencies are locked and every entry imports and builds its CLI.

    A bare skill's script runs as ``python3 -S -s <script> --help``. A packaged skill
    runs ``uv lock --check`` and then ``uv run --no-sync <entry> --help`` for each
    entry file. Commands go through ``runner`` (injectable for testing). Returns an
    error message on the first failure, otherwise ``None``. A skill with neither is
    trivially runnable.
    """
    bare_module = bare_skill_module(skill_dir)
    if bare_module is not None:
        module = bare_module.resolve()
        return _run_checked(
            ["python3", "-S", "-s", str(module), "--help"],
            module.parent,
            runner,
            f"`python3 {module} --help` with no site-packages (it may import something "
            "outside the standard library, or be broken)",
        )
    project_dir = (skill_dir / "python").resolve()
    if not project_dir.is_dir():
        return None
    error = _run_checked(
        ["uv", "lock", "--check"],
        project_dir,
        runner,
        "`uv lock --check` (the workspace lock must include the skill's "
        "dependencies; run `uv lock`, then `uv sync --all-packages`)",
    )
    if error is not None:
        return error
    try:
        entries = _entry_files(skill_dir.resolve())
    except SyntaxError as exc:
        return f"{exc.filename} cannot be parsed: {exc}"
    for entry in entries:
        is_module = (entry.parent / "__init__.py").is_file()
        shadowing = stdlib_shadowing_modules(entry.parent) if is_module else []
        if shadowing:
            return (
                f"{entry} runs by its own path, so its package's {', '.join(shadowing)} "
                "would shadow the standard-library module of that name; rename it"
            )
        error = _run_checked(
            ["uv", "run", "--no-sync", str(entry), "--help"],
            entry.parent,
            runner,
            f"`uv run --no-sync {entry} --help` (its imports or dependencies may be broken)",
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
    if not NAME_PATTERN.fullmatch(name):
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


if __name__ == "__main__":
    sys.exit(main())
