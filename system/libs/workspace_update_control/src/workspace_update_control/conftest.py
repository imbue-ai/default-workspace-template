import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from workspace_update_control.agents import agent_events_path
from workspace_update_control.testing import (
    LocalFeedServer,
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


@pytest.fixture
def local_feed() -> Generator[str, None, None]:
    """The base address of a feed served from this machine, which publishes the stable channel's file, and
    misbehaves for the channels ``LocalFeedHandler`` names."""
    server = LocalFeedServer(("127.0.0.1", 0))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.is_taken_down.set()
        server.shutdown()
        server.server_close()
        serving.join()
