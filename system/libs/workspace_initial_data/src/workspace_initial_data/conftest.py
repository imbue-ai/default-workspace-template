from pathlib import Path

import pytest

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
