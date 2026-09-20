"""The scripts documentation runs with a bare ``python3`` import only the standard library.

Inside a workspace ``python3`` is the system interpreter, which has none of the root venv's
packages: a script that imports one runs fine under ``uv run`` and in these tests, and fails
with ``ModuleNotFoundError`` the moment an agent or a supervisord program line calls it as
documented. The list below is every script the docs, skills, prompts, and program lines invoke
that way (``grep -rho "python3 system/scripts/[a-z_]*\\.py"``); a script may import a sibling
script, which is then held to the same rule.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent

_SCRIPTS_RUN_WITH_SYSTEM_PYTHON = (
    "collect_bug_report_diagnostics.py",
    "forward_port.py",
    "install_mngr.py",
    "layout.py",
    "message_chat.py",
    "provision_backups.py",
    "refresh_workspace_view.py",
    "require_create_account.py",
    "seed_welcome_chat.py",
    "tool_env.py",
    "welcome_count.py",
)


def _imported_top_level_modules(script: Path) -> set[str]:
    """The first segment of every module the script imports at any depth (a function-local import runs too)."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(script.read_text(), filename=str(script))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            modules.add(node.module.split(".")[0])
    return modules


def _modules_outside_the_stdlib(script: Path, checked: set[Path]) -> set[str]:
    """Every imported module that is neither standard library nor a sibling script that passes this same check."""
    checked.add(script)
    offenders: set[str] = set()
    for module in _imported_top_level_modules(script):
        if module in sys.stdlib_module_names:
            continue
        sibling = script.parent / f"{module}.py"
        if not sibling.is_file():
            offenders.add(module)
        elif sibling not in checked:
            offenders.update(
                f"{module} -> {nested}"
                for nested in _modules_outside_the_stdlib(sibling, checked)
            )
    return offenders


@pytest.mark.parametrize("script_name", _SCRIPTS_RUN_WITH_SYSTEM_PYTHON)
def test_a_script_run_with_the_system_python_imports_only_the_standard_library(
    script_name: str,
) -> None:
    script = _SCRIPTS_DIR / script_name
    assert script.is_file(), (
        f"{script_name} is listed but does not exist beside this test"
    )
    offenders = _modules_outside_the_stdlib(script, set())
    assert offenders == set(), (
        f"{script_name} runs under the system python3, which cannot import {sorted(offenders)}; "
        "use the standard library (tomllib, json, urllib) instead"
    )


def test_the_check_sees_a_third_party_import_wherever_it_is(tmp_path: Path) -> None:
    script = tmp_path / "leaky.py"
    script.write_text(
        "import json\n\n\ndef run() -> None:\n    import yaml\n\n    yaml.safe_dump({})\n"
    )
    assert _modules_outside_the_stdlib(script, set()) == {"yaml"}
    sibling = tmp_path / "helper.py"
    sibling.write_text("import tomlkit\n")
    (tmp_path / "caller.py").write_text("from helper import x\n")
    assert _modules_outside_the_stdlib(tmp_path / "caller.py", set()) == {
        "helper -> tomlkit"
    }
