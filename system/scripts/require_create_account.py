#!/usr/bin/env python3
"""Refuse a `mngr create` run inside the workspace while no provider account is signed in.

mngr runs this from the project root before every `create` (`[pre_command_scripts]` in
`.mngr/settings.toml`). The committed settings name no default agent type: the type, and the
account binding that goes with it, come from `.mngr/settings.local.toml`, which the chat app
writes from the account store. Without that file an unqualified create fails on mngr's own
"No agent type provided" and its config-set hint, which is the wrong advice here; the fix is
to sign in, and that is what this says instead.

Only a create run from inside the workspace is gated: one running in an agent's environment
(`MNGR_AGENT_ID`, which mngr sources for every shell, service, cron job and `mngr exec` here)
from that agent's own checkout. The create of the workspace itself runs from a checkout of
this template on the user's machine, outside any agent, and is never refused. A create that
names its own `--type` cannot be told apart here and is refused too while nothing is signed
in, which is the state every agent in the workspace is unusable in anyway.

Standard library only: it runs before any venv exists. `tomllib` is imported only past the
gate: the create of the workspace itself runs this under whatever `python3` the user's machine
has (the 3.9 of macOS's Command Line Tools has no `tomllib`), and that run must exit 0 before
touching anything the container's 3.12 provides.
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
