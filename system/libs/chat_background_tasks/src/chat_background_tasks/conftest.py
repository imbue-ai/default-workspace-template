import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def marker_root(tmp_path: Path) -> Path:
    return tmp_path / "background_tasks"


@pytest.fixture
def exited_pid() -> int:
    """The pid of a process that has already exited, which a marker naming it is stale by."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid
