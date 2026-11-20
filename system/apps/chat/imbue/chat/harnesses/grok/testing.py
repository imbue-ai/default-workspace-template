"""Test utilities for the grok harness: the captured grok sessions under ``fixtures/``, and a fake grok agent state
dir in mngr's layout, written by hand, built from them."""

import json
from pathlib import Path
from typing import Final
from uuid import uuid4

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.session_parser import UPDATES_FILENAME
from imbue.chat.harnesses.grok.watcher import SESSIONS_DIRNAME
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.mngr_grok.grok_config import get_grok_home

GROK_FIXTURES_DIR: Final[Path] = Path(__file__).parent / "fixtures"
# The directories of captured scenarios: mngr's TUI captures, then the chat app's TUI and headless ones.
FIXTURE_GROUP_DIRS: Final[tuple[Path, ...]] = tuple(
    GROK_FIXTURES_DIR / group for group in ("sessions", "tui_sessions", "headless_sessions")
)
# A scenario directory's list of its top-level session ids, one per line.
FIXTURE_ROOTS_FILENAME: Final[str] = "roots"


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


# Where mngr's SessionStart hook appends each top-level session id, one per line.
ROOTS_RELATIVE_PATH: Final[Path] = Path("plugin", "grok", "roots")
# The session bucket the fake agent's sessions are filed under: grok names a bucket after its work dir.
FAKE_SESSION_BUCKET: Final[str] = "%2Ftmp%2Fgrok-fixtures"


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
    """Append a top-level session id to ``roots``, as mngr's SessionStart hook does."""
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


def make_grok_agent_info(agent_state_dir: Path, agent_id: str, name: str, state: str = "RUNNING") -> AgentInfo:
    """A grok agent in lifecycle ``state`` whose state dir is ``agent_state_dir``; grok reads no claude config dir."""
    return AgentInfo(
        id=agent_id,
        name=name,
        state=state,
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.GROK,
    )


def make_grok_agent_state_dir(parent: Path) -> Path:
    """A new, empty agent state dir under ``parent``, named by a fresh agent id as mngr names one."""
    agent_state_dir = parent / f"agent-{uuid4().hex}"
    agent_state_dir.mkdir(parents=True)
    return agent_state_dir


def user_prompt_line(session_id: str, text: str, prompt_index: int, timestamp_ms: int) -> bytes:
    """One hand-built updates.jsonl record of a user prompt grok logged, carrying the fields the parser reads."""
    record = {
        "timestamp": timestamp_ms // 1000,
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
                "_meta": {"modelId": "stub-model", "promptIndex": prompt_index},
            },
            "_meta": {"eventId": f"{session_id}-{timestamp_ms}", "agentTimestampMs": timestamp_ms},
        },
    }
    return (json.dumps(record) + "\n").encode()
