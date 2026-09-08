#!/usr/bin/env python3
"""Migrate this workspace's Claude auth from the mngr host env file into an account.

Older workspaces received ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` via the mngr
host env file (``$MNGR_HOST_DIR/env``), which every process freezes at start. Auth now
lives in a provider account under ``~/.minds/accounts``, and a chat binds to one when it
is created. This script performs the one-time move:

1. Mint an Anthropic account from any managed auth keys in the host env file
   (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``, ``CLAUDE_CODE_OAUTH_TOKEN``).
2. Scrub those keys from the host env file, so a stale value can never shadow the
   account's credential in some process's frozen environment.

Subscription-based workspaces (no key in the host env) need no migration: their existing
``.credentials.json`` login keeps working, and the provider chooser signs in when it
expires.

Nothing is restarted. That used to be step 3 -- the shared settings env is read at claude
process start, so every running agent had to be torn down to see a new credential. An
account is picked up at create time instead, so existing chats keep running on whatever
they were created with and only new ones use the migrated account.

Idempotent: re-running after a successful migration is a no-op, since no managed keys
remain in the host env.

Run from the repo root: ``uv run python system/scripts/migrate_claude_auth.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from imbue.mngr.utils.env_utils import parse_env_file
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.system_interface.harnesses.claude.auth import derive_auth_mode


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
        raise SystemExit(
            "MNGR_HOST_DIR is unset; run this inside the workspace (e.g. from the workspace terminal)."
        )
    return Path(host_dir) / "env"


def migrate() -> bool:
    """Move managed auth keys out of the host env file and into an account.

    Returns True when anything changed.
    """
    host_env_path = _resolve_host_env_path()
    host_env = parse_env_file(host_env_path.read_text()) if host_env_path.exists() else {}
    stale_managed = {key: value for key, value in host_env.items() if key in MANAGED_AUTH_ENV_KEYS and value}
    if not stale_managed:
        print("Host env file holds no Claude auth keys; nothing to migrate.")
        return False

    pasted = "\n".join(f"{key}={value}" for key, value in sorted(stale_managed.items()))
    account = AuthFlowService.create().adopt_claude_credentials(pasted)
    print(
        "Moved {} into account {} ({} mode).".format(
            ", ".join(sorted(stale_managed)), account.id, derive_auth_mode(stale_managed).value
        )
    )

    remaining = {key: value for key, value in host_env.items() if key not in MANAGED_AUTH_ENV_KEYS}
    host_env_path.write_text(_format_env_file(remaining))
    print(f"Scrubbed {', '.join(sorted(stale_managed))} from {host_env_path}.")
    print("Existing chats keep their current credential; new chats will use this account.")
    return True


if __name__ == "__main__":
    migrate()
