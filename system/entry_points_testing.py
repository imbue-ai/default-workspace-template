"""Where the workspace's Python entry points are, and which interpreter each one runs under.

An entry point is either a module of a package with a top-level ``if __name__ == "__main__":``
block, run by its own path, or a stub: a top-level ``.py`` file in a scripts directory
(``system/scripts``, ``.agents/shared/scripts``, ``.agents/skills/<name>/scripts``) that imports
its package, kept where something outside the release calls a fixed path or the script runs with
no venv and imports its own package. An entry's tier is its package's -- an entry into a bare
package runs under the system ``python3`` with no venv (stdlib only), any other runs from the
root venv (``uv run --no-sync``) -- except for the bare modules of venv packages listed below,
a bare skill's single script, and a few standalone stdlib scripts outside the scripts
directories. See .agents/shared/references/running-python.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agents_shared.validate_skill import bare_skill_module, package_entry_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SCRIPTS = REPO_ROOT / "system" / "scripts"
UPDATE_SELF_SCRIPTS = REPO_ROOT / ".agents" / "skills" / "update-self" / "scripts"

# The packages the system python3 runs: stdlib only, no venv.
BARE_PACKAGES = frozenset({"workspace_bare_scripts", "update_self_skill"})

# Modules of venv packages that import only the standard library and the bare packages, so
# they run under the system python3 too. One that needs a bare package puts its root on
# sys.path itself when it runs by its path.
BARE_MODULES = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "notify-user"
    / "python"
    / "notify_user_skill"
    / "notify_user.py",
    REPO_ROOT
    / ".agents"
    / "skills"
    / "connect-external-service"
    / "python"
    / "connect_external_service_skill"
    / "request_secret.py",
    REPO_ROOT
    / ".agents"
    / "shared"
    / "scripts"
    / "agents_shared"
    / "serve_isolated_instance.py",
    REPO_ROOT
    / ".agents"
    / "shared"
    / "scripts"
    / "agents_shared"
    / "resolve_template_base.py",
    REPO_ROOT
    / ".agents"
    / "shared"
    / "scripts"
    / "agents_shared"
    / "preview_wrapper_server.py",
)

# The only directories bare code may put on sys.path: where the bare packages and the
# stdlib-only libraries live. The bare type check searches exactly these.
BARE_SEARCH_PATHS = (
    SYSTEM_SCRIPTS,
    UPDATE_SELF_SCRIPTS.parent / "python",
    REPO_ROOT / "system" / "libs" / "tk_command_parsing" / "src",
    REPO_ROOT / "system" / "services" / "oom_priority" / "src",
    REPO_ROOT / "system" / "libs" / "chat_background_tasks" / "src",
)

# Bare scripts that are not package stubs: oom_priority's own entry scripts, a
# stdlib-only maintenance script, and the Datalib app's launcher (the app is not a
# workspace member, and the launcher is its supervisord program).
STANDALONE_BARE_DIRS = (REPO_ROOT / "system" / "services" / "oom_priority" / "bin",)
STANDALONE_BARE_SCRIPTS = (
    REPO_ROOT / "catalog" / "build_catalog_from_export.py",
    REPO_ROOT / "system" / "apps" / "datalib" / "launch_datalib_http.py",
)


def scripts_dirs(repo_root: Path = REPO_ROOT) -> list[Path]:
    return [
        repo_root / "system" / "scripts",
        repo_root / ".agents" / "shared" / "scripts",
        *sorted((repo_root / ".agents" / "skills").glob("*/scripts")),
    ]


def project_dirs(repo_root: Path = REPO_ROOT) -> list[Path]:
    """The uv projects whose packages hold the workspace's scripts."""
    return [
        repo_root / "system" / "scripts",
        repo_root / ".agents" / "shared" / "scripts",
        *sorted((repo_root / ".agents" / "skills").glob("*/python")),
    ]


def module_entry_points(repo_root: Path = REPO_ROOT) -> list[Path]:
    """The package modules run by their own path."""
    return [
        module
        for directory in project_dirs(repo_root)
        for module in package_entry_modules(directory)
    ]


def is_test_file(path: Path) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def entry_files(scripts_dir: Path) -> list[Path]:
    return sorted(path for path in scripts_dir.glob("*.py") if not is_test_file(path))


def project_dir(entry: Path) -> Path:
    """The uv project an entry's package lives in: the package's parent for a module, else
    the stub's own dir, or a skill's ``python/``."""
    if (entry.parent / "__init__.py").is_file():
        return entry.parent.parent
    skill_project = entry.parent.parent / "python"
    return (
        skill_project if (skill_project / "pyproject.toml").is_file() else entry.parent
    )


def entry_package(entry: Path) -> str | None:
    """The package a stub imports from, or None for a file that imports none of its own."""
    for node in ast.walk(ast.parse(entry.read_text(), filename=str(entry))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            package = node.module.split(".")[0]
            if (project_dir(entry) / package / "__init__.py").is_file():
                return package
    return None


def bare_skill_modules(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every bare skill's single script."""
    return [
        module
        for skill_dir in sorted((repo_root / ".agents" / "skills").glob("*/"))
        if (module := bare_skill_module(skill_dir)) is not None
    ]


def is_bare_entry(path: Path) -> bool:
    if path in STANDALONE_BARE_SCRIPTS or path.parent in STANDALONE_BARE_DIRS:
        return path.is_file()
    if path in BARE_MODULES:
        return path.is_file()
    if path.parent.name == "scripts" and bare_skill_module(path.parent.parent) == path:
        return True
    if (path.parent / "__init__.py").is_file():
        return path.is_file() and path.parent.name in BARE_PACKAGES
    return path.is_file() and entry_package(path) in BARE_PACKAGES
