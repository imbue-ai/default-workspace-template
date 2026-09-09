"""Tests for ``install_worker_skills.sh``.

Run via: ``uv run pytest .agents/shared/scripts/install_worker_skills_test.py``

The script is what the ``worker`` create template runs at provision time
(see ``.mngr/settings.toml``) to make the generic worker at
``.agents/shared/worker/`` loadable inside a worker as the ``harden-worker``
skill. These tests run the real script against a temp destination and pin
the installed layout, the overwrite semantics, and the failure modes -- the
contract every launched worker's skill tree currently depends on.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SCRIPT = _SCRIPTS_DIR / "install_worker_skills.sh"
_WORKER_SOURCE = _SCRIPTS_DIR.parent / "worker"

_spec = importlib.util.spec_from_file_location(
    "validate_skill", _SCRIPTS_DIR / "validate_skill.py"
)
assert _spec is not None and _spec.loader is not None
validate_skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_skill)

# The name the worker template installs the generic worker under. Lead skills
# tell workers to "use the installed `harden-worker` sub-skill" by this name.
_INSTALLED_NAME = "harden-worker"


def _run_install(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_installs_generic_worker_skill_md_only(tmp_path: Path) -> None:
    """The generic worker lands at ``<dest>/harden-worker/`` as a bare SKILL.md.

    The references are read from the repo copy, so the installed skill must not
    carry a second copy of ``references/`` (a never-read duplicate that would
    drift from the source of truth).
    """
    destination = tmp_path / "skills"

    result = _run_install(_SCRIPT, str(destination))

    assert result.returncode == 0, result.stderr
    installed = destination / _INSTALLED_NAME
    assert sorted(p.name for p in installed.iterdir()) == ["SKILL.md"]
    assert (installed / "SKILL.md").read_text() == (
        _WORKER_SOURCE / "SKILL.md"
    ).read_text()
    assert str(destination) in result.stdout


def test_installed_skill_is_a_valid_skill_under_its_installed_name(
    tmp_path: Path,
) -> None:
    """The installed directory passes the agentskills.io structural checks.

    In particular the SKILL.md ``name:`` must equal the install directory's
    basename, or the worker's skill loader will not surface it as
    ``harden-worker`` -- which is the name every lead's task file tells the
    worker to use.
    """
    destination = tmp_path / "skills"
    assert _run_install(_SCRIPT, str(destination)).returncode == 0

    error = validate_skill.validate(destination / _INSTALLED_NAME)

    assert error is None, error


def test_reinstall_replaces_a_stale_installed_copy(tmp_path: Path) -> None:
    """A second install overwrites whatever was there, so a worker always gets
    the freshest copy and never inherits leftovers from an earlier install."""
    destination = tmp_path / "skills"
    assert _run_install(_SCRIPT, str(destination)).returncode == 0
    installed = destination / _INSTALLED_NAME
    stale = installed / "stale-leftover.md"
    stale.write_text("from a previous install\n")
    (installed / "SKILL.md").write_text("tampered\n")

    result = _run_install(_SCRIPT, str(destination))

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (installed / "SKILL.md").read_text() == (
        _WORKER_SOURCE / "SKILL.md"
    ).read_text()


def test_creates_a_missing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "not" / "yet" / "there"

    result = _run_install(_SCRIPT, str(destination))

    assert result.returncode == 0, result.stderr
    assert (destination / _INSTALLED_NAME / "SKILL.md").is_file()


def test_wrong_argument_count_is_a_usage_error(tmp_path: Path) -> None:
    no_args = _run_install(_SCRIPT)
    assert no_args.returncode == 2
    assert "usage:" in no_args.stderr

    two_args = _run_install(_SCRIPT, str(tmp_path / "a"), str(tmp_path / "b"))
    assert two_args.returncode == 2
    assert "usage:" in two_args.stderr
    assert not (tmp_path / "a").exists()


def test_missing_worker_source_fails_loud(tmp_path: Path) -> None:
    """The script locates the worker source relative to its own path; a copy
    of the script with no sibling ``worker/`` must fail rather than install an
    empty skill."""
    relocated_scripts = tmp_path / ".agents" / "shared" / "scripts"
    relocated_scripts.mkdir(parents=True)
    relocated_script = relocated_scripts / _SCRIPT.name
    shutil.copy(_SCRIPT, relocated_script)
    destination = tmp_path / "skills"

    result = _run_install(relocated_script, str(destination))

    assert result.returncode == 1
    assert "expected generic worker at" in result.stderr
    assert not destination.exists()
