"""The root project type-checks clean, and the bare tier type-checks against the standard library alone.

The root ``[tool.ty.src]`` covers the agent skills and their shared scripts, ``system/scripts``
and the root-level tests, checked against the root venv (every other package under ``system/``
runs its own ``test_no_type_errors``).

The bare tier -- what the system ``python3`` runs with no venv -- is checked a second time
against an environment with no third-party packages at all, so an import it cannot make at
runtime is an ``unresolved-import`` here, at the workspace's floor Python version.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from entry_points_testing import (
    BARE_PACKAGES,
    REPO_ROOT,
    STANDALONE_BARE_DIRS,
    STANDALONE_BARE_SCRIPTS,
    entry_files,
    entry_package,
    is_bare_entry,
    is_test_file,
    project_dir,
    scripts_dirs,
)

_TY = Path(sys.executable).parent / "ty"
_FLOOR_PYTHON_VERSION = "3.12"

_SYSTEM_SCRIPTS = REPO_ROOT / "system" / "scripts"
_UPDATE_SELF_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "update-self" / "scripts"
_UPDATE_SELF_PYTHON = REPO_ROOT / ".agents" / "skills" / "update-self" / "python"
_TK_COMMAND_PARSING_SRC = REPO_ROOT / "system" / "libs" / "tk_command_parsing" / "src"
_OOM_PRIORITY_SRC = REPO_ROOT / "system" / "services" / "oom_priority" / "src"

# Where the bare tier's imports resolve from at runtime: system/scripts (the stubs' own
# directory), update-self's python/ and the stdlib-only library trees, which the stubs and
# update-self put on sys.path.
_BARE_SEARCH_PATHS = (
    _SYSTEM_SCRIPTS,
    _UPDATE_SELF_PYTHON,
    _TK_COMMAND_PARSING_SRC,
    _OOM_PRIORITY_SRC,
)


def _is_test_support(path: Path) -> bool:
    return is_test_file(path) or path.name.endswith("testing.py")


def _bare_packages(stubs: list[Path]) -> set[Path]:
    return {
        project_dir(stub) / package
        for stub in stubs
        if (package := entry_package(stub)) is not None
    }


def _bare_files() -> list[Path]:
    stubs = [
        entry
        for directory in scripts_dirs()
        for entry in entry_files(directory)
        if is_bare_entry(entry)
    ]
    stubs += [
        entry for directory in STANDALONE_BARE_DIRS for entry in entry_files(directory)
    ]
    stubs += STANDALONE_BARE_SCRIPTS
    packages = _bare_packages(stubs)
    assert {package.name for package in packages} == BARE_PACKAGES
    trees = [*packages, _TK_COMMAND_PARSING_SRC, _OOM_PRIORITY_SRC]
    modules = [
        path
        for tree in trees
        for path in sorted(tree.rglob("*.py"))
        if not _is_test_support(path)
    ]
    return sorted({*stubs, *modules})


def _run_ty(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_TY), "check", "--output-format", "concise", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _empty_environment(tmp_path: Path) -> Path:
    environment = tmp_path / "bare-env"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        timeout=120,
    )
    return environment


def _check_bare(
    files: list[Path], search_paths: tuple[Path, ...], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "--python",
        str(_empty_environment(tmp_path)),
        "--python-version",
        _FLOOR_PYTHON_VERSION,
    ]
    for search_path in search_paths:
        arguments += ["--extra-search-path", str(search_path)]
    return _run_ty([*arguments, *(str(path) for path in files)])


@pytest.mark.timeout(600)
def test_the_root_project_has_no_type_errors() -> None:
    result = _run_ty([])
    assert result.returncode == 0, (
        f"ty found problems in the root project:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.timeout(600)
def test_the_bare_tier_type_checks_against_the_standard_library_alone(
    tmp_path: Path,
) -> None:
    files = _bare_files()
    assert _SYSTEM_SCRIPTS / "forward_port.py" in files
    assert _UPDATE_SELF_SCRIPTS / "update_self.py" in files
    assert set(STANDALONE_BARE_SCRIPTS) <= set(files)

    result = _check_bare(files, _BARE_SEARCH_PATHS, tmp_path)

    assert result.returncode == 0, (
        "The bare tier (run by the system python3 with no venv) has type errors, or imports "
        f"something outside the standard library:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.timeout(600)
def test_the_bare_check_rejects_a_third_party_import_and_accepts_the_floor_syntax(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    leaky = scripts / "leaky.py"
    leaky.write_text("import yaml\n\nyaml.safe_dump({})\n")
    modern = scripts / "modern.py"
    # A PEP 695 type alias: syntax that exists from Python 3.12, the floor.
    modern.write_text(
        "type Pair = tuple[int, int]\n\n\ndef first(pair: Pair) -> int:\n    return pair[0]\n"
    )

    result = _check_bare([leaky, modern], (scripts,), tmp_path)

    assert result.returncode != 0
    assert "unresolved-import" in result.stdout
    assert "leaky.py" in result.stdout
    assert "modern.py" not in result.stdout
