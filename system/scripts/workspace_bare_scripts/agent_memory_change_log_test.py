"""Claude's memory change-log hooks record a turn's memory changes (one per note) and tell the agent about each.

The grouping, per-session and undo rules are the library's (system/libs/agent_memory,
changes_test.py); this checks the hook wiring around them: the payload fields it reads, the
note it prints, that it never fails the tool call, and the hooks run exactly as
``.claude/settings.json`` wires them.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from workspace_bare_scripts import agent_memory_change_log as hooks

_SCRIPT_NAME = "agent_memory_change_log.py"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "memories").mkdir()
    return {
        "AGENT_MEMORY_DIR": str(tmp_path / "memories"),
        "AGENT_MEMORY_CHANGES_DIR": str(tmp_path / "changes"),
        "MINDS_CHAT_ID": "chat-1",
        "MNGR_AGENT_NAME": "Chat-1",
    }


def _hook(environ: dict[str, str], command: str, payload: dict[str, Any]) -> str:
    """What the hook prints for ``payload``; it always exits 0."""
    stdout = io.StringIO()
    code = hooks.main(
        [command],
        environ,
        io.StringIO(json.dumps({"session_id": "s1", **payload})),
        stdout,
    )
    assert code == 0
    return stdout.getvalue()


def _context(output: str) -> str:
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


def test_a_memory_write_is_recorded_against_its_tool_call_and_the_agent_is_told(
    environ: dict[str, str],
) -> None:
    memories = Path(environ["AGENT_MEMORY_DIR"])
    assert _hook(environ, "sync", {}) == ""
    (memories / "tea.md").write_text("likes tea")
    output = _hook(
        environ,
        "post",
        {
            "tool_name": "Write",
            "tool_use_id": "toolu_1",
            "tool_input": {"file_path": str(memories / "tea.md")},
        },
    )
    note = _context(output)
    assert 'added the note "Tea"' in note and "Undo" in note
    [record] = list(Path(environ["AGENT_MEMORY_CHANGES_DIR"]).glob("*.json"))
    change = json.loads(record.read_text())
    assert change["tool_use_ids"] == ["toolu_1"]
    assert (change["chat_id"], change["agent_name"], change["session_id"]) == (
        "chat-1",
        "Chat-1",
        "s1",
    )


def test_a_call_that_changes_nothing_prints_nothing(environ: dict[str, str]) -> None:
    _hook(environ, "sync", {})
    output = _hook(
        environ, "post", {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    )
    assert output == ""


def test_a_broken_payload_never_fails_the_tool_call(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    stdout = io.StringIO()
    assert hooks.main(["post"], environ, io.StringIO("not json"), stdout) == 0
    assert stdout.getvalue() == ""
    assert "not JSON" in capsys.readouterr().err


def test_a_memory_folder_it_cannot_write_never_fails_the_tool_call(
    environ: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The changes dir sits under a file, so writing the baseline fails.
    (tmp_path / "blocker").write_text("")
    blocked = {
        **environ,
        "AGENT_MEMORY_CHANGES_DIR": str(tmp_path / "blocker" / "changes"),
    }
    assert _hook(blocked, "sync", {}) == ""
    assert "could not start the turn" in capsys.readouterr().err


def test_an_index_edit_touching_two_notes_names_each_change_on_its_own_line(
    environ: dict[str, str],
) -> None:
    memories = Path(environ["AGENT_MEMORY_DIR"])
    (memories / "MEMORY.md").write_text("# Index\n")
    _hook(environ, "sync", {})
    (memories / "tea.md").write_text("likes tea")
    (memories / "coffee.md").write_text("no coffee")
    (memories / "MEMORY.md").write_text(
        "# Index\n- [Tea](tea.md) — tea\n- [Coffee](coffee.md) — coffee\n"
    )
    output = _hook(
        environ,
        "post",
        {
            "tool_name": "Bash",
            "tool_use_id": "toolu_2",
            "tool_input": {"command": f"ls {memories}"},
        },
    )
    lines = _context(output).splitlines()
    assert len(lines) == 2
    assert all(line.startswith("MEMORY CHANGED (") for line in lines)
    assert sorted(
        line.split("you ", 1)[1].split(". The chat", 1)[0] for line in lines
    ) == [
        'added the note "Coffee" and its index line',
        'added the note "Tea" and its index line',
    ]
    assert len(list(Path(environ["AGENT_MEMORY_CHANGES_DIR"]).glob("*.json"))) == 2


def _wired_command(event: str) -> str:
    """The hook command ``.claude/settings.json`` runs for ``event``."""
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    [command] = [
        hook["command"]
        for matcher in settings["hooks"][event]
        for hook in matcher["hooks"]
        if _SCRIPT_NAME in hook["command"]
    ]
    return command


def _run_wired_hook(
    event: str, payload: str, environ: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", _wired_command(event)],
        input=payload,
        text=True,
        env={**os.environ, **environ, "CLAUDE_PROJECT_DIR": str(_REPO_ROOT)},
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_the_wired_hooks_record_a_write_under_the_bare_python3(
    environ: dict[str, str],
) -> None:
    memories = Path(environ["AGENT_MEMORY_DIR"])
    started = _run_wired_hook(
        "UserPromptSubmit", json.dumps({"session_id": "s1"}), environ
    )
    assert (started.returncode, started.stdout, started.stderr) == (0, "", "")
    (memories / "tea.md").write_text("likes tea")
    payload = {
        "session_id": "s1",
        "tool_name": "Write",
        "tool_use_id": "toolu_1",
        "tool_input": {"file_path": str(memories / "tea.md")},
    }
    posted = _run_wired_hook("PostToolUse", json.dumps(payload), environ)
    assert posted.returncode == 0, posted.stderr
    assert 'added the note "Tea"' in _context(posted.stdout)


def test_the_wired_post_hook_exits_0_on_a_broken_payload(
    environ: dict[str, str],
) -> None:
    finished = _run_wired_hook("PostToolUse", "not json", environ)
    assert (finished.returncode, finished.stdout) == (0, "")
