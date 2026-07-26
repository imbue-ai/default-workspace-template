#!/usr/bin/env python3
"""Migrate this workspace's Claude auth from the mngr host env file to settings.json.

Older workspaces received ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` via
the mngr host env file (``$MNGR_HOST_DIR/env``), which every process freezes
at start. Auth now lives in the ``env`` block of the shared
``$CLAUDE_CONFIG_DIR/settings.json`` (written by the in-UI sign-in modal),
so changing credentials only requires restarting claude agents -- never the
services agent. This script performs the one-time move:

1. Copy any managed auth keys (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
   ``CLAUDE_CODE_OAUTH_TOKEN``) from the host env file into the settings env
   block -- unless the settings block already holds a credential, which then
   wins (the modal may already have been used).
2. Scrub those keys from the host env file so a stale value can never shadow
   a future settings-managed credential in any process environment.
3. Restart the workspace's claude-binary agents so they pick up the settings
   credentials, messaging previously-RUNNING agents to continue.

Subscription-based workspaces (no key in the host env) need no migration:
their existing ``.credentials.json`` login keeps working until it expires,
at which point the sign-in modal offers the setup-token flow.

Idempotent: re-running after a successful migration is a no-op (no managed
keys remain in the host env, so there is nothing to move and no restart).

The restart phase runs DETACHED (``--restart-phase`` re-invocation under
``setsid`` with output to a log file), so an agent running this script on
itself still completes the migration: the restart tears the invoking agent
down, the detached process finishes the stop/start/message sequence, and
the invoking agent comes back with the standard "please continue" message.
Detached-phase logs land in ``/tmp/migrate_claude_auth_restart.log``.

Run from the repo root: ``uv run python scripts/migrate_claude_auth.py``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


from imbue.mngr.utils.env_utils import parse_env_file
from imbue.system_interface.claude_auth import CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR
from imbue.system_interface.claude_auth import MANAGED_AUTH_ENV_KEYS
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.claude_auth import derive_auth_mode
from imbue.system_interface.claude_auth import read_managed_auth_env
from imbue.system_interface.claude_auth import write_managed_auth_env

_RESTART_LOG_PATH = Path("/tmp/migrate_claude_auth_restart.log")


def _format_env_value(value: str) -> str:
    """Quote a value the same way mngr's _format_env_file does."""
    if " " in value or '"' in value or "'" in value or "\n" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _format_env_file(env: dict[str, str]) -> str:
    return "\n".join(f"{key}={_format_env_value(value)}" for key, value in env.items()) + "\n"


def _resolve_host_env_path() -> Path:
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        raise SystemExit("MNGR_HOST_DIR is unset; run this inside the workspace (e.g. from the workspace terminal).")
    return Path(host_dir) / "env"


def _migrate_env_files() -> bool:
    """Move managed auth keys from the host env file into the settings env block.

    Returns True when anything changed (so the caller knows a restart is
    needed). Settings-held credentials win over host-env ones: the modal is
    the source of truth once it has been used.
    """
    host_env_path = _resolve_host_env_path()
    host_env = parse_env_file(host_env_path.read_text()) if host_env_path.exists() else {}
    stale_managed = {key: value for key, value in host_env.items() if key in MANAGED_AUTH_ENV_KEYS and value}
    if not stale_managed:
        print("Host env file holds no Claude auth keys; nothing to migrate.")
        return False

    existing_settings_env = read_managed_auth_env()
    if existing_settings_env:
        print(
            "Settings already hold {} credentials; scrubbing the stale host env keys only.".format(
                derive_auth_mode(existing_settings_env).value
            )
        )
    else:
        write_managed_auth_env(stale_managed)
        print(
            "Moved {} into the shared Claude settings ({} mode).".format(
                ", ".join(sorted(stale_managed)), derive_auth_mode(stale_managed).value
            )
        )

    remaining = {key: value for key, value in host_env.items() if key not in MANAGED_AUTH_ENV_KEYS}
    host_env_path.write_text(_format_env_file(remaining))
    print(f"Scrubbed {', '.join(sorted(stale_managed))} from {host_env_path}.")
    return True


def _spawn_detached_restart() -> None:
    """Re-invoke this script's restart phase detached from the current process.

    ``start_new_session`` puts the child in its own session so it survives
    the invoking agent's teardown (the restart stops that agent's whole
    tmux session). Output goes to a log file for post-hoc inspection.
    """
    with _RESTART_LOG_PATH.open("ab") as log_file:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--restart-phase"],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print(f"Restarting claude agents in the background (log: {_RESTART_LOG_PATH}).")
    print("If you are chatting with an agent right now, it will restart and then continue on its own.")


def _run_restart_phase() -> None:
    managed_env = read_managed_auth_env()
    has_token = bool(managed_env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR))
    print(f"Restarting claude agents (settings mode: {derive_auth_mode(managed_env).value}, token={has_token}).")
    service = ClaudeAuthService()
    restarted = service.restart_all_claude_agents()
    print(f"Restarted agents: {', '.join(restarted) if restarted else '(none running)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--restart-phase",
        action="store_true",
        help="Internal: run the detached agent-restart phase (spawned automatically).",
    )
    arguments = parser.parse_args()
    if arguments.restart_phase:
        _run_restart_phase()
        return
    if _migrate_env_files():
        _spawn_detached_restart()


if __name__ == "__main__":
    main()
