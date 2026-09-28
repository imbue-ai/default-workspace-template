"""Tests for the step-tracking PreToolUse reminder.

The reminder nudges an agent that is doing substantive work without having declared any step
records. A build's plan-node worker is exempt: it keeps no records at all, because the only
progress timeline the user sees belongs to the orchestrator that launched it, and that timeline
shows stages of the build rather than nodes. Without the exemption the worker would be nudged on
every call to do the one thing its task file forbids.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = Path(__file__).parent / "agent_require_steps_pretool.sh"
# Substantive enough to be judged: not a read-only command, not a tk call, not the plan recorder.
_WORK = json.dumps({"tool_name": "Bash", "tool_input": {"command": "python3 build_something.py"}})


def _run(tickets_dir: Path, **env: str) -> str:
    """The hook's stdout for one tool call, which is the reminder or nothing at all."""
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        input=_WORK,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "MNGR_AGENT_WORK_DIR": str(_REPO_ROOT),
            "TICKETS_DIR": str(tickets_dir),
            **env,
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_reminds_an_agent_with_no_steps(tmp_path: Path) -> None:
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    assert "Step tracking reminder" in _run(tickets)


def test_says_nothing_to_a_plan_node_worker(tmp_path: Path) -> None:
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    assert _run(tickets, MNGR_AGENT_ROLE="worktree_worker") == ""


def test_still_reminds_other_roles(tmp_path: Path) -> None:
    """The exemption is for the one role that keeps no records, not for any worker."""
    tickets = tmp_path / "tickets"
    tickets.mkdir()
    assert "Step tracking reminder" in _run(tickets, MNGR_AGENT_ROLE="worker")
