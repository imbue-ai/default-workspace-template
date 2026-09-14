#!/usr/bin/env python3
"""Refuse a `mngr create` run inside the workspace while no provider account is signed in.

mngr runs this from the project root before every `create` (`[pre_command_scripts]` in
`.mngr/settings.toml`). The committed settings name no default agent type: the type, and the
account binding that goes with it, come from `.mngr/settings.local.toml`, which the chat app
writes from the account store. Without that file an unqualified create fails on mngr's own
"No agent type provided" and its config-set hint, which is the wrong advice here; the fix is
to sign in, and that is what this says instead.

The gate is on one thing: the create's project root is the work dir of the agent running it
(`MNGR_AGENT_WORK_DIR` under `MNGR_AGENT_ID`, which mngr sources for every shell, service,
cron job and `mngr exec` here). That is what a create from inside the workspace looks like.
The create of the workspace itself runs on the user's machine from a clone of this template
that is nobody's work dir, and is never refused; neither is a create an agent on that machine
runs from a checkout other than its own, which is how this repo is normally worked on -- an
agent in the mngr monorepo, cd'd into a worktree of this one. An agent whose own work dir *is*
a checkout of this template is gated exactly like one inside the workspace, and on a machine
with no account store nothing there will ever satisfy it, so work on this repo from the
monorepo. A create that names its own `--type` cannot be told apart here and is refused too
while nothing is signed in, which is the state every agent in the workspace is unusable in
anyway.

Standard library only: it runs before any venv exists. `MNGR_AGENT_ID` is set for any mngr
agent, on the user's machine as much as in the container, so the settings entry's shell test
spares only a plain user shell the `python3` -- an agent-run create on a Mac does start one,
under whatever `python3` that agent has. `tomllib` is imported only past the gate so that run
exits 0 without needing a version the 3.9 of macOS's Command Line Tools lacks. The one path
that reaches the import on such a Mac is the gated one above, where the create was going to
fail regardless and does, on the ImportError rather than on the message.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NO_ACCOUNT_MESSAGE = "No provider account is signed in on this machine. Sign in from a chat tab, then try again."

_DEFAULT_PROJECT_CONFIG_DIR = Path(".mngr")
_LOCAL_SETTINGS_FILENAME = "settings.local.toml"


def _is_inside_workspace(environ: dict[str, str], cwd: Path) -> bool:
    """Whether this create runs in an agent's environment, from that agent's checkout."""
    if not environ.get("MNGR_AGENT_ID"):
        return False
    work_dir = environ.get("MNGR_AGENT_WORK_DIR", "")
    if not work_dir:
        return False
    return Path(work_dir).resolve() == cwd.resolve()


def _local_settings_path(environ: dict[str, str]) -> Path:
    override = environ.get("MNGR_PROJECT_CONFIG_DIR", "").strip()
    config_dir = Path(override) if override else _DEFAULT_PROJECT_CONFIG_DIR
    return config_dir / _LOCAL_SETTINGS_FILENAME


def _names_default_type(path: Path) -> bool:
    import tomllib

    if not path.is_file():
        return False
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    create = raw.get("commands", {}).get("create", {})
    agent_type = create.get("type") if isinstance(create, dict) else None
    return isinstance(agent_type, str) and bool(agent_type)


def main(environ: dict[str, str], cwd: Path) -> int:
    if not _is_inside_workspace(environ, cwd):
        return 0
    if _names_default_type(_local_settings_path(environ)):
        return 0
    print(NO_ACCOUNT_MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(dict(os.environ), Path.cwd()))
