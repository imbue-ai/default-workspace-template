"""The SessionStart sync never relocks: it syncs from the committed lock and reports a drift once."""

import os
import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().with_name("session_start_sync.sh")

_PYPROJECT = """\
[project]
name = "session-start-sync-probe"
version = "0.1.0"
requires-python = "{requires_python}"
dependencies = []

[tool.uv]
package = false
"""


def _project(root: Path, requires_python: str) -> None:
    (root / "pyproject.toml").write_text(
        _PYPROJECT.format(requires_python=requires_python)
    )


def _locked_project(root: Path) -> str:
    """Write the probe project, lock it, and return the committed lock's text."""
    _project(root, ">=3.11")
    subprocess.run(["uv", "lock", "--quiet"], cwd=root, check=True, timeout=120)
    return (root / "uv.lock").read_text()


def _run_hook(root: Path) -> subprocess.CompletedProcess[str]:
    # Offline: the probe project has no dependencies, so nothing here needs an index.
    return subprocess.run(
        ["bash", str(_HOOK)],
        cwd=root,
        env={**os.environ, "UV_OFFLINE": "1", "VIRTUAL_ENV": ""},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_an_in_sync_lock_syncs_silently(tmp_path: Path) -> None:
    lock_before = _locked_project(tmp_path)

    result = _run_hook(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (tmp_path / "uv.lock").read_text() == lock_before
    assert (tmp_path / ".venv").is_dir()


def test_a_drifted_lock_is_left_alone_and_reported_once(tmp_path: Path) -> None:
    lock_before = _locked_project(tmp_path)
    _project(tmp_path, ">=3.12")

    result = _run_hook(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "uv.lock is out of date with pyproject.toml" in result.stdout
    assert len(result.stdout.splitlines()) == 1
    assert (tmp_path / "uv.lock").read_text() == lock_before
    assert (tmp_path / ".venv").is_dir()
