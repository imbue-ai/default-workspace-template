"""Entry points stay cheap to start: importing one loads no heavy module it has not declared.

A one-off pays for its imports on every call, and on gVisor a few heavy libraries dominate a
CLI's startup (see .agents/shared/references/running-python.md). Each entry point is imported in
a fresh interpreter of its own tier -- the root venv's python for a venv entry, ``python -S -s``
(no site-packages) for a bare one -- and the modules it left in ``sys.modules`` are checked
against a short list. A venv entry that needs one at import declares it in its project's
``pyproject.toml`` (a skill's ``python/pyproject.toml``) under ``[tool.workspace-template.entry-points."<entry>.py"]`` as
``heavy-imports``; everything else imports heavy modules inside the function that uses them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from entry_points_testing import (
    STANDALONE_BARE_DIRS,
    STANDALONE_BARE_SCRIPTS,
    declared_heavy_imports,
    entry_files,
    is_bare_entry,
    scripts_dirs,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_HEAVY_VENV_MODULES = frozenset({"pydantic", "loguru", "click", "tenacity", "httpx"})
_HEAVY_STDLIB_MODULES = frozenset({"asyncio"})

# Imports the entry the way running it would (its directory first on sys.path) without
# running its __main__ block, then reports every module left loaded.
_PROBE = """
import importlib.util, json, sys
path = sys.argv[1]
sys.path.insert(0, path.rsplit("/", 1)[0])
spec = importlib.util.spec_from_file_location("_entry_point_probe", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(sorted({name.split(".")[0] for name in sys.modules})))
"""


def _all_entry_points() -> list[Path]:
    entries = [entry for directory in scripts_dirs() for entry in entry_files(directory)]
    entries += [path for directory in STANDALONE_BARE_DIRS for path in entry_files(directory)]
    entries += list(STANDALONE_BARE_SCRIPTS)
    return entries


def _loaded_modules(entry: Path, *, bare: bool) -> frozenset[str]:
    flags = ["-S", "-s"] if bare else []
    result = subprocess.run(
        [sys.executable, *flags, "-c", _PROBE, str(entry)],
        cwd=_REPO_ROOT,
        # As a real run sees it: some libraries change behaviour when they detect pytest.
        env={name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"importing {entry} failed:\n{result.stderr}"
    return frozenset(json.loads(result.stdout))


def _undeclared_heavy_imports(entry: Path, *, bare: bool) -> set[str]:
    heavy = _HEAVY_STDLIB_MODULES if bare else _HEAVY_VENV_MODULES | _HEAVY_STDLIB_MODULES
    return set(_loaded_modules(entry, bare=bare) & heavy) - declared_heavy_imports(entry)


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "entry", _all_entry_points(), ids=lambda path: str(path.relative_to(_REPO_ROOT))
)
def test_an_entry_point_loads_no_undeclared_heavy_module(entry: Path) -> None:
    bare = is_bare_entry(entry)
    offenders = _undeclared_heavy_imports(entry, bare=bare)
    assert not offenders, (
        f"importing {entry.relative_to(_REPO_ROOT)} loads {sorted(offenders)}. Import them inside "
        "the function that uses them"
        + ("" if bare else ', or declare them under [tool.workspace-template.entry-points."<entry>.py"]')
    )


def _skill_scripts(tmp_path: Path, module_source: str, pyproject: str = "") -> Path:
    """A skill laid out as the built-in ones are: its project in ``python/``, its stub in ``scripts/``.

    A tmp package has no editable install, so the stub puts ``../python`` on ``sys.path`` itself.
    """
    project = tmp_path / "probe-skill" / "python"
    package = project / "probe_skill"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(module_source)
    (project / "pyproject.toml").write_text(pyproject)
    scripts = project.parent / "scripts"
    scripts.mkdir()
    entry = scripts / "probe.py"
    entry.write_text(
        "import sys\nfrom pathlib import Path\n\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))\n\n'
        'from probe_skill.cli import main\n\nif __name__ == "__main__":\n    main()\n'
    )
    return entry


@pytest.mark.timeout(60)
def test_the_probe_catches_a_top_level_heavy_import(tmp_path: Path) -> None:
    entry = _skill_scripts(tmp_path, "import pydantic\n\n\ndef main() -> None:\n    pass\n")

    assert _undeclared_heavy_imports(entry, bare=False) == {"pydantic"}


@pytest.mark.timeout(60)
def test_a_declared_heavy_import_passes(tmp_path: Path) -> None:
    entry = _skill_scripts(
        tmp_path,
        "import pydantic\n\n\ndef main() -> None:\n    pass\n",
        '[tool.workspace-template.entry-points."probe.py"]\nheavy-imports = ["pydantic"]\n',
    )

    assert _undeclared_heavy_imports(entry, bare=False) == set()


@pytest.mark.timeout(60)
def test_a_heavy_import_inside_a_subcommand_passes(tmp_path: Path) -> None:
    entry = _skill_scripts(tmp_path, "def main() -> None:\n    import pydantic\n\n    del pydantic\n")

    assert _undeclared_heavy_imports(entry, bare=False) == set()


@pytest.mark.timeout(60)
def test_a_bare_entry_is_probed_without_site_packages(tmp_path: Path) -> None:
    entry = _skill_scripts(tmp_path, "import asyncio\n\n\ndef main() -> None:\n    pass\n")

    assert _undeclared_heavy_imports(entry, bare=True) == {"asyncio"}


@pytest.mark.timeout(60)
def test_a_bare_entry_cannot_reach_the_venv(tmp_path: Path) -> None:
    entry = _skill_scripts(tmp_path, "import pydantic\n\n\ndef main() -> None:\n    pass\n")

    with pytest.raises(AssertionError, match="ModuleNotFoundError"):
        _loaded_modules(entry, bare=True)
