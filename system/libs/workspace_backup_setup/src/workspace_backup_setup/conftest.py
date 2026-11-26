from pathlib import Path

import pytest

from workspace_backup_setup.testing import (
    FAKE_RESTIC_CONTROL_DIR_ENV,
    install_fake_restic,
    path_with_fake_restic_first,
)


@pytest.fixture
def fake_restic_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``restic`` first on PATH; the directory its mode, calls, refused tokens and release files live in."""
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
def lease_path(tmp_path: Path) -> Path:
    """Where a test keeps the backup lease, in place of ``/run``."""
    directory = tmp_path / "run"
    directory.mkdir()
    return directory / "backup-operation.lease"
