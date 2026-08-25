"""Preflight that refuses to launch an alt-harness agent whose CLI is not signed in.

Each non-claude harness (codex, pi) authenticates through its own
CLI, independently of the shared claude login. Launching an agent on a signed-out harness
produces a chat that can never take a turn -- the CLI just reprints its login prompt -- so
before the system interface creates one, :func:`find_unauthenticated_harness_reason` runs
that CLI's status command here and returns a readable refusal when it comes back signed out.

Claude is the workspace default; its auth lives in the shared ``~/.claude`` and is not gated
here, so the function returns ``None`` for claude and for any harness without a registered
check ("cleared to launch").

The registered commands and their signed-out signatures:

- codex: ``codex login status`` exits non-zero when signed out.
- pi:    ``pi --list-models`` prints ``No models available`` (still exits 0), so the
         signal is in the output.

Fail-closed: if the command cannot be run at all (binary missing) or does not finish within
the timeout, the harness is treated as signed out -- an agent we cannot confirm is usable
must not be launched.
"""

import re

from loguru import logger

from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.subprocess_runner import run_detached_command

# Bounds a wedged CLI so a create cannot hang. These commands are not instant: they are the
# same shape as `claude auth status`, measured at 3-8s on its first run after an idle gap (see
# _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS in harnesses/claude/auth.py). 20s rather than that
# constant's 25s because codex and pi ship much smaller binaries than claude's 256MB and their
# cold start has not been measured here -- matching it would be a guess.
_AUTH_CHECK_TIMEOUT_SECONDS = 20.0


class HarnessAuthCheck(FrozenModel):
    """How to tell whether one harness's CLI is signed in."""

    # The status command to run.
    command: tuple[str, ...]
    # Human-readable harness name, used in the refusal message shown to the user.
    display_name: str
    # Step-by-step, user-facing instructions for signing this harness's CLI in, appended to the
    # refusal so the user knows exactly what to do (the harnesses sign in through different flows).
    signin_instructions: str
    # When set, the harness is signed OUT iff this regex is found in the command's combined
    # stdout+stderr (the CLI exits 0 either way). When None, signed OUT iff the command exits
    # non-zero.
    unauthenticated_output_pattern: str | None = None


# The two registered checks, wired onto their ``HarnessSpec.auth_check`` in the registry --
# the ONE per-harness table -- rather than a parallel dict here.
CODEX_AUTH_CHECK = HarnessAuthCheck(
    command=("codex", "login", "status"),
    display_name="Codex",
    signin_instructions="Go to New tab (+) → New terminal → run `codex`",
)
PI_AUTH_CHECK = HarnessAuthCheck(
    command=("pi", "--list-models"),
    display_name="Pi",
    signin_instructions="Go to New tab (+) → New terminal → run `pi` → type `/login`",
    unauthenticated_output_pattern=r"No models available",
)


def _is_signed_in(check: HarnessAuthCheck) -> bool:
    """Run ``check``'s status command and decide whether the harness is signed in.

    Fail-closed: a command that cannot start (binary missing) or times out counts as signed
    out, since we cannot confirm the harness is usable.
    """
    try:
        result = run_detached_command(
            list(check.command),
            timeout=_AUTH_CHECK_TIMEOUT_SECONDS,
            name=check.command[0],
        )
    except ProcessError as error:
        logger.warning("Auth check {} could not run; treating as signed out: {}", check.command, error)
        return False

    if result.is_timed_out:
        logger.warning("Auth check {} timed out; treating as signed out", check.command)
        return False

    if check.unauthenticated_output_pattern is not None:
        combined_output = f"{result.stdout}\n{result.stderr}"
        return re.search(check.unauthenticated_output_pattern, combined_output) is None

    return result.returncode == 0


def find_unauthenticated_harness_reason(check: HarnessAuthCheck | None) -> str | None:
    """Return a user-facing reason if the harness behind ``check`` is not signed in, else ``None``.

    ``None`` means cleared to launch: no auth gate is registered (claude's auth lives in the
    shared ``~/.claude``) or the CLI reports a signed-in account. Callers pass their harness's
    ``HarnessSpec.auth_check``. A returned string is safe to show the user and names the
    harness that needs signing in.
    """
    if check is None:
        return None
    if _is_signed_in(check):
        return None
    return f"{check.display_name} is not signed in on this workspace. {check.signin_instructions}"
