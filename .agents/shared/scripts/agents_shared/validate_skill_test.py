"""Tests for ``validate_skill.py``.

Run via: ``uv run --no-sync pytest
.agents/shared/scripts/agents_shared/validate_skill_test.py``
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agents_shared import validate_skill

_REPO_ROOT = Path(__file__).resolve().parents[4]

_PYPROJECT = """\
[project]
name = "{project}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]
"""


def _write_skill(
    base: Path,
    name: str,
    description: str = "Valid description",
    body_lines: int = 5,
    metadata_crystallized: bool = False,
    include_scripts: bool | None = None,
    frontmatter_override: str | None = None,
) -> Path:
    """Build a skill directory on disk; return the skill path.

    With scripts, the skill is laid out the way the spec asks: a ``python/`` project
    whose ``pyproject.toml`` names ``<name>-skill`` and its package, and a
    ``scripts/run.py`` stub.
    """
    skill = base / name
    skill.mkdir(parents=True)
    meta = "\nmetadata:\n  crystallized: true" if metadata_crystallized else ""
    fm = (
        frontmatter_override
        if frontmatter_override is not None
        else (f"---\nname: {name}\ndescription: {description}{meta}\n---\n")
    )
    body = "\n".join(f"line {i}" for i in range(body_lines))
    (skill / "SKILL.md").write_text(fm + body)
    if include_scripts is None:
        include_scripts = metadata_crystallized
    if include_scripts:
        package = name.replace("-", "_") + "_skill"
        project = skill / "python"
        (project / package).mkdir(parents=True)
        (project / package / "__init__.py").write_text("")
        (project / "pyproject.toml").write_text(
            _PYPROJECT.format(project=f"{name}-skill", package=package)
        )
        (skill / "scripts").mkdir()
        (skill / "scripts" / "run.py").write_text(f"from {package} import cli\n")
    return skill


def test_valid_skill(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill")
    assert validate_skill.validate(skill) is None


def test_name_mismatch(tmp_path: Path) -> None:
    skill = tmp_path / "dirname"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: wrong\ndescription: x\n---\nbody\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "does not match parent directory" in error


def test_missing_description(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\n---\nbody\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "description" in error


def test_description_too_long(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", description="x" * 2000)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "length" in error


def test_body_too_long(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", body_lines=600)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "500" in error


def test_crystallized_without_scripts_is_ok(tmp_path: Path) -> None:
    """Crystallized skills do not require scripts -- pure-prose skills are valid."""
    skill = _write_skill(
        tmp_path, "s", metadata_crystallized=True, include_scripts=False
    )
    assert validate_skill.validate(skill) is None


def test_packaged_scripts_are_valid(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill", include_scripts=True)
    assert validate_skill.validate(skill) is None


def test_scripts_without_a_pyproject_are_invalid(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill", include_scripts=True)
    (skill / "python" / "pyproject.toml").unlink()
    error = validate_skill.validate(skill)
    assert error is not None
    assert "pyproject.toml is missing" in error


def test_a_project_not_named_for_the_skill_is_invalid(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill", include_scripts=True)
    (skill / "python" / "pyproject.toml").write_text(
        _PYPROJECT.format(project="my-scripts", package="my_skill_skill")
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "'my-skill-skill'" in error


def test_a_package_not_named_for_the_skill_is_invalid(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill", include_scripts=True)
    (skill / "python" / "pyproject.toml").write_text(
        _PYPROJECT.format(project="my-skill-skill", package="helpers")
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "'my_skill_skill'" in error


def test_a_shell_only_scripts_dir_needs_no_python_project(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill")
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("echo hi\n")
    assert validate_skill.validate(skill) is None

    (scripts / "run.py").write_text("print('hi')\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "python/pyproject.toml is missing" in error


def test_missing_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("no frontmatter here\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "frontmatter" in error


def test_malformed_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: x\n")  # no closing ---
    error = validate_skill.validate(skill)
    assert error is not None


def test_missing_skill_md(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    error = validate_skill.validate(skill)
    assert error is not None
    assert "SKILL.md" in error


def test_missing_directory(tmp_path: Path) -> None:
    error = validate_skill.validate(tmp_path / "does-not-exist")
    assert error is not None


@pytest.mark.parametrize(
    "bad_name",
    [
        "Bad-Name",  # uppercase
        "bad_name",  # underscore
        "-leading",  # leading hyphen
        "trailing-",  # trailing hyphen
        "double--hyphen",  # consecutive hyphens
    ],
)
def test_invalid_name_format(tmp_path: Path, bad_name: str) -> None:
    """frontmatter.name must match the kebab-case rules even if dir matches."""
    skill = _write_skill(tmp_path, bad_name)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "lowercase letters/digits" in error


def test_name_too_long(tmp_path: Path) -> None:
    long_name = "a" * 65
    skill = _write_skill(tmp_path, long_name)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "length" in error


# --- Runnability check (check_runnable) -------------------------------------


class _RecordingRunner:
    """Answers each command with the result its argv prefix is mapped to (exit 0 otherwise)."""

    def __init__(
        self,
        failures: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.failures = failures or {}
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        for prefix, result in self.failures.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="", stderr=""
        )


def _failed(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr=stderr)


def test_check_runnable_without_scripts(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=False)
    runner = _RecordingRunner()
    assert validate_skill.check_runnable(skill, runner=runner) is None
    assert runner.calls == []


def test_check_runnable_checks_the_lock_then_runs_every_entry(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=True)
    scripts = (skill / "scripts").resolve()
    (scripts / "extra.py").write_text("")
    (scripts / "conftest.py").write_text("")
    (skill / "python" / "s_skill" / "cli_test.py").write_text("")
    runner = _RecordingRunner()

    assert validate_skill.check_runnable(skill, runner=runner) is None
    assert runner.calls == [
        ["uv", "lock", "--check"],
        ["uv", "run", "--no-sync", str(scripts / "extra.py"), "--help"],
        ["uv", "run", "--no-sync", str(scripts / "run.py"), "--help"],
    ]


def test_check_runnable_reports_a_stale_lock(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=True)
    runner = _RecordingRunner(
        {("uv", "lock"): _failed("The lockfile needs to be updated")}
    )

    error = validate_skill.check_runnable(skill, runner=runner)

    assert error is not None
    assert "uv lock --check" in error
    assert "The lockfile needs to be updated" in error


def test_check_runnable_reports_an_entry_that_cannot_import(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=True)
    runner = _RecordingRunner(
        {
            ("uv", "run"): _failed(
                "ModuleNotFoundError: No module named 'missing_dependency'"
            )
        }
    )

    error = validate_skill.check_runnable(skill, runner=runner)

    assert error is not None
    assert "run.py --help" in error
    assert "exit 2" in error
    assert "missing_dependency" in error


def test_check_runnable_timeout(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=True)

    def _timeout(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="uv", timeout=1)

    error = validate_skill.check_runnable(skill, runner=_timeout)
    assert error is not None
    assert "did not finish" in error


def test_check_runnable_uv_missing(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_scripts=True)

    def _no_uv(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    error = validate_skill.check_runnable(skill, runner=_no_uv)
    assert error is not None
    assert "`uv` was not found" in error


_SKILLS_WITH_PYTHON = sorted(
    path.parent for path in (_REPO_ROOT / ".agents" / "skills").glob("*/python")
)


# Real `uv lock --check` and `uv run --no-sync <entry> --help` runs against the workspace:
# a cold lock check resolves git sources, far past the suite's 10s default.
@pytest.mark.timeout(300)
@pytest.mark.parametrize("skill", _SKILLS_WITH_PYTHON, ids=lambda path: path.name)
def test_every_built_in_skill_with_python_is_runnable(skill: Path) -> None:
    assert validate_skill.check_runnable(skill) is None
