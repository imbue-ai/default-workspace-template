"""Where the workspace's Python entry points are, and which interpreter each one runs under.

An entry point is either a module of a package with a top-level ``if __name__ == "__main__":``
block, run by its own path from the root venv (``uv run --no-sync``), or a stub: a top-level
``.py`` file in a scripts directory (``system/scripts``, ``.agents/shared/scripts``,
``.agents/skills/<name>/scripts``) that imports its package, kept where something outside the
release calls a fixed path or the script runs with no venv. A stub's tier is its package's: a
stub into a bare package runs under the system ``python3`` with no venv (stdlib only), any other
stub runs from the root venv. A few standalone stdlib scripts outside the scripts directories
are bare too. See .agents/shared/references/running-python.md.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from agents_shared.validate_skill import package_entry_modules

REPO_ROOT = Path(__file__).resolve().parents[1]

# The packages the system python3 runs: stdlib only, no venv.
BARE_PACKAGES = frozenset({"workspace_bare_scripts", "update_self_skill"})

# Bare scripts that are not package stubs: oom_priority's own entry scripts, a
# stdlib-only maintenance script, and the Datalib app's launcher (the app is not a
# workspace member, and the launcher is its supervisord program).
STANDALONE_BARE_DIRS = (REPO_ROOT / "system" / "services" / "oom_priority" / "bin",)
STANDALONE_BARE_SCRIPTS = (
    REPO_ROOT / "catalog" / "build_catalog_from_export.py",
    REPO_ROOT / "system" / "apps" / "datalib" / "launch_datalib_http.py",
)

# Where a scripts directory declares the heavy modules an entry point may load at import.
ENTRY_POINTS_TABLE = ("workspace-template", "entry-points")


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


def is_bare_entry(path: Path) -> bool:
    if path in STANDALONE_BARE_SCRIPTS or path.parent in STANDALONE_BARE_DIRS:
        return path.is_file()
    return path.is_file() and entry_package(path) in BARE_PACKAGES


def declared_heavy_imports(entry: Path) -> frozenset[str]:
    """The heavy modules the entry's project's pyproject.toml lets it load at import time."""
    pyproject = project_dir(entry) / "pyproject.toml"
    if not pyproject.is_file():
        return frozenset()
    table = tomllib.loads(pyproject.read_text()).get("tool", {})
    for key in ENTRY_POINTS_TABLE:
        table = table.get(key, {})
    return frozenset(table.get(entry.name, {}).get("heavy-imports", ()))
