"""The finish-notification nudge: who it fires for, when, and what silences it.

The two hooks and the notify-user skill's script share one directory of turn
state through a path each spells out for itself, so these drive the real
scripts against a real vendored ``tk`` and a real notification POST rather than
asserting on the path string: a drift between the two ends shows up as a nudge
that fires after a notification already went out.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "system" / "scripts"
_VENDORED_TK = _REPO_ROOT / "system" / "vendor" / "tk"
_NOTIFY_USER = (
    _REPO_ROOT / ".agents" / "skills" / "notify-user" / "scripts" / "notify_user.py"
)

_TURN_START = _SCRIPTS / "agent_notify_user_turn_start.sh"
_STOP_NUDGE = _SCRIPTS / "agent_notify_user_stop_nudge.sh"

_CHAT_ID = "chat-under-test"


@pytest.fixture
def chat_env(tmp_path: Path) -> dict[str, str]:
    """A chat agent's environment, with a work dir the hooks can find ``tk`` and ``data/`` in."""
    work_dir = tmp_path / "workspace"
    (work_dir / "system" / "vendor").mkdir(parents=True)
    (work_dir / "system" / "vendor" / "tk").symlink_to(_VENDORED_TK)
    tickets_dir = work_dir / "data" / ".tickets"
    tickets_dir.mkdir(parents=True)
    return {
        **os.environ,
        "MNGR_AGENT_WORK_DIR": str(work_dir),
        "MNGR_AGENT_ROLE": "chat",
        "MNGR_AGENT_ID": "agent-under-test",
        "MINDS_CHAT_ID": _CHAT_ID,
        "TICKETS_DIR": str(tickets_dir),
        # Cleared rather than inherited: these tests run inside an agent often
        # enough, and an inherited marker would silence every hook under test
        # while the assertions still passed.
        "MNGR_CLAUDE_SUBAGENT_PROXY_CHILD": "",
    }


def _state_root(env: dict[str, str]) -> Path:
    return Path(env["MNGR_AGENT_WORK_DIR"]) / "data" / ".state" / "notify-user"


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _tk(env: dict[str, str], *args: str) -> str:
    """Drive the vendored ticket script the hooks read, as the agent would."""
    result = subprocess.run(
        [str(_VENDORED_TK / "ticket"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def _create_step(env: dict[str, str], title: str) -> str:
    """Create one step record and return its id (``Created <id>: <title>``)."""
    return _tk(env, "create", "--step", title).split()[1].rstrip(":")


class _AcceptingAppHandler(BaseHTTPRequestHandler):
    """A gateway that accepts every notification, so the skill's script succeeds for real."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        server: Any = self.server
        server.posted.append(self.path)
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def accepting_app() -> Iterator[Any]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AcceptingAppHandler)
    server.posted = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_turn_that_did_work_is_nudged_once_and_then_let_through(
    chat_env: dict[str, str],
) -> None:
    """The nudge is spent on the work it fired for: an agent that declines can still stop."""
    _run(_TURN_START, chat_env)
    step_id = _create_step(chat_env, "Rebuild the login flow")
    _tk(chat_env, "close", step_id, "Rebuilt the login flow.")

    first_stop = _run(_STOP_NUDGE, chat_env)
    assert first_stop.returncode == 2
    assert "notify_user.py" in first_stop.stderr

    second_stop = _run(_STOP_NUDGE, chat_env)
    assert second_stop.returncode == 0
    assert second_stop.stderr == ""


def test_a_turn_that_touched_no_steps_is_never_nudged(chat_env: dict[str, str]) -> None:
    """Chitchat, a clarifying question and a single quick read leave the step records alone."""
    _create_step(chat_env, "Work carried over from an earlier turn")
    _run(_TURN_START, chat_env)

    assert _run(_STOP_NUDGE, chat_env).returncode == 0


def test_a_notification_already_sent_this_turn_silences_the_nudge(
    chat_env: dict[str, str], accepting_app: Any
) -> None:
    """The skill's script and the hook must agree on where a turn's state lives."""
    _run(_TURN_START, chat_env)
    _create_step(chat_env, "Run the migration")

    sent = subprocess.run(
        [
            "python3",
            str(_NOTIFY_USER),
            "The migration finished: 3 tables moved and verified.",
        ],
        env={
            **chat_env,
            "LATCHKEY_GATEWAY": f"http://127.0.0.1:{accepting_app.server_address[1]}",
            "LATCHKEY_GATEWAY_PASSWORD": "pw",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert sent.returncode == 0, sent.stderr
    assert accepting_app.posted == [
        "/minds-api-proxy/api/v1/agents/agent-under-test/notifications"
    ]

    assert _run(_STOP_NUDGE, chat_env).returncode == 0


def test_the_next_turn_reopens_the_nudge(chat_env: dict[str, str]) -> None:
    """Every turn is judged on its own work, so one notification does not cover the next turn."""
    _run(_TURN_START, chat_env)
    _create_step(chat_env, "First turn's work")
    assert _run(_STOP_NUDGE, chat_env).returncode == 2

    _run(_TURN_START, chat_env)
    _create_step(chat_env, "Second turn's work")
    assert _run(_STOP_NUDGE, chat_env).returncode == 2


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MNGR_AGENT_ROLE", "worker"),
        ("MNGR_AGENT_ROLE", "caretaker"),
        ("MNGR_AGENT_ROLE", "automation"),
        ("MNGR_AGENT_ROLE", ""),
        ("MNGR_CLAUDE_SUBAGENT_PROXY_CHILD", "1"),
    ],
)
def test_only_a_chat_agent_is_nudged(
    chat_env: dict[str, str], variable: str, value: str
) -> None:
    """A worker reports through its lead's chat, and a subagent's turn ends inside its parent's.

    Both hooks gate for themselves: the first half checks the one that opens a
    turn, the second hands the other a turn a chat opened, so neither can be
    passing on the other's refusal.
    """
    env = {**chat_env, variable: value}
    _run(_TURN_START, env)
    assert not _state_root(env).exists()
    _create_step(env, "Work a non-chat agent did")
    assert _run(_STOP_NUDGE, env).returncode == 0

    _run(_TURN_START, chat_env)
    _create_step(chat_env, "Work done after a chat opened the turn")
    assert _run(_STOP_NUDGE, env).returncode == 0
    assert _run(_STOP_NUDGE, chat_env).returncode == 2
