"""A pipe into tail/head is blocked unless the whole command is a `cat` of files piped into it."""

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
        "  cat out.txt | tail -n 20 > /tmp/last.txt",
        "cat build.log | tail -50 &>/tmp/last.txt",
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
        "pytest > >(cat | tail -20)",
        "pytest |\ncat | tail -20",
        "cat notes.md\npytest | tail -20",
        "cat $(pytest) | head",
        "cat <(pytest) | head",
        "cat `pytest` | head",
        # The exemption covers a whole command, never one pipeline inside a compound one.
        "cd /tmp && cat out.txt | tail -20",
        "cat f | head; pytest | tail -5",
        "cat f && pytest | tail -20",
        "cat f & pytest | tail -20",
        # The escaped `>` leaves `&` a background operator, not part of a `>&` redirect.
        "cat x\\>& pytest | tail -20",
        "cat f | head -5 | tail -2",
        "cat f | grep x | head",
        "concatenate f | head",
    ],
)
def test_any_other_pipe_into_head_or_tail_is_blocked(command: str) -> None:
    assert _run(command) == 2
