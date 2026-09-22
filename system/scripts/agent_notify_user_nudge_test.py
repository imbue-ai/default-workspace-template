"""The finish-notification nudge: who it asks, and how the asking ends.

The hook reaches the model only by exiting 2, which makes claude continue the
conversation -- so the one property that must hold is that the continuation's
own Stop goes through. Without it a chat agent can never finish a turn.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_STOP_NUDGE = Path(__file__).resolve().parent / "agent_notify_user_stop_nudge.sh"


@pytest.fixture
def chat_env() -> dict[str, str]:
    """A chat agent's environment."""
    return {
        **os.environ,
        "MNGR_AGENT_ROLE": "chat",
        # Cleared rather than inherited: these tests run inside an agent often
        # enough, and an inherited marker would silence the hook under test
        # while every assertion still passed.
        "MNGR_CLAUDE_SUBAGENT_PROXY_CHILD": "",
    }


def _run(env: dict[str, str], **payload: object) -> subprocess.CompletedProcess[str]:
    """Run the hook on a Stop payload, as claude delivers it."""
    return subprocess.run(
        ["bash", str(_STOP_NUDGE)],
        input=json.dumps({"hook_event_name": "Stop", **payload}),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_chat_agent_is_asked_at_the_end_of_a_turn(chat_env: dict[str, str]) -> None:
    result = _run(chat_env, stop_hook_active=False)

    assert result.returncode == 2
    assert result.stderr != ""


def test_a_payload_with_no_stop_hook_active_field_still_asks(
    chat_env: dict[str, str],
) -> None:
    """An absent field is a first stop, not a continuation; `// false` must not read as true."""
    assert _run(chat_env).returncode == 2


def test_the_continuation_this_hook_caused_is_let_through(
    chat_env: dict[str, str],
) -> None:
    """The loop break. claude sets `stop_hook_active` while continuing from a stop
    hook, so the agent stops for real whether it sent the notification or declined."""
    result = _run(chat_env, stop_hook_active=True)

    assert result.returncode == 0
    assert result.stderr == ""


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
def test_only_a_chat_agent_is_asked(
    chat_env: dict[str, str], variable: str, value: str
) -> None:
    """A worker reports through its lead's chat, and a subagent's turn ends inside its parent's."""
    result = _run({**chat_env, variable: value}, stop_hook_active=False)

    assert result.returncode == 0
    assert result.stderr == ""
