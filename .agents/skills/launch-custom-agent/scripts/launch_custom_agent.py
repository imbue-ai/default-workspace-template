"""Launch a custom coding agent: a chat working in the user's own repo or directory.

Run from the workspace root (/home/user/workspace), which is where mngr resolves the
workspace's agent types, role templates, and default provider account:

    uv run python .agents/skills/launch-custom-agent/scripts/launch_custom_agent.py \
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
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn
from typing import Sequence

from imbue.chat import accounts
from imbue.chat.agent_discovery import agent_state_dir
from imbue.chat.agent_discovery import get_host_dir
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_account_binding
from imbue.mngr.primitives import AgentId

_CUSTOM_TEMPLATE = "custom"

# The harnesses a custom agent can run on: every real harness, not the chat app's
# pseudo-harness for pre-agent chat segments.
_LAUNCHABLE_HARNESSES = tuple(h for h in HarnessType if h is not HarnessType.SEED)

# See the module docstring: the custom template's deliberate aggregate replacements
# need the narrowing guard relaxed for this one create.
_NARROWING_ENV = {"MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING": "true"}


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs):
        return subprocess.run(list(argv), **kwargs)


def _fail(message: str) -> NoReturn:
    print(f"launch_custom_agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def _require_workspace_root() -> Path:
    """The cwd must be the workspace root: that is where mngr finds the workspace's
    project config (the custom template, the signed-in account defaults)."""
    cwd = Path.cwd()
    if not (cwd / ".mngr" / "settings.toml").is_file():
        _fail(
            f"run this from the workspace root (a directory with .mngr/settings.toml); cwd is {cwd}. "
            "The workspace's mngr config -- the custom template and the account defaults -- resolves from there."
        )
    return cwd


def _resolve_account(account_id: str, harness_name: str) -> accounts.Account:
    """The provider account the agent runs on: the named one, else the default on the
    named harness, else the workspace default (pinned, else most recent, else oldest)."""
    index = accounts.read_index()
    if account_id:
        account = next((a for a in index.accounts if a.id == account_id), None)
        if account is None:
            known = ", ".join(a.id for a in index.accounts) or "(none)"
            _fail(f"no provider account with id {account_id!r}; signed-in accounts: {known}")
        account_harness = accounts.harness_for(account)
        if account_harness is None:
            _fail(f"account {account_id!r} is on a lane this build does not support")
        if harness_name and account_harness != HarnessType(harness_name):
            _fail(
                f"account {account_id!r} runs on harness {account_harness}, not {harness_name!r}; "
                "name one or the other, not a conflicting pair"
            )
        return account
    if harness_name:
        harness = HarnessType(harness_name)
        candidates = tuple(a for a in index.accounts if accounts.harness_for(a) == harness)
        if not candidates:
            _fail(f"no signed-in account runs on harness {harness_name!r}; sign one in from a chat first")
        filtered = accounts.AccountIndex(
            version=index.version,
            accounts=candidates,
            mru=index.mru,
            default_account=index.default_account,
        )
        chosen = accounts.choose_default_account(filtered)
        assert chosen is not None, "choose_default_account returns an account when usable candidates exist"
        return chosen
    account = accounts.choose_default_account(index)
    if account is None:
        _fail("No provider account is signed in for this workspace. Sign in from a chat, then try again.")
    return account


def _validate_source(path: Path, mode: str) -> None:
    if not path.is_absolute():
        _fail(f"--path must be absolute, got {path}")
    if not path.is_dir():
        _fail(f"--path does not exist or is not a directory: {path}")
    if mode == "worktree":
        probe = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            _fail(f"worktree mode needs a git repository, and {path} is not one; use --mode in-place instead")


def _list_agent_records(runner: Runner) -> list[dict[str, object]]:
    """Every agent `mngr list` reports, as parsed jsonl records (non-JSON lines skipped)."""
    listing = runner.run(
        ["mngr", "list", "--format", "jsonl", "--on-error", "continue"],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    records: list[dict[str, object]] = []
    for line in (listing.stdout or "").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _existing_agent_names(runner: Runner) -> set[str]:
    names: set[str] = set()
    for record in _list_agent_records(runner):
        name = record.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def build_create_argv(
    name: str,
    source_path: Path,
    mode: str,
    harness: HarnessType,
    account_id: str,
    agent_id: str,
    binding_args: Sequence[str],
    display_name: str,
    branch: str,
    message: str,
) -> list[str]:
    argv = [
        "mngr",
        "create",
        name,
        "--id",
        agent_id,
        "--from",
        f":{source_path}",
        "--transfer",
        "git-worktree" if mode == "worktree" else "none",
        "--type",
        str(harness),
        "--template",
        _CUSTOM_TEMPLATE,
        "--label",
        "user_created=true",
        "--label",
        f"display_name={display_name}",
        "--label",
        f"custom_source={source_path}",
        "--label",
        f"account={account_id}",
        "--no-connect",
        "--format",
        "jsonl",
        *binding_args,
    ]
    if branch:
        argv += ["--branch", branch]
    if message:
        argv += ["--message", message]
    return argv


def _created_work_dir(runner: Runner, agent_id: str) -> str:
    for record in _list_agent_records(runner):
        if record.get("id") == agent_id:
            work_dir = record.get("work_dir")
            if isinstance(work_dir, str):
                return work_dir
    return "(unknown; see `mngr list`)"


def launch(
    name: str,
    source_path: Path,
    mode: str,
    account_id: str,
    harness_name: str,
    branch: str,
    display_name: str,
    message: str,
    runner: Runner | None = None,
) -> int:
    runner = runner or Runner()
    _require_workspace_root()
    _validate_source(source_path, mode)
    if name in _existing_agent_names(runner):
        _fail(f"an agent named {name!r} already exists; pick another name or `mngr destroy {name}` first")

    account = _resolve_account(account_id, harness_name)
    harness = accounts.harness_for(account)
    assert harness is not None, "_resolve_account only returns accounts on a supported lane"

    # Bind the agent to the account the way a chat is bound: claude rides an env
    # var; the other harnesses get a credential link provisioned over the new
    # agent's state dir, which is why the agent id is minted before the create.
    agent_id = str(AgentId())
    binding_args = build_account_binding(harness).create_args(
        accounts.account_dir(account.id), agent_state_dir(get_host_dir(), agent_id)
    )

    argv = build_create_argv(
        name=name,
        source_path=source_path,
        mode=mode,
        harness=harness,
        account_id=account.id,
        agent_id=agent_id,
        binding_args=binding_args,
        display_name=display_name or name,
        branch=branch,
        message=message,
    )
    env = {**os.environ, **_NARROWING_ENV}
    result = runner.run(argv, env=env, check=False)
    if result.returncode != 0:
        _fail(
            f"`mngr create {name}` failed with exit code {result.returncode}. "
            f"If a half-created agent is left behind, `mngr destroy {name}` cleans it up."
        )

    work_dir = _created_work_dir(runner, agent_id)
    print(f"Launched custom agent {name} ({agent_id}) on account {account.id} ({harness}).")
    print(f"  work dir: {work_dir}")
    print(f"  source: {source_path} ({mode})")
    print("The agent appears as a chat in the workspace UI.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="mngr agent name (also the worktree branch suffix)")
    parser.add_argument("--path", required=True, type=Path, help="absolute path to the user's repo or directory")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("worktree", "in-place"),
        help="worktree: own git worktree of --path; in-place: run directly in --path",
    )
    parser.add_argument("--account", default="", help="provider account id (default: the workspace default)")
    parser.add_argument(
        "--harness",
        default="",
        choices=("", *(str(h) for h in _LAUNCHABLE_HARNESSES)),
        help="harness to run on (default: the chosen account's harness)",
    )
    parser.add_argument("--branch", default="", help="mngr create --branch spec, e.g. 'main' or 'main:feature-x'")
    parser.add_argument("--display-name", default="", help="name shown in the chat list (default: --name)")
    message_group = parser.add_mutually_exclusive_group()
    message_group.add_argument("--message", default="", help="first message to send the agent")
    message_group.add_argument("--message-file", type=Path, help="file whose contents become the first message")
    options = parser.parse_args(argv)

    message = options.message
    if options.message_file is not None:
        if not options.message_file.is_file():
            _fail(f"--message-file does not exist: {options.message_file}")
        message = options.message_file.read_text()

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
