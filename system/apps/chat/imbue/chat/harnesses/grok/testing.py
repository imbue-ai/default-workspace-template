"""Test utilities for the grok harness: a fake grok agent state dir in mngr_grok's layout, built from captured sessions.

The layout is mngr_grok's private one, written here by hand; ``watcher_test.py`` pins it against
``imbue.mngr_grok.api``'s readers, so an mngr bump that moves it fails there first.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.watcher import SESSIONS_DIRNAME
from imbue.chat.harnesses.grok.watcher import UPDATES_FILENAME
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.mngr_grok.api import get_grok_home

GROK_FIXTURES_DIR: Final[Path] = Path(__file__).parent / "fixtures"
# The directories of captured scenarios: mngr's TUI captures, then the chat app's TUI and headless ones.
FIXTURE_GROUP_DIRS: Final[tuple[Path, ...]] = tuple(
    GROK_FIXTURES_DIR / group for group in ("sessions", "tui_sessions", "headless_sessions")
)
# A scenario directory's list of its top-level session ids, one per line.
FIXTURE_ROOTS_FILENAME: Final[str] = "roots"

# Where mngr_grok's SessionStart hook appends each top-level session id, one per line.
ROOTS_RELATIVE_PATH: Final[Path] = Path("plugin", "grok", "roots")
# Where mngr_grok's hooks keep one file per top-level turn in flight, named by its session id.
ROOT_TURNS_RELATIVE_PATH: Final[Path] = Path("plugin", "grok", "turns")
# mngr_grok's record of the grok process's pid, which its SessionStart hook writes.
GROK_PID_RELATIVE_PATH: Final[Path] = Path("plugin", "grok", "grok.pid")
# The session bucket the fake agent's sessions are filed under: grok names a bucket after its work dir.
FAKE_SESSION_BUCKET: Final[str] = "%2Ftmp%2Fgrok-fixtures"
# The auth.json scope of a grok session login, named by its issuer and client id.
GROK_SESSION_SCOPE: Final[str] = "https://auth.x.ai::client-id"


def find_fixture_scenario(name: str) -> Path:
    """The directory of the captured scenario ``name``, from the first fixture group that has it."""
    scenario_dirs = [group_dir / name for group_dir in FIXTURE_GROUP_DIRS]
    return next((scenario_dir for scenario_dir in scenario_dirs if scenario_dir.is_dir()), scenario_dirs[-1])


def list_fixture_scenarios() -> list[Path]:
    return sorted(path for group_dir in FIXTURE_GROUP_DIRS for path in group_dir.iterdir() if path.is_dir())


def read_fixture_root_ids(scenario_dir: Path) -> list[str]:
    return scenario_dir.joinpath(FIXTURE_ROOTS_FILENAME).read_text().split()


def read_fixture_session_ids(scenario_dir: Path) -> list[str]:
    """Every session the scenario captured: its top-level sessions in order, then its child sessions."""
    root_ids = read_fixture_root_ids(scenario_dir)
    child_ids = sorted(path.name for path in scenario_dir.iterdir() if path.is_dir() and path.name not in root_ids)
    return [*root_ids, *child_ids]


def read_fixture_updates(scenario_dir: Path, session_id: str) -> bytes:
    return (scenario_dir / session_id / UPDATES_FILENAME).read_bytes()


def get_fake_session_dir(agent_state_dir: Path, session_id: str) -> Path:
    return get_grok_home(agent_state_dir) / SESSIONS_DIRNAME / FAKE_SESSION_BUCKET / session_id


def add_grok_session(agent_state_dir: Path, session_id: str, updates: bytes, *, is_root: bool) -> Path:
    """Create a session's directory holding ``updates`` as its log, recording a top-level one in ``roots``; returns its log."""
    session_dir = get_fake_session_dir(agent_state_dir, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    updates_path = session_dir / UPDATES_FILENAME
    updates_path.write_bytes(updates)
    if is_root:
        record_root_session(agent_state_dir, session_id)
    return updates_path


def record_root_session(agent_state_dir: Path, session_id: str) -> None:
    """Append a top-level session id to ``roots``, as mngr_grok's SessionStart hook does."""
    roots_path = agent_state_dir / ROOTS_RELATIVE_PATH
    roots_path.parent.mkdir(parents=True, exist_ok=True)
    with roots_path.open("a", encoding="utf-8") as roots_file:
        roots_file.write(session_id + "\n")


def append_grok_updates(agent_state_dir: Path, session_id: str, data: bytes) -> None:
    with (get_fake_session_dir(agent_state_dir, session_id) / UPDATES_FILENAME).open("ab") as updates_file:
        updates_file.write(data)


def write_grok_agent_state(agent_state_dir: Path, scenario_dir: Path) -> None:
    """Lay a captured scenario's sessions out in ``agent_state_dir`` as a live grok agent's are."""
    root_ids = read_fixture_root_ids(scenario_dir)
    for session_id in read_fixture_session_ids(scenario_dir):
        add_grok_session(
            agent_state_dir,
            session_id,
            read_fixture_updates(scenario_dir, session_id),
            is_root=session_id in root_ids,
        )


def write_root_turn(agent_state_dir: Path, session_id: str, prompt_id: str, prompt_tail: str = "") -> None:
    """Record a top-level turn in flight, as mngr_grok's UserPromptSubmit hook does."""
    turns_dir = agent_state_dir / ROOT_TURNS_RELATIVE_PATH
    turns_dir.mkdir(parents=True, exist_ok=True)
    (turns_dir / session_id).write_text(f"{prompt_id}\n{prompt_tail}\n", encoding="utf-8")


def remove_root_turn(agent_state_dir: Path, session_id: str) -> None:
    """Record a top-level turn's end, as mngr_grok's Stop hook does."""
    (agent_state_dir / ROOT_TURNS_RELATIVE_PATH / session_id).unlink()


def build_grok_key_scope(key: str = "xai-key-5c1e") -> dict[str, object]:
    """The ``xai::api_key`` scope grok itself writes for a stored key."""
    return {
        "key": key,
        "auth_mode": "api_key",
        "create_time": "2026-10-19T19:11:22.318953Z",
        "user_id": "",
        "email": None,
        "coding_data_retention_opt_out": True,
    }


def write_grok_auth(account_dir: Path, scopes: Mapping[str, object]) -> None:
    """Write a grok account's ``auth.json`` holding ``scopes``."""
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "auth.json").write_text(json.dumps(scopes))


def make_grok_agent_info(agent_state_dir: Path, agent_id: str, name: str) -> AgentInfo:
    """A running grok agent whose state dir is ``agent_state_dir``; grok reads no claude config dir."""
    return AgentInfo(
        id=agent_id,
        name=name,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.GROK,
    )


def write_live_grok_pid(agent_state_dir: Path) -> None:
    """Record this test process as the agent's running grok, as mngr_grok's SessionStart hook does."""
    pid_path = agent_state_dir / GROK_PID_RELATIVE_PATH
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{os.getpid()}\n")


def grok_update_line(session_id: str, update: dict[str, Any], timestamp_ms: int) -> bytes:
    """One updates.jsonl record in the shape grok 1.0.62 writes it."""
    record = {
        "timestamp": timestamp_ms // 1000,
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": update,
            "_meta": {"eventId": f"{session_id}-{timestamp_ms}", "agentTimestampMs": timestamp_ms},
        },
    }
    return (json.dumps(record) + "\n").encode()
