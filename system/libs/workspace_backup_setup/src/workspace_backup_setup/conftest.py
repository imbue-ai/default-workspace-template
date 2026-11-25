import subprocess
import sys
from pathlib import Path

import pytest

from workspace_backup_setup.testing import FAKE_RESTIC_CONTROL_DIR_ENV
from workspace_backup_setup.testing import install_fake_restic
from workspace_backup_setup.testing import path_with_fake_restic_first


@pytest.fixture
def fake_restic_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``restic`` first on PATH; the directory its mode, calls and release files live in."""
    control_directory = tmp_path / "restic-control"
    install_fake_restic(tmp_path / "bin", control_directory)
    monkeypatch.setenv("PATH", path_with_fake_restic_first(tmp_path / "bin"))
    monkeypatch.setenv(FAKE_RESTIC_CONTROL_DIR_ENV, str(control_directory))
    return control_directory


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def exited_pid() -> int:
    """The pid of a process that has already exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid
