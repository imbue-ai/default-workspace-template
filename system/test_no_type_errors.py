"""The root project type-checks clean, and the bare tier type-checks against the standard library alone.

The root ``[tool.ty.src]`` covers the agent skills and their shared scripts, ``system/scripts``
and the root-level tests, checked against the root venv (every other package under ``system/``
runs its own ``test_no_type_errors``).

The bare tier -- what the system ``python3`` runs with no venv -- is checked a second time
against an environment with no third-party packages at all, so an import it cannot make at
runtime is an ``unresolved-import`` here, at the workspace's floor Python version.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from entry_points_testing import (
    BARE_PACKAGES,
    REPO_ROOT,
    STANDALONE_BARE_SCRIPTS,
    SYSTEM_SCRIPTS,
    UPDATE_SELF_SCRIPTS,
    all_entry_points,
    entry_package,
    is_bare_entry,
    is_test_file,
    project_dir,
)

_TY = Path(sys.executable).parent / "ty"
_FLOOR_PYTHON_VERSION = "3.12"

_UPDATE_SELF_DIR = UPDATE_SELF_SCRIPTS.parent
_UPDATE_SELF_PYTHON = _UPDATE_SELF_DIR / "python"
_TK_COMMAND_PARSING_SRC = REPO_ROOT / "system" / "libs" / "tk_command_parsing" / "src"
_OOM_PRIORITY_SRC = REPO_ROOT / "system" / "services" / "oom_priority" / "src"

# Where the bare tier's imports resolve from at runtime: system/scripts (the stubs' own
# directory), update-self's python/ and the stdlib-only library trees, which the stubs and
# update-self put on sys.path.
_BARE_SEARCH_PATHS = (
    SYSTEM_SCRIPTS,
    _UPDATE_SELF_PYTHON,
    _TK_COMMAND_PARSING_SRC,
    _OOM_PRIORITY_SRC,
)


# The first-party modules each bare runtime context has on sys.path. update-self runs as a
# staged copy that puts only its own python/ there (and loads oom_priority off the target
# tree); everything else runs beside system/scripts with the stdlib-only library trees.
_UPDATE_SELF_IMPORTABLE = frozenset({"update_self_skill", "oom_priority"})
_SYSTEM_IMPORTABLE = frozenset(
    {"workspace_bare_scripts", "tk_command_parsing", "oom_priority"}
)


def _bare_importable(path: Path) -> frozenset[str]:
    """The first-party modules on ``sys.path`` in the bare runtime context ``path`` runs in."""
    if path.is_relative_to(_UPDATE_SELF_DIR):
        return _UPDATE_SELF_IMPORTABLE
    return _SYSTEM_IMPORTABLE


def _imports_outside_the_bare_tier(path: Path, importable: frozenset[str]) -> list[str]:
    """Top-level modules ``path`` imports (anywhere, lazily included) beyond the stdlib and ``importable``.

    ty alone does not catch a first-party one: it resolves a sibling package such as the
    venv-tier ``workspace_scripts`` relative to the importing file, and it checks every bare
    file against every bare search path, so it never reports either.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    return sorted(imported - sys.stdlib_module_names - importable)


def _is_test_support(path: Path) -> bool:
    return is_test_file(path) or path.name.endswith("testing.py")


def _bare_packages(stubs: list[Path]) -> set[Path]:
    return {
        project_dir(stub) / package
        for stub in stubs
        if (package := entry_package(stub)) is not None
    }


def _bare_files() -> list[Path]:
    stubs = [entry for entry in all_entry_points() if is_bare_entry(entry)]
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
    assert SYSTEM_SCRIPTS / "forward_port.py" in files
    assert UPDATE_SELF_SCRIPTS / "update_self.py" in files
    assert set(STANDALONE_BARE_SCRIPTS) <= set(files)

    result = _check_bare(files, _BARE_SEARCH_PATHS, tmp_path)

    assert result.returncode == 0, (
        "The bare tier (run by the system python3 with no venv) has type errors, or imports "
        f"something outside the standard library:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.timeout(600)
def test_the_bare_check_rejects_a_third_party_import_and_holds_to_the_floor(
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
    newer = scripts / "newer.py"
    # base64.z85encode exists only from Python 3.13, past the floor.
    newer.write_text(
        "import base64\n\n\ndef encode(data: bytes) -> bytes:\n    return base64.z85encode(data)\n"
    )

    result = _check_bare([leaky, modern, newer], (scripts,), tmp_path)

    assert result.returncode != 0
    assert "unresolved-import" in result.stdout
    assert "leaky.py" in result.stdout
    assert "modern.py" not in result.stdout
    assert "has no member `z85encode`" in result.stdout


def test_the_bare_tier_imports_only_the_stdlib_and_bare_code() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): outside
        for path in _bare_files()
        if (outside := _imports_outside_the_bare_tier(path, _bare_importable(path)))
    }
    assert offenders == {}, (
        "Bare-tier files import modules the system python3 cannot load with no venv: "
        f"{offenders}"
    )


def test_the_bare_import_rule_rejects_a_lazy_venv_tier_import(tmp_path: Path) -> None:
    module = tmp_path / "lazy.py"
    module.write_text(
        "import json\n\n\ndef render() -> str:\n"
        "    from workspace_scripts import docs_viewer\n\n    return json.dumps(str(docs_viewer))\n"
    )

    assert _imports_outside_the_bare_tier(module, _bare_importable(module)) == [
        "workspace_scripts"
    ]


def test_the_bare_import_rule_keeps_update_self_to_its_staged_copy(
    tmp_path: Path,
) -> None:
    module = tmp_path / "mirror.py"
    module.write_text(
        "from workspace_bare_scripts import tool_env\n\ntool_env.main()\n"
    )
    update_self_module = _UPDATE_SELF_PYTHON / "update_self_skill" / "update_self.py"

    assert _imports_outside_the_bare_tier(
        module, _bare_importable(update_self_module)
    ) == ["workspace_bare_scripts"]
    assert _imports_outside_the_bare_tier(module, _bare_importable(module)) == []
