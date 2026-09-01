"""Deciding whether an account folder actually holds a working sign-in.

Used at exactly one moment: just after a sign-in flow finishes, to decide whether to commit
an account row. It is NOT a liveness check -- nothing polls this, and an account that stops
working later is discovered when a turn fails, not by asking here.

The three-way answer matters. Collapsing "could not run the probe" into "signed out" would
delete a folder the user just completed a browser OAuth into, because the probes shell out to
CLIs that fetch over the network and a 20-second hiccup is not evidence of anything.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.system_interface.harnesses.binding import account_env
from imbue.system_interface.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.system_interface.harnesses.harness_type import HarnessType

logger = _loguru_logger

_PROBE_TIMEOUT_SECONDS: Final = 30.0


class SignedIn(StrEnum):
    YES = "yes"
    NO = "no"
    # The probe could not answer. Keep the folder and let the user retry.
    UNKNOWN = "unknown"


# The command that answers, per harness, and the text that means "signed out" when the exit
# code alone cannot say.
_PROBES: Final[dict[HarnessType, tuple[tuple[str, ...], str | None]]] = {
    HarnessType.CLAUDE: (("claude", "auth", "status", "--json"), None),
    HarnessType.CODEX: (("codex", "login", "status"), None),
    # `agy models` fetches the catalogue over the network, so a non-zero exit means "signed
    # out OR the network blinked". Its text distinguishes the two, and the extra "Error"
    # guard keeps a transient failure from reading as a successful sign-in.
    HarnessType.ANTIGRAVITY: (("agy", "models"), "Please sign in"),
    # pi's lanes are file writes, so this is not asking "did a browser flow finish" -- it is
    # asking whether pi actually ACCEPTED the file we just wrote.
    #
    # The string matters: measured against 0.83.0 and 0.84.1, an empty dir, an unknown
    # provider id and a malformed auth.json all print "No models available. Use /login ..."
    # and exit 0. Matching anything else -- "No usable API key is configured", which pi
    # prints when asked to take a TURN, not when asked to list -- makes NO unreachable and
    # the probe a network round trip that always says yes.
    HarnessType.PI_CODING: (("pi", "--list-models"), "No models available"),
}


def is_signed_in(
    harness: HarnessType, account_dir: Path, runner: Callable[..., Any] = run_local_command_modern_version
) -> SignedIn:
    """Ask the harness's own CLI whether this account folder is authenticated.

    `runner` is injectable so the decision table can be exercised without four CLIs on PATH
    and without a network round trip -- every arm below is a judgement about a command's
    output, not about the command.
    """
    probe = _PROBES.get(harness)
    if probe is None:
        # Nothing to ask. A file write either happened or raised.
        return SignedIn.YES
    command, unauthenticated_text = probe

    # The scoping variable is layered OVER the ambient environment, never used alone:
    # `Popen` replaces rather than merges, so a bare {"CODEX_HOME": ...} would drop PATH and
    # the probe would fail closed on every account.
    #
    # But the ambient environment is the SERVER's, and on a workspace upgraded from the
    # shared-login era it can still carry ANTHROPIC_API_KEY. claude reports `loggedIn: true,
    # apiKeySource: ANTHROPIC_API_KEY` on the strength of that alone -- so an empty account
    # folder would commit as signed in, become the most-recently-used, and launch every later
    # chat with no credential. The question is whether THIS FOLDER is authenticated.
    env = {k: v for k, v in os.environ.items() if k not in MANAGED_AUTH_ENV_KEYS}
    env.update(account_env(harness, account_dir))
    try:
        finished = runner(
            command=list(command),
            is_checked=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
            cwd=None,
            env=env,
            name=f"{harness.value} signed-in probe",
        )
    except ProcessError as e:
        logger.warning("{} signed-in probe could not run: {}", harness.value, e)
        return SignedIn.UNKNOWN
    if finished.is_timed_out:
        logger.warning("{} signed-in probe timed out", harness.value)
        return SignedIn.UNKNOWN

    output = (finished.stdout or "") + (finished.stderr or "")
    if unauthenticated_text is not None:
        if unauthenticated_text in output:
            return SignedIn.NO
        # A failure that is not the signed-out message is the network, not the credential.
        if "Error" in output or finished.returncode != 0:
            return SignedIn.UNKNOWN
        return SignedIn.YES
    return SignedIn.YES if finished.returncode == 0 else SignedIn.NO
