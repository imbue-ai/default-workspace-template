"""Unit tests for opencode's tool-call labels."""

from __future__ import annotations

import json

from imbue.system_interface.harnesses.opencode.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.opencode.tool_labels import tool_labels


def _preview(**kwargs: object) -> str:
    return json.dumps(kwargs)


def test_read_labels_header_and_caption() -> None:
    header, caption = tool_labels("read", _preview(filePath="/home/user/workspace/foo.py"))
    assert header == "Tool: Read"
    assert caption == "Reading foo.py"


def test_bash_captions_the_command_not_a_description() -> None:
    # opencode's bash has no description field (verified), so the command is the target.
    header, caption = tool_labels("bash", _preview(command="ls -la"))
    assert header == "Tool: Bash"
    assert caption == "Running ls -la"


def test_grep_and_glob_quote_the_pattern() -> None:
    _, grep_caption = tool_labels("grep", _preview(pattern="TODO"))
    _, glob_caption = tool_labels("glob", _preview(pattern="**/*.py"))
    assert grep_caption == 'Searching "TODO"'
    assert glob_caption == 'Searching "**/*.py"'


def test_edit_write_list_targets() -> None:
    assert tool_labels("edit", _preview(filePath="/a/beta.py")) == ("Tool: Edit", "Editing beta.py")
    assert tool_labels("write", _preview(filePath="/a/delta.txt")) == ("Tool: Write", "Writing delta.txt")
    assert tool_labels("list", _preview(path="/a/sub")) == ("Tool: List", "Listing sub")


def test_webfetch_and_websearch() -> None:
    _, fetch = tool_labels("webfetch", _preview(url="https://example.com"))
    _, search = tool_labels("websearch", _preview(query="opencode docs"))
    assert fetch == "Fetching page https://example.com"
    assert search == 'Searching the web "opencode docs"'


def test_skill_and_todowrite() -> None:
    assert tool_labels("skill", _preview(name="deep-research")) == ("Tool: Skill", "Loading skill deep-research")
    header, caption = tool_labels("todowrite", _preview(todos=[]))
    assert header == "Tool: TodoWrite"
    # no target key on a todo write -> the verb takes the no-target ellipsis.
    assert caption == "Updating todos…"


def test_task_captions_as_delegation() -> None:
    header, caption = tool_labels("task", _preview(subagent_type="general", description="do a thing"))
    assert header == "Tool: Task"
    assert caption == "Delegating to sub-agent…"


def test_mcp_tool_falls_back_to_raw_id_and_generic_caption() -> None:
    # opencode names MCP tools <server>_<tool>; there is no reliable split, so it falls to the
    # honest raw-id header + generic caption (NOT the mcp__ shared helper, which won't match).
    header, caption = tool_labels("linear_create_issue", _preview(title="x"))
    assert header == "Tool: linear_create_issue"
    assert caption == "Running tool…"


def test_unknown_tool_without_target_is_generic() -> None:
    assert tool_labels("mystery", "not json") == ("Tool: mystery", "Running tool…")


def test_keeps_full_input_for_tk_lifecycle_bash() -> None:
    assert keeps_full_tool_input("bash", _preview(command='tk create --step "A very long plan title"'))
    assert not keeps_full_tool_input("bash", _preview(command="ls -la"))
    # a tk close merely mentioned in a quoted arg is not a real lifecycle call
    assert not keeps_full_tool_input("bash", _preview(command='echo "remember to tk close s1"'))
    # only bash carries tk commands
    assert not keeps_full_tool_input("edit", _preview(command="tk create --step x"))
