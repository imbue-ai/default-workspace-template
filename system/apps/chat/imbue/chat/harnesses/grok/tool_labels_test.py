"""Tests for grok's tool labels, held to the tool names and argument keys grok 1.0.62 declares."""

import json

import pytest

from imbue.chat.harnesses.grok.testing import GROK_FIXTURES_DIR
from imbue.chat.harnesses.grok.tool_labels import SHELL_COMMAND_KEY
from imbue.chat.harnesses.grok.tool_labels import SHELL_TOOL_NAMES
from imbue.chat.harnesses.grok.tool_labels import TOOL_LABELS
from imbue.chat.harnesses.grok.tool_labels import USE_TOOL_INNER_NAME_KEY
from imbue.chat.harnesses.grok.tool_labels import shell_command
from imbue.chat.harnesses.grok.tool_labels import tool_labels


def _labels(tool_name: str, raw_input: dict[str, object], native_label: str = "Native") -> tuple[str, str]:
    return tool_labels(tool_name, json.dumps(raw_input), native_label)


@pytest.mark.parametrize(
    ("tool_name", "raw_input", "expected"),
    [
        (
            "run_terminal_command",
            {"command": "pytest -q", "description": "run the tests"},
            ("Tool: Bash", "Running run the tests"),
        ),
        ("run_terminal_command", {"command": "pytest -q"}, ("Tool: Bash", "Running pytest -q")),
        (
            "monitor",
            {"command": "tail -f app.log", "description": "watch the log", "timeout_ms": 5000},
            ("Tool: Monitor", "Monitoring watch the log"),
        ),
        ("read_file", {"target_file": "/home/user/workspace/notes.md"}, ("Tool: Read", "Reading notes.md")),
        ("write", {"file_path": "src/app.py", "content": "x"}, ("Tool: Write", "Writing app.py")),
        (
            "search_replace",
            {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
            ("Tool: Edit", "Editing app.py"),
        ),
        ("grep", {"pattern": "TODO", "path": "src"}, ("Tool: Grep", 'Searching "TODO"')),
        ("list_dir", {"target_directory": "system/apps/chat/"}, ("Tool: List", "Listing chat/")),
        ("search_tool", {"query": "github issues"}, ("Tool: ToolSearch", 'Loading tool "github issues"')),
        ("get_command_or_subagent_output", {"task_ids": ["t1"]}, ("Tool: TaskOutput", "Retrieving results")),
        ("wait_commands_or_subagents", {}, ("Tool: TaskOutput", "Waiting for background work")),
        ("kill_command_or_subagent", {"task_id": "t1"}, ("Tool: TaskStop", "Stopping a background task")),
        (
            "scheduler_create",
            {"prompt": "check the build", "interval": "1h"},
            ("Tool: Schedule", 'Scheduling "check the build"'),
        ),
        ("scheduler_list", {}, ("Tool: Schedule", "Listing scheduled tasks")),
        ("scheduler_delete", {"task_id": "s1"}, ("Tool: Schedule", "Cancelling a scheduled task")),
        ("web_search", {}, ("Tool: WebSearch", "Searching the web")),
        ("web_fetch", {}, ("Tool: WebFetch", "Fetching page")),
    ],
)
def test_each_mapped_tool_reads_in_the_shared_vocabulary(
    tool_name: str, raw_input: dict[str, object], expected: tuple[str, str]
) -> None:
    assert _labels(tool_name, raw_input) == expected


def test_a_mapped_tool_without_its_target_argument_keeps_the_bare_verb() -> None:
    assert _labels("read_file", {}) == ("Tool: Read", "Reading…")
    assert _labels("grep", {"pattern": ""}) == ("Tool: Grep", "Searching…")


def test_spawn_subagent_reads_as_a_delegation_whatever_it_was_asked() -> None:
    assert _labels("spawn_subagent", {"prompt": "do it", "description": "fix the bug"}) == (
        "Tool: Agent",
        "Delegating to sub-agent…",
    )


def test_use_tool_reads_as_the_integration_tool_it_calls() -> None:
    assert _labels("use_tool", {"tool_name": "mcp__github__create_issue", "tool_input": {}}) == (
        "Tool: mcp__github__create_issue",
        "Running create issue",
    )
    assert _labels("use_tool", {"tool_name": "linear_search", "tool_input": {}}) == (
        "Tool: linear_search",
        "Running linear_search",
    )
    assert _labels("use_tool", {}, "Use Tool") == ("Tool: use_tool", "Use Tool")


def test_an_unmapped_tool_keeps_groks_own_label() -> None:
    assert _labels("todo_write", {"todos": []}, "Todo") == ("Tool: todo_write", "Todo")
    assert _labels("some_future_tool", {}, "") == ("Tool: some_future_tool", "Running tool…")
    assert _labels("", {}, "") == ("Tool", "Running tool…")


def test_shell_command_is_the_command_of_the_two_shell_tools_only() -> None:
    assert shell_command("run_terminal_command", json.dumps({"command": "tk create x"})) == "tk create x"
    assert shell_command("monitor", json.dumps({"command": "tail -f x.log"})) == "tail -f x.log"
    assert shell_command("use_tool", json.dumps({"tool_name": "run_terminal_command", "tool_input": {}})) is None
    assert shell_command("read_file", json.dumps({"target_file": "x"})) is None
    assert shell_command("run_terminal_command", "not json") is None


def _declared_arguments() -> dict[str, set[str]]:
    """Each tool's argument keys, from the tool definitions grok 1.0.62 recorded for a captured session."""
    definitions = json.loads(next(GROK_FIXTURES_DIR.rglob("tool_definitions.json")).read_text())
    return {
        definition["function"]["name"]: set(definition["function"]["parameters"].get("properties", {}))
        for definition in definitions
    }


def test_every_tool_and_argument_key_the_labels_read_is_one_grok_declares() -> None:
    declared = _declared_arguments()
    named_only = {"wait_commands_or_subagents", "web_fetch", "web_search"}

    for tool_name, label in TOOL_LABELS.items():
        if tool_name in named_only:
            assert tool_name not in declared
            assert label.target_keys == ()
            continue
        assert set(label.target_keys) <= declared[tool_name], tool_name
    for tool_name in SHELL_TOOL_NAMES:
        assert SHELL_COMMAND_KEY in declared[tool_name]
    assert USE_TOOL_INNER_NAME_KEY in declared["use_tool"]
    assert "spawn_subagent" in declared


def test_every_argument_a_captured_call_sent_is_one_grok_declares() -> None:
    declared = _declared_arguments()
    sent: dict[str, set[str]] = {}
    for path in GROK_FIXTURES_DIR.rglob("updates.jsonl"):
        for line in path.read_text().splitlines():
            update = json.loads(line)["params"]["update"]
            if update.get("sessionUpdate") == "tool_call":
                sent.setdefault(update["_meta"]["x.ai/tool"]["name"], set()).update(update["rawInput"])

    assert len(sent) == 14
    for tool_name, keys in sent.items():
        assert keys <= declared[tool_name], tool_name
