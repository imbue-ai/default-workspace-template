from pathlib import Path

import pytest
from workspace_backup_setup.state import WORKSPACE_ID_ENV
from workspace_backup_setup.testing import (
    FAKE_RESTIC_CONTROL_DIR_ENV,
    WORKSPACE_ID,
    install_fake_restic,
    path_with_fake_restic_first,
)
from workspace_bare_scripts.backup_lease import LEASE_PATH_ENV

from workspace_initial_data.cli import HOST_DIR_ENV_VAR
from workspace_initial_data.testing import RecordingBackupDestination


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty workspace root, which is the working directory, as it is for the boot sequence."""
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def host_dir(tmp_path: Path) -> Path:
    """The machine's host directory, where a creator leaves the document; nothing is there yet."""
    directory = tmp_path / "host"
    directory.mkdir()
    return directory


@pytest.fixture
def backup_destination() -> RecordingBackupDestination:
    return RecordingBackupDestination()


@pytest.fixture
def lease_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the boot step and the backup setup keep the backup lease, in place of ``/run``."""
    directory = tmp_path / "run"
    directory.mkdir()
    path = directory / "backup-operation.lease"
    monkeypatch.setenv(LEASE_PATH_ENV, str(path))
    return path


@pytest.fixture
def boot_environment(
    host_dir: Path, lease_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the boot sequence runs the step with: the host directory and the workspace's id."""
    monkeypatch.setenv(HOST_DIR_ENV_VAR, str(host_dir))
    monkeypatch.setenv(WORKSPACE_ID_ENV, WORKSPACE_ID)


@pytest.fixture
def fake_restic_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in ``restic`` first on PATH, for a test that runs the backup setup on what the step recorded."""
    control_directory = tmp_path / "restic-control"
    install_fake_restic(tmp_path / "bin", control_directory)
    monkeypatch.setenv("PATH", path_with_fake_restic_first(tmp_path / "bin"))
    monkeypatch.setenv(FAKE_RESTIC_CONTROL_DIR_ENV, str(control_directory))
    return control_directory
