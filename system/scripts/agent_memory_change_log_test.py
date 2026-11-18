"""Claude's memory change-log hooks record a turn's memory changes (one per note) and tell the agent about each.

The grouping, per-session and undo rules are the library's (system/libs/agent_memory,
changes_test.py); this checks the hook wiring around them: the payload fields it reads, the
note it prints, and that it never fails the tool call.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "agent_memory_change_log.py"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "memories").mkdir()
    return {
        **os.environ,
        "AGENT_MEMORY_DIR": str(tmp_path / "memories"),
        "AGENT_MEMORY_CHANGES_DIR": str(tmp_path / "changes"),
        "MINDS_CHAT_ID": "chat-1",
        "MNGR_AGENT_NAME": "Chat-1",
    }


def _hook(env: dict[str, str], mode: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_SCRIPT), mode],
        input=json.dumps({"session_id": "s1", **payload}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_a_memory_write_is_recorded_against_its_tool_call_and_the_agent_is_told(
    env: dict[str, str],
) -> None:
    memories = Path(env["AGENT_MEMORY_DIR"])
    assert _hook(env, "sync", {}).returncode == 0
    (memories / "tea.md").write_text("likes tea")
    result = _hook(
        env,
        "post",
        {
            "tool_name": "Write",
            "tool_use_id": "toolu_1",
            "tool_input": {"file_path": str(memories / "tea.md")},
        },
    )
    assert result.returncode == 0
    note = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'added the note "Tea"' in note and "Undo" in note
    [record] = list(Path(env["AGENT_MEMORY_CHANGES_DIR"]).glob("*.json"))
    change = json.loads(record.read_text())
    assert change["tool_use_ids"] == ["toolu_1"]
    assert (change["chat_id"], change["agent_name"], change["session_id"]) == (
        "chat-1",
        "Chat-1",
        "s1",
    )


def test_a_call_that_changes_nothing_prints_nothing(env: dict[str, str]) -> None:
    _hook(env, "sync", {})
    result = _hook(
        env, "post", {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    )
    assert (result.returncode, result.stdout) == (0, "")


def test_a_broken_payload_never_fails_the_tool_call(env: dict[str, str]) -> None:
    result = subprocess.run(
        ["python3", str(_SCRIPT), "post"],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0


def test_an_index_edit_touching_two_notes_names_each_change_on_its_own_line(
    env: dict[str, str],
) -> None:
    memories = Path(env["AGENT_MEMORY_DIR"])
    (memories / "MEMORY.md").write_text("# Index\n")
    _hook(env, "sync", {})
    (memories / "tea.md").write_text("likes tea")
    (memories / "coffee.md").write_text("no coffee")
    (memories / "MEMORY.md").write_text(
        "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n"
    )
    result = _hook(
        env,
        "post",
        {
            "tool_name": "Bash",
            "tool_use_id": "toolu_2",
            "tool_input": {"command": f"ls {memories}"},
        },
    )
    lines = json.loads(result.stdout)["hookSpecificOutput"][
        "additionalContext"
    ].splitlines()
    assert len(lines) == 2
    assert all(line.startswith("MEMORY CHANGED (") for line in lines)
    assert sorted(
        line.split("you ", 1)[1].split(". The chat", 1)[0] for line in lines
    ) == [
        'added the note "Coffee" and its index line',
        'added the note "Tea" and its index line',
    ]
    assert len(list(Path(env["AGENT_MEMORY_CHANGES_DIR"]).glob("*.json"))) == 2
