"""Tests for pi's tool-call labels and the tk-input-truncation exemption."""

from __future__ import annotations

import json

from imbue.system_interface.harnesses.pi_coding.tool_labels import keeps_full_tool_input
from imbue.system_interface.harnesses.pi_coding.tool_labels import tool_labels


def _preview(**arguments: object) -> str:
    return json.dumps(arguments)


def test_read_labels() -> None:
    header, caption = tool_labels("read", _preview(path="/home/user/workspace/README.md", limit=5))
    assert header == "Tool: Read"
    assert caption == "Reading README.md"


def test_bash_captions_the_command() -> None:
    # pi's bash has no `description`, unlike claude's; the command itself is the target.
    header, caption = tool_labels("bash", _preview(command="ls /home/user/workspace"))
    assert header == "Tool: Bash"
    assert caption == "Running ls /home/user/workspace"


def test_grep_quotes_the_pattern() -> None:
    header, caption = tool_labels("grep", _preview(pattern="TODO"))
    assert header == "Tool: Grep"
    assert caption == 'Searching "TODO"'


def test_unknown_tool_falls_back_to_name_and_generic() -> None:
    header, caption = tool_labels("weirdtool", _preview())
    assert header == "Tool: weirdtool"
    assert caption == "Running tool…"


def test_keeps_full_input_for_tk_lifecycle_bash() -> None:
    assert keeps_full_tool_input("bash", _preview(command='tk create --step "A long plan title"')) is True
    assert keeps_full_tool_input("bash", _preview(command="tk close wor-step-abcd 'done'")) is True


def test_does_not_keep_full_for_plain_bash() -> None:
    assert keeps_full_tool_input("bash", _preview(command="ls -la /home/user")) is False


def test_does_not_keep_full_for_non_bash_tools() -> None:
    assert keeps_full_tool_input("read", _preview(path="/x")) is False


def test_tk_mentioned_inside_a_quoted_argument_is_not_a_lifecycle_call() -> None:
    # shlex-aware recognition: a `tk close` merely echoed is NOT a real lifecycle call.
    assert keeps_full_tool_input("bash", _preview(command='echo "run tk close later"')) is False
