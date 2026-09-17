import os
from pathlib import Path

import pytest


@pytest.fixture
def fake_tool_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory at the front of PATH, for fake ``ssh`` / ``scp`` executables that stand in for the real ones."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir
