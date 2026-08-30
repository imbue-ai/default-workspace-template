#!/usr/bin/env python3
"""Print the `mngr create` arguments that bind a new agent to the default account.

Every chat gets an account because the UI picks one. Nothing else does: automations, the
weekly Caretaker and anything else launched from cron or supervisord shell `mngr create`
with no account and no agent to inherit one from, so they fall back to a `~/.claude` that
holds no credential on this workspace and cannot take a turn.

Workers are the exception and need nothing from this script. `mngr` sources an agent's env
file into every process in its tmux session, so a worker created from inside a bound chat's
shell already carries that chat's `CLAUDE_CONFIG_DIR`, and mngr propagates it -- a background
worker runs on its parent's account, which is what it should do.

Prints one argument per line, nothing at all when no usable account exists (in which case the
caller launches unbound exactly as before). Usage from the repo root:

    mapfile -t account_args < <(uv run python system/scripts/default_account_args.py claude)
"""

from __future__ import annotations

import sys

from imbue.system_interface.accounts import AccountError
from imbue.system_interface.accounts import account_dir
from imbue.system_interface.harnesses.binding import BindingError
from imbue.system_interface.harnesses.binding import account_env
from imbue.system_interface.harnesses.binding import harness_for
from imbue.system_interface.harnesses.binding import resolve_binding
from imbue.system_interface.harnesses.harness_type import HarnessType


def main(argv: list[str]) -> int:
    wanted = HarnessType(argv[1]) if len(argv) > 1 else HarnessType.CLAUDE
    try:
        account = resolve_binding()
    except (AccountError, BindingError) as e:
        print(f"could not resolve the default account: {e}", file=sys.stderr)
        return 0
    if account is None:
        return 0
    if harness_for(account) is not wanted:
        # The most recently used account is on another harness. Binding a claude automation
        # to an agy credential would fail on its first turn with a confusing error, so this
        # launches unbound instead, which fails in a way the user already understands.
        print(
            f"the default account runs {harness_for(account)}, not {wanted.value}; "
            "launching without an account",
            file=sys.stderr,
        )
        return 0
    # `--env`, not the symlink the other harnesses use: a symlink needs the agent's state
    # directory, which does not exist until `mngr create` provisions it. claude is bound by
    # an env var, and claude is what every non-chat creator here runs.
    for name, value in account_env(wanted, account_dir(account.id)).items():
        print("--env")
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
