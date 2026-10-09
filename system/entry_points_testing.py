"""Where the workspace's Python entry points are, and which interpreter each one runs under.

An entry point is a top-level ``.py`` file in a scripts directory (``system/scripts``,
``.agents/shared/scripts``, ``.agents/skills/<name>/scripts``) -- a thin stub that imports its
package, which sits beside it (``system/scripts``, ``.agents/shared/scripts``) or in the skill's
``python/`` project. Its tier is its package's: a stub into a bare package runs under the system
``python3`` with no venv (stdlib only), any other stub runs from the root venv
(``uv run --no-sync``). A few standalone stdlib scripts outside the scripts directories are bare
too. See .agents/shared/references/running-python.md.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The packages the system python3 runs: stdlib only, no venv.
BARE_PACKAGES = frozenset({"workspace_bare_scripts", "update_self_skill"})

# Bare scripts that are not package stubs: oom_priority's own entry scripts, and a
# stdlib-only maintenance script.
STANDALONE_BARE_DIRS = (REPO_ROOT / "system" / "services" / "oom_priority" / "bin",)
STANDALONE_BARE_SCRIPTS = (REPO_ROOT / "catalog" / "build_catalog_from_export.py",)

# ``python -c`` source taking an entry's path: imports the entry the way running it would (its
# directory first on sys.path) without running its __main__ block, then prints every top-level
# module left loaded, as JSON.
IMPORT_PROBE = """
import importlib.util, json, sys
path = sys.argv[1]
sys.path.insert(0, path.rsplit("/", 1)[0])
spec = importlib.util.spec_from_file_location("_entry_point_probe", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(sorted({name.split(".")[0] for name in sys.modules})))
"""

# Where an entry point's project declares the heavy modules the entry may load at import.
ENTRY_POINTS_TABLE = ("workspace-template", "entry-points")


def scripts_dirs(repo_root: Path = REPO_ROOT) -> list[Path]:
    return [
        repo_root / "system" / "scripts",
        repo_root / ".agents" / "shared" / "scripts",
        *sorted((repo_root / ".agents" / "skills").glob("*/scripts")),
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
    """The uv project an entry's package lives in: its own dir, or a skill's ``python/``."""
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
