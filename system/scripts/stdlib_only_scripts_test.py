"""The scripts documentation runs with a bare ``python3`` import only the standard library.

Inside a workspace ``python3`` is the system interpreter, which has none of the root venv's
packages: a script that imports one runs fine under ``uv run`` and in these tests, and fails
with ``ModuleNotFoundError`` the moment an agent or a supervisord program line calls it as
documented. The list below is every script the docs, skills, prompts, program lines, and shell
scripts invoke that way (``grep -rho "python3 system/scripts/[a-z_]*\\.py"``, and the shell
scripts' ``python3 "$REPO_ROOT/system/scripts/<name>.py"`` spelling), plus the shared agent
scripts outside ``system/scripts/`` the skills run that way; a script may import a sibling
script, which is then held to the same rule.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent

_SCRIPTS_RUN_WITH_SYSTEM_PYTHON = (
    "background_tasks.py",
    "claude_background_task_hooks.py",
    "collect_bug_report_diagnostics.py",
    "forward_port.py",
    "install_mngr.py",
    "list_mngr_plugins.py",
    "message_chat.py",
    "provision_backups.py",
    "refresh_workspace_view.py",
    "require_create_account.py",
    "run_in_background.py",
    "seed_welcome_chat.py",
    "set_mngr_pin.py",
    "tool_env.py",
    "vendor_mngr_skill.py",
)

_OTHER_SCRIPTS_RUN_WITH_SYSTEM_PYTHON = (
    _SCRIPTS_DIR.parents[1]
    / ".agents"
    / "shared"
    / "scripts"
    / "serve_isolated_instance.py",
)

# The stdlib-only workspace libraries a script may put on ``sys.path`` and import, by package,
# each held to the same rule: the package directory, or the one module of it scripts import
# where the rest of the package (a click command) runs only under ``uv run``.
_STDLIB_ONLY_LIBRARIES = {
    "chat_background_tasks": _SCRIPTS_DIR.parent
    / "libs"
    / "chat_background_tasks"
    / "src"
    / "chat_background_tasks",
    "disk_headroom": _SCRIPTS_DIR.parent
    / "libs"
    / "disk_headroom"
    / "src"
    / "disk_headroom"
    / "space.py",
    "oom_priority": _SCRIPTS_DIR.parent
    / "services"
    / "oom_priority"
    / "src"
    / "oom_priority",
}


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


def _modules_outside_the_stdlib(
    script: Path, checked: frozenset[Path] = frozenset()
) -> set[str]:
    """Every imported module that is neither standard library nor a sibling script that passes this same check.

    ``checked`` is the chain of scripts that led here, so an import cycle between siblings ends.
    """
    visited = checked | {script}
    offenders: set[str] = set()
    for module in _imported_top_level_modules(script):
        if module in sys.stdlib_module_names:
            continue
        if module in _STDLIB_ONLY_LIBRARIES:
            offenders.update(
                f"{module} -> {nested}"
                for nested in _library_modules_outside_the_stdlib(module)
            )
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


def _library_modules_outside_the_stdlib(library: str) -> set[str]:
    """Every module a stdlib-only library's own (non-test) code imports that is neither standard
    library nor the library itself."""
    location = _STDLIB_ONLY_LIBRARIES[library]
    sources = [location] if location.is_file() else list(location.glob("*.py"))
    return {
        module
        for source in sources
        if not source.name.endswith("_test.py") and source.name != "conftest.py"
        for module in _imported_top_level_modules(source)
        if module not in sys.stdlib_module_names and module != library
    }


@pytest.mark.parametrize(
    "script",
    [
        *(_SCRIPTS_DIR / name for name in _SCRIPTS_RUN_WITH_SYSTEM_PYTHON),
        *_OTHER_SCRIPTS_RUN_WITH_SYSTEM_PYTHON,
    ],
    ids=lambda script: script.name,
)
def test_a_script_run_with_the_system_python_imports_only_the_standard_library(
    script: Path,
) -> None:
    assert script.is_file(), f"{script} is listed but does not exist"
    offenders = _modules_outside_the_stdlib(script)
    assert offenders == set(), (
        f"{script.name} runs under the system python3, which cannot import {sorted(offenders)}; "
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
    assert _modules_outside_the_stdlib(tmp_path / "caller.py") == {"helper -> tomlkit"}
