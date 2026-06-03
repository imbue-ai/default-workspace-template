"""Contract tests: agent_manager's mngr argv builders vs the live mngr CLI.

agent_manager shells out to ``mngr create`` (worktree + chat) and
``mngr observe``. Previously these argvs were only checked at ``cmd[0]`` (the
binary path), so a vendor/mngr CLI rename would not have been caught here --
the same blind spot that shipped the broken ``mngr push`` (PR 77). These tests
confront the actual builder output with ``imbue.mngr.main.cli`` so a dropped
subcommand or renamed flag fails the build.
"""

from __future__ import annotations

from pathlib import Path

from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.system_interface.agent_manager import _build_chat_create_command
from imbue.system_interface.agent_manager import _build_observe_command_argv
from imbue.system_interface.agent_manager import _build_worktree_create_command


def test_worktree_create_argv_accepted_by_live_cli() -> None:
    argv = _build_worktree_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        current_branch="main",
        new_branch="mngr/demo",
        parent_labels={"project": "proj"},
    )
    assert_mngr_argv_valid(argv)


def test_worktree_create_argv_without_project_label() -> None:
    argv = _build_worktree_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        current_branch="main",
        new_branch="mngr/demo",
        parent_labels={},
    )
    assert_mngr_argv_valid(argv)


def test_chat_create_argv_accepted_by_live_cli() -> None:
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"workspace": "ws", "project": "proj"},
    )
    assert_mngr_argv_valid(argv)


def test_observe_argv_accepted_by_live_cli() -> None:
    argv = _build_observe_command_argv("mngr", Path("/tmp/events"))
    assert_mngr_argv_valid(argv)
