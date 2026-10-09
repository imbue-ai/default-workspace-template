"""The bare package, which the system ``python3`` runs, imports only the standard library.

Inside a workspace ``python3`` is the system interpreter, which has none of the root venv's
packages: a module that imports one runs fine under ``uv run`` and in these tests, and fails
with ``ModuleNotFoundError`` the moment an agent, a hook or a supervisord program line runs its
entry script as documented. Every module of ``workspace_bare_scripts`` is held to that; the
stdlib-only libraries the hook entry scripts put on ``sys.path`` count as standard library.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_BARE_PACKAGE_DIR = Path(__file__).parent / "workspace_bare_scripts"
# The bare package itself, and the stdlib-only libraries hook entry scripts put on sys.path.
_ALSO_ALLOWED = frozenset({"workspace_bare_scripts", "tk_command_parsing", "oom_priority"})

_BARE_MODULES = sorted(
    path.name
    for path in _BARE_PACKAGE_DIR.glob("*.py")
    if not path.name.endswith("_test.py") and path.name != "__init__.py"
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


def _modules_outside_the_stdlib(script: Path, checked: frozenset[Path] = frozenset()) -> set[str]:
    """Every imported module that is neither standard library nor a sibling script that passes this same check.

    ``checked`` is the chain of scripts that led here, so an import cycle between siblings ends.
    """
    visited = checked | {script}
    offenders: set[str] = set()
    for module in _imported_top_level_modules(script):
        if module in sys.stdlib_module_names or module in _ALSO_ALLOWED:
            continue
        sibling = script.parent / f"{module}.py"
        if not sibling.is_file():
            offenders.add(module)
        elif sibling not in visited:
            offenders.update(
                f"{module} -> {nested}"
                for nested in _modules_outside_the_stdlib(sibling, visited)
            )
    return offenders


@pytest.mark.parametrize("module_name", _BARE_MODULES)
def test_a_bare_module_imports_only_the_standard_library(module_name: str) -> None:
    offenders = _modules_outside_the_stdlib(_BARE_PACKAGE_DIR / module_name)
    assert offenders == set(), (
        f"{module_name} runs under the system python3, which cannot import {sorted(offenders)}; "
        "use the standard library (tomllib, json, urllib) instead"
    )


def test_the_check_sees_a_third_party_import_wherever_it_is(tmp_path: Path) -> None:
    script = tmp_path / "leaky.py"
    script.write_text(
        "import json\n\n\ndef run() -> None:\n    import yaml\n\n    yaml.safe_dump({})\n"
    )
    assert _modules_outside_the_stdlib(script) == {"yaml"}
    sibling = tmp_path / "helper.py"
    sibling.write_text("import tomlkit\n")
    (tmp_path / "caller.py").write_text("from helper import x\n")
    assert _modules_outside_the_stdlib(tmp_path / "caller.py") == {
        "helper -> tomlkit"
    }
