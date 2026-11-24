"""Launch a custom coding agent: a chat working in the user's own repo or directory.

Run from the workspace root (/home/user/workspace), which is where mngr resolves the
workspace's agent types, role templates, and default provider account:

    uv run --no-sync .agents/skills/launch-custom-agent/python/launch_custom_agent_skill/launch_custom_agent.py \
        --name my-task --path /home/user/projects/myrepo --mode worktree

The agent is created with the `custom` role template (committed in
.mngr/settings.toml): it runs vanilla in the chosen directory -- the user's own repo
conventions govern it, none of the workspace's -- with one appended context block
saying where it is running. `--mode` is required and explicit:

- worktree: the agent gets its own git worktree of --path, so parallel agents on the
  same repo do not collide. --path must be a git repository; uncommitted changes in
  it do not ride into the worktree.
- in-place: the agent runs directly in --path (it sees, and makes, uncommitted
  changes there). Works for non-git directories too.

The create runs with MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true: the custom
template deliberately replaces __extend-built agent-type aggregates (each harness's
workspace prompt block, codex's provisioning command), and mngr's narrowing guard
would otherwise reject those assigns.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--name",
        required=True,
        help="mngr agent name (also the worktree branch suffix)",
    )
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="absolute path to the user's repo or directory",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("worktree", "in-place"),
        help="worktree: own git worktree of --path; in-place: run directly in --path",
    )
    parser.add_argument(
        "--account",
        default="",
        help="provider account id (default: the workspace default)",
    )
    parser.add_argument(
        "--harness",
        default="",
        help="harness to run on (default: the chosen account's harness); an unknown "
        "name is refused with the list of known ones",
    )
    parser.add_argument(
        "--branch",
        default="",
        help="mngr create --branch spec, e.g. 'main' or 'main:feature-x'",
    )
    parser.add_argument(
        "--display-name",
        default="",
        help="name shown in the chat list (default: --name)",
    )
    message_group = parser.add_mutually_exclusive_group()
    message_group.add_argument(
        "--message", default="", help="first message to send the agent"
    )
    message_group.add_argument(
        "--message-file", type=Path, help="file whose contents become the first message"
    )
    options = parser.parse_args(argv)

    message = options.message
    if options.message_file is not None:
        if not options.message_file.is_file():
            print(
                f"launch_custom_agent: --message-file does not exist: {options.message_file}",
                file=sys.stderr,
            )
            return 1
        message = options.message_file.read_text()

    from launch_custom_agent_skill.launcher import launch

    return launch(
        name=options.name,
        source_path=options.path,
        mode=options.mode,
        account_id=options.account,
        harness_name=options.harness,
        branch=options.branch,
        display_name=options.display_name,
        message=message,
    )


if __name__ == "__main__":
    raise SystemExit(main())
