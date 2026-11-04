"""The require-a-step nudge (policy P5) spares the tools that cannot change anything."""

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS.parent.parent
_NUDGE = "[Step tracking reminder]"


def _run(tmp_path: Path, tool_name: str, tool_input: dict[str, object]) -> str:
    """The script's stdout for one call, by an agent that has declared no steps."""
    tickets = tmp_path / "tickets"
    tickets.mkdir(exist_ok=True)
    completed = subprocess.run(
        ["bash", str(_SCRIPTS / "agent_require_steps_pretool.sh")],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "MNGR_AGENT_WORK_DIR": str(_REPO_ROOT),
            "TICKETS_DIR": str(tickets),
        },
        timeout=30,
        check=True,
    )
    return completed.stdout


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("read_file", {"target_file": "README.md"}),
        ("list_dir", {"target_directory": "system"}),
        ("grep", {"pattern": "TODO", "path": "system"}),
        ("search_tool", {"query": "linear create issue"}),
        ("web_search", {"query": "ripgrep glob"}),
        ("web_fetch", {"url": "https://example.com"}),
        ("get_command_or_subagent_output", {"task_ids": ["t1"]}),
        ("wait_commands_or_subagents", {"task_ids": ["t1"]}),
        ("scheduler_list", {}),
        ("ask_user_question", {"questions": []}),
        ("enter_plan_mode", {}),
        ("exit_plan_mode", {}),
    ],
)
def test_a_grok_tool_that_cannot_change_anything_is_not_nudged(
    tmp_path: Path, tool_name: str, tool_input: dict[str, object]
) -> None:
    assert _run(tmp_path, tool_name, tool_input) == ""


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("write", {"file_path": "notes.md", "content": "x"}),
        (
            "search_replace",
            {"file_path": "notes.md", "old_string": "a", "new_string": "b"},
        ),
        # Shell tools reach the script as Bash; under their own names they are still work.
        ("run_terminal_command", {"command": "cat README.md", "description": "x"}),
        ("monitor", {"command": "tail -f build.log", "description": "x"}),
        ("todo_write", {"todos": []}),
        ("kill_command_or_subagent", {"task_id": "t1"}),
        ("spawn_subagent", {"prompt": "x", "description": "x"}),
        ("scheduler_create", {"prompt": "x", "interval": "1h"}),
        ("image_gen", {"prompt": "x"}),
        # An MCP tool called through use_tool arrives under its own name.
        (
            "linear__create_issue",
            {"tool_name": "linear__create_issue", "tool_input": {}},
        ),
    ],
)
def test_a_grok_tool_that_can_change_something_is_nudged(
    tmp_path: Path, tool_name: str, tool_input: dict[str, object]
) -> None:
    assert _NUDGE in _run(tmp_path, tool_name, tool_input)


def test_claudes_read_only_names_are_still_spared_and_its_edits_nudged(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, "Read", {"file_path": "README.md"}) == ""
    assert _NUDGE in _run(
        tmp_path, "Edit", {"file_path": "x", "old_string": "a", "new_string": "b"}
    )
