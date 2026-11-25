import subprocess
import sys
from pathlib import Path

import pytest

from workspace_update_control.agents import agent_events_path
from workspace_update_control.testing import (
    StagedUpdateWorkspace,
    stage_update_workspace,
)


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StagedUpdateWorkspace:
    """A workspace an update can run in: the skill, a signed-in account, cron, and the chat messenger."""
    return stage_update_workspace(tmp_path, monkeypatch)


@pytest.fixture
def exited_pid() -> int:
    """The pid of a process that has already exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def events_path(tmp_path: Path) -> Path:
    """Where the observer of a host dir under ``tmp_path`` writes its agents events; nothing is there yet."""
    return Path(agent_events_path({"MNGR_HOST_DIR": str(tmp_path / "host-dir")}))
