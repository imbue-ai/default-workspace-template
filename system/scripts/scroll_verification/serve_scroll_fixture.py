"""Standalone system-interface server over a real tool-heavy transcript, for
manually verifying the transcript smooth-scroll engine in a browser.

Mirrors test_e2e's harness: fake agent state dirs, a fixture claude config dir
whose session file is a REAL session JSONL copied from ~/.claude/projects, a
never-started AgentManager seeded with the fixture agent, and patched agent
discovery. Serves on the given port until killed.
"""

import os
import shutil
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
SOURCE_JSONL = Path(sys.argv[2]).expanduser()
FIXTURE_ROOT = Path(sys.argv[3]).expanduser()
AGENT_ID = "agent-scrollfix-1"
AGENT_NAME = "scroll-fixture"
SESSION_ID = "scrollfix-session-001"


def build_fixture() -> AgentInfo:
    agent_state_dir = FIXTURE_ROOT / "agents" / AGENT_ID
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    claude_config_dir = FIXTURE_ROOT / "claude_config"
    projects_dir = claude_config_dir / "projects" / "fixture-project"
    projects_dir.mkdir(parents=True, exist_ok=True)
    (agent_state_dir / "claude_session_id_history").write_text(f"{SESSION_ID}\n")
    (agent_state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={claude_config_dir}\n")
    shutil.copyfile(SOURCE_JSONL, projects_dir / f"{SESSION_ID}.jsonl")
    return AgentInfo(
        id=AGENT_ID,
        name=AGENT_NAME,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )


def main() -> None:
    agent_info = build_fixture()

    fake_bin_dir = FIXTURE_ROOT / "fake-bin"
    fake_bin_dir.mkdir(exist_ok=True)
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(
        '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}\'\n'
    )
    fake_claude.chmod(0o755)
    fake_mngr = fake_bin_dir / "mngr"
    fake_mngr.write_text("#!/bin/sh\nexit 0\n")
    fake_mngr.chmod(0o755)

    with (
        patch.dict(
            os.environ,
            {
                "MNGR_HOST_DIR": str(FIXTURE_ROOT),
                "MNGR_AGENT_ID": "",
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
            },
        ),
        patch("imbue.system_interface.server.discover_agents", return_value=[agent_info]),
    ):
        broadcaster = WebSocketBroadcaster()
        manager = AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())
        with manager._lock:
            manager._agents[agent_info.id] = AgentStateItem(
                id=agent_info.id,
                name=agent_info.name,
                state="RUNNING",
                labels={},
                work_dir=str(FIXTURE_ROOT / "work"),
            )
        manager._ensure_activity_tracking(agent_info.id)

        config = Config(system_interface_host="127.0.0.1", system_interface_port=PORT)
        app = create_application(build_test_state(config=config, agent_manager=manager))
        server = make_threaded_server("127.0.0.1", PORT, app)
        print(f"serving http://127.0.0.1:{PORT} agent={AGENT_ID}", flush=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        thread.join()


if __name__ == "__main__":
    main()
