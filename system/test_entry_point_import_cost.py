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
    IMPORT_PROBE,
    REPO_ROOT,
    all_entry_points,
    declared_heavy_imports,
    is_bare_entry,
    module_entry_points,
)

_HEAVY_VENV_MODULES = frozenset({"pydantic", "loguru", "click", "tenacity", "httpx"})
_HEAVY_STDLIB_MODULES = frozenset({"asyncio"})

# Imports one module the way the entry's own import resolves it (the entry's directory
# first on sys.path), then reports every module left loaded.
_MODULE_PROBE = """
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])
print(json.dumps(sorted({name.split(".")[0] for name in sys.modules})))
"""



def _run_probe(probe: str, *args: str, bare: bool) -> frozenset[str]:
    flags = ["-S", "-s"] if bare else []
    result = subprocess.run(
        [sys.executable, *flags, "-c", probe, *args],
        cwd=REPO_ROOT,
        # As a real run sees it: some libraries change behaviour when they detect pytest.
        env={
            name: value
            for name, value in os.environ.items()
            if not name.startswith("PYTEST_")
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"importing {args[-1]} failed:\n{result.stderr}"
    return frozenset(json.loads(result.stdout))


def _loaded_modules(entry: Path, *, bare: bool) -> frozenset[str]:
    return _run_probe(IMPORT_PROBE, str(entry), bare=bare)


def _undeclared_heavy_imports(entry: Path, *, bare: bool) -> set[str]:
    """The heavy modules importing ``entry`` loads that it has not declared.

    A heavy module that another loaded heavy module imports is part of that one's cost, not
    a second offence: on Python 3.12, importing pydantic loads asyncio (through
    typing_extensions' ``deprecated``), so declaring pydantic covers it.
    """
    heavy = (
        _HEAVY_STDLIB_MODULES if bare else _HEAVY_VENV_MODULES | _HEAVY_STDLIB_MODULES
    )
    loaded = _loaded_modules(entry, bare=bare) & heavy
    pulled_in = {
        dependency
        for module in loaded
        for dependency in _run_probe(
            _MODULE_PROBE, str(entry.parent), module, bare=bare
        )
        - {module}
    }
    return set(loaded - pulled_in) - declared_heavy_imports(entry)


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "entry", all_entry_points(), ids=lambda path: str(path.relative_to(REPO_ROOT))
)
def test_an_entry_point_loads_no_undeclared_heavy_module(entry: Path) -> None:
    bare = is_bare_entry(entry)
    offenders = _undeclared_heavy_imports(entry, bare=bare)
    assert not offenders, (
        f"importing {entry.relative_to(REPO_ROOT)} loads {sorted(offenders)}. Import them inside "
        "the function that uses them"
        + (
            ""
            if bare
            else ', or declare them under [tool.workspace-template.entry-points."<entry>.py"]'
        )
    )


def _stale_heavy_declarations(entry: Path) -> set[str]:
    """The heavy modules ``entry`` declares but no longer loads at import."""
    return set(
        declared_heavy_imports(entry)
        - _loaded_modules(entry, bare=is_bare_entry(entry))
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "entry",
    [entry for entry in all_entry_points() if declared_heavy_imports(entry)],
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_every_declared_heavy_import_is_still_loaded(entry: Path) -> None:
    stale = _stale_heavy_declarations(entry)
    assert not stale, (
        f"{entry.relative_to(REPO_ROOT)} declares heavy-imports {sorted(stale)} that it no longer "
        "loads at import; drop them from its project's pyproject.toml so a later regression is caught"
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
    entry = _skill_scripts(
        tmp_path, "import pydantic\n\n\ndef main() -> None:\n    pass\n"
    )

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
def test_a_declared_heavy_import_the_entry_no_longer_loads_is_stale(
    tmp_path: Path,
) -> None:
    entry = _skill_scripts(
        tmp_path,
        "def main() -> None:\n    pass\n",
        '[tool.workspace-template.entry-points."probe.py"]\nheavy-imports = ["pydantic"]\n',
    )

    assert _stale_heavy_declarations(entry) == {"pydantic"}


@pytest.mark.timeout(60)
def test_a_package_module_run_by_path_is_probed_and_declares_in_its_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / ".agents" / "skills" / "probe" / "python"
    package = project / "probe_skill"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "lib.py").write_text("import pydantic\n")
    (package / "cli.py").write_text(
        "import pydantic\n\n\ndef main() -> None:\n    pass\n\n\n"
        'if __name__ == "__main__":\n    main()\n'
    )

    [entry] = module_entry_points(tmp_path)

    assert entry == package / "cli.py"
    assert _undeclared_heavy_imports(entry, bare=False) == {"pydantic"}
    (project / "pyproject.toml").write_text(
        '[tool.workspace-template.entry-points."cli.py"]\nheavy-imports = ["pydantic"]\n'
    )
    assert _undeclared_heavy_imports(entry, bare=False) == set()


@pytest.mark.timeout(60)
def test_a_heavy_module_another_heavy_module_imports_is_not_counted(
    tmp_path: Path,
) -> None:
    entry = _skill_scripts(
        tmp_path, "import httpx\n\n\ndef main() -> None:\n    pass\n"
    )
    (entry.parent / "httpx").mkdir()
    (entry.parent / "httpx" / "__init__.py").write_text("import asyncio\n")

    assert _undeclared_heavy_imports(entry, bare=False) == {"httpx"}


@pytest.mark.timeout(60)
def test_a_heavy_import_inside_a_subcommand_passes(tmp_path: Path) -> None:
    entry = _skill_scripts(
        tmp_path, "def main() -> None:\n    import pydantic\n\n    del pydantic\n"
    )

    assert _undeclared_heavy_imports(entry, bare=False) == set()


@pytest.mark.timeout(60)
def test_a_bare_entry_is_held_to_the_expensive_stdlib_list(tmp_path: Path) -> None:
    entry = _skill_scripts(
        tmp_path, "import asyncio\n\n\ndef main() -> None:\n    pass\n"
    )

    assert _undeclared_heavy_imports(entry, bare=True) == {"asyncio"}


@pytest.mark.timeout(60)
def test_a_bare_entry_cannot_reach_the_venv(tmp_path: Path) -> None:
    entry = _skill_scripts(
        tmp_path, "import pydantic\n\n\ndef main() -> None:\n    pass\n"
    )

    with pytest.raises(AssertionError, match="ModuleNotFoundError"):
        _loaded_modules(entry, bare=True)
