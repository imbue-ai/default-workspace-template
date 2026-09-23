"""A pipe into tail/head is blocked unless it reads a file with `cat`."""

import pytest
from guard_testing import run_guard


def _run(command: str) -> int:
    return run_guard(
        "agent_block_pipe_tail_head.sh",
        {"tool_name": "Bash", "tool_input": {"command": command}},
    )


@pytest.mark.parametrize(
    "command",
    [
        "cat notes.md | head -120",
        "cat a.md b.md 2>&1 | head -120",
        "cat log.txt|tail",
        "cd /tmp && cat out.txt | tail -20",
        "ls; cat out.txt | head",
        "echo $(cat f | head -1)",
        "cd /tmp && { cat out.txt | tail -20; }",
        "cd /tmp\ncat out.txt | tail -20",
    ],
)
def test_a_cat_of_files_may_pipe_into_head_or_tail(command: str) -> None:
    assert _run(command) == 0


@pytest.mark.parametrize(
    "command",
    [
        "pytest | tail -20",
        # The cat is reading pytest's output, not a file.
        "pytest | cat | tail -20",
        "pytest |& cat | tail -20",
        "pytest | (cat | tail -20)",
        "pytest | { cat | tail -20; }",
        "pytest > >(cat | tail -20)",
        # A trailing pipe or backslash continues the pipeline onto the next line.
        "pytest |\ncat | tail -20",
        "pytest | \\\ncat | tail -20",
        # One exempt pipe does not excuse another in the same command.
        "cat f | head; pytest | tail -5",
        "cat f && pytest | tail -5",
        "concatenate f | head",
        "cat f | grep x | head",
    ],
)
def test_any_other_pipe_into_head_or_tail_is_blocked(command: str) -> None:
    assert _run(command) == 2
