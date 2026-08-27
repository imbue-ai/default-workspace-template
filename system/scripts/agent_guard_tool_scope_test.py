"""The shell guards must police SHELL calls only.

codex delivers `apply_patch` through the same PreToolUse hook as its shell tool, with the PATCH
BODY in `.tool_input.command` (measured against codex-cli 0.147.0). Without a tool-name gate,
editing any file whose CONTENTS contain a `| head` is hard-blocked and the whole code-mode
program aborts -- a live failure on a legitimate edit, caused by a guard aimed at commands.
"""

import json
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent
# The two guards that inspect a command. The other two already gated on tool_name.
_COMMAND_GUARDS = ("agent_block_pipe_tail_head.sh", "agent_prevent_commit_rewrite.sh")

_PATCH_BODY = "*** Begin Patch\n*** Update File: README.md\n+Run: cat foo | head -5\n+git rebase is discussed here\n*** End Patch"


def _run(guard: str, payload: dict) -> int:
    return subprocess.run(
        ["bash", str(_SCRIPTS / guard)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


@pytest.mark.parametrize("guard", _COMMAND_GUARDS)
def test_a_file_edit_carrying_a_flagged_string_is_not_blocked(guard: str) -> None:
    """codex's apply_patch is not a shell call, however command-shaped its payload looks."""
    assert (
        _run(
            guard, {"tool_name": "apply_patch", "tool_input": {"command": _PATCH_BODY}}
        )
        == 0
    )


@pytest.mark.parametrize("guard", _COMMAND_GUARDS)
def test_a_non_shell_tool_is_never_policed(guard: str) -> None:
    assert (
        _run(
            guard,
            {"tool_name": "update_plan", "tool_input": {"command": "ls | head -5"}},
        )
        == 0
    )


def test_a_real_shell_call_is_still_blocked() -> None:
    assert (
        _run(
            "agent_block_pipe_tail_head.sh",
            {"tool_name": "Bash", "tool_input": {"command": "ls | head -5"}},
        )
        == 2
    )
    assert (
        _run(
            "agent_prevent_commit_rewrite.sh",
            {"tool_name": "Bash", "tool_input": {"command": "git rebase -i HEAD~2"}},
        )
        == 2
    )


@pytest.mark.parametrize("guard", _COMMAND_GUARDS)
def test_a_payload_with_no_tool_name_is_still_policed(guard: str) -> None:
    """The gate must not become a way to opt out. A payload that names no tool is treated as a
    shell call, which is also what agy's shim produces on older paths."""
    command = "ls | head -5" if "pipe" in guard else "git rebase -i HEAD~2"
    assert _run(guard, {"tool_input": {"command": command}}) == 2
