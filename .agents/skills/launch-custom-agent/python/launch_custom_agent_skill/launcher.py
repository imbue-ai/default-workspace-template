"""The launch behind ``launch_custom_agent.py``: validates the inputs, binds the agent to a
provider account the way a chat is bound, and runs the ``mngr create``.

Its own module so the entry's ``--help`` never loads the chat app and mngr (see
``.agents/shared/references/running-python.md``, "Keep startup imports light").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Sequence

from imbue.chat import accounts
from imbue.chat.agent_discovery import agent_state_dir, get_host_dir
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_account_binding
from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs

_CUSTOM_TEMPLATE = "custom"

# Where a custom agent's worktree lands: an explicit --target-path under the same base
# the workspace's `worktree_base_folder` setting names, passed so the work dir is known
# BEFORE the create (claude's folder-trust seeding below needs the path up front; an
# mngr-generated worktree path is only knowable afterwards).
_WORKTREE_BASE = Path("/home/user/worktrees")

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
            _fail(
                f"no provider account with id {account_id!r}; signed-in accounts: {known}"
            )
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
        candidates = tuple(
            a for a in index.accounts if accounts.harness_for(a) == harness
        )
        if not candidates:
            _fail(
                f"no signed-in account runs on harness {harness_name!r}; sign one in from a chat first"
            )
        filtered = accounts.AccountIndex(
            version=index.version,
            accounts=candidates,
            mru=index.mru,
            default_account=index.default_account,
        )
        chosen = accounts.choose_default_account(filtered)
        assert chosen is not None, (
            "choose_default_account returns an account when usable candidates exist"
        )
        return chosen
    account = accounts.choose_default_account(index)
    if account is None:
        _fail(
            "No provider account is signed in for this workspace. Sign in from a chat, then try again."
        )
    return account


def _validate_source(path: Path, mode: str, branch: str) -> None:
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
            _fail(
                f"worktree mode needs a git repository, and {path} is not one; use --mode in-place instead"
            )
    elif branch:
        # In-place mode never touches the directory's checkout, and mngr ignores --branch
        # outright for --transfer none -- so honoring the request is impossible and
        # dropping it silently would mislead the caller.
        _fail(
            "--branch only applies to worktree mode; in-place mode runs on whatever is checked out in --path"
        )


def _list_agent_records(runner: Runner) -> tuple[int, list[dict[str, object]]]:
    """`mngr list`'s exit code, and every agent it reports as parsed jsonl records
    (non-JSON lines skipped; per-agent errors already ride --on-error continue)."""
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
    return listing.returncode, records


def _existing_agent_names(runner: Runner) -> set[str]:
    """The names `mngr list` reports. An unusable listing fails the launch outright:
    an empty parse of a failed listing reads as "no agents", which would silently
    disable the taken-name guard."""
    returncode, records = _list_agent_records(runner)
    if returncode != 0:
        _fail(
            f"`mngr list` failed with exit code {returncode}, so the agent name cannot be checked for conflicts"
        )
    names: set[str] = set()
    for record in records:
        name = record.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def build_create_argv(
    name: str,
    source_path: Path,
    mode: str,
    work_dir: Path,
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
        *(["--target-path", str(work_dir)] if mode == "worktree" else []),
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
    if mode == "worktree":
        # Explicit, not inferred: the workspace's `[commands.create] ensure_clean = false`
        # default flips mngr's inferred include-unclean to true, which would rsync the
        # source repo's modified and untracked files into the fresh worktree -- the
        # opposite of what this mode promises the user.
        argv += ["--exclude-unclean"]
    if branch:
        argv += ["--branch", branch]
    if message:
        argv += ["--message", message]
    return argv


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
    known_harnesses = tuple(str(h) for h in _LAUNCHABLE_HARNESSES)
    if harness_name and harness_name not in known_harnesses:
        _fail(
            f"unknown harness {harness_name!r}; choose one of: {', '.join(known_harnesses)}"
        )
    _validate_source(source_path, mode, branch)
    if name in _existing_agent_names(runner):
        _fail(
            f"an agent named {name!r} already exists; pick another name or `mngr destroy {name}` first"
        )

    account = _resolve_account(account_id, harness_name)
    harness = accounts.harness_for(account)
    assert harness is not None, (
        "_resolve_account only returns accounts on a supported lane"
    )

    # The agent's work dir is known before the create: the source itself in-place, or
    # an explicit worktree target (mngr would otherwise generate the path, and claude's
    # trust seeding below needs it up front).
    work_dir = source_path if mode == "in-place" else _WORKTREE_BASE / name
    if mode == "worktree" and work_dir.exists():
        _fail(f"{work_dir} already exists; remove it or pick another --name")

    # Bind the agent to the account the way a chat is bound: claude rides an env
    # var; the other harnesses get a credential link provisioned over the new
    # agent's state dir, which is why the agent id is minted before the create.
    agent_id = str(AgentId())
    account_dir = accounts.account_dir(account.id)
    binding_args = build_account_binding(harness).create_args(
        account_dir, agent_state_dir(get_host_dir(), agent_id)
    )

    # Pre-trust the work dir for claude, the same way the account seeding trusts the
    # workspace checkout: without it, claude opens its folder-trust dialog in the new
    # directory and the create's first-message send refuses while it is up.
    if harness is HarnessType.CLAUDE:
        auto_dismiss_claude_dialogs(account_dir / ".claude.json", work_dir)

    argv = build_create_argv(
        name=name,
        source_path=source_path,
        mode=mode,
        work_dir=work_dir,
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

    print(
        f"Launched custom agent {name} ({agent_id}) on account {account.id} ({harness})."
    )
    print(f"  work dir: {work_dir}")
    print(f"  source: {source_path} ({mode})")
    print("The agent appears as a chat in the workspace UI.")
    return 0
