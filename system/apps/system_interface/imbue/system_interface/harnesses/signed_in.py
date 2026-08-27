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
from enum import StrEnum
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.system_interface.harnesses.binding import account_env
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
    # asking whether pi actually ACCEPTED the file we just wrote. `--list-models` scoped to
    # the account answers that: it names the provider's models when the credential was read
    # and says so plainly when it was not. It does not reach the provider, so it cannot tell
    # a valid key from a well-formed one -- but a typo'd provider or a schema we got wrong
    # would otherwise be discovered only by a chat that silently could not take a turn.
    HarnessType.PI_CODING: (("pi", "--list-models"), "No usable API key is configured"),
}


def is_signed_in(harness: HarnessType, account_dir: Path) -> SignedIn:
    """Ask the harness's own CLI whether this account folder is authenticated."""
    probe = _PROBES.get(harness)
    if probe is None:
        # Nothing to ask. A file write either happened or raised.
        return SignedIn.YES
    command, unauthenticated_text = probe

    # The scoping variable is layered OVER the ambient environment, never used alone:
    # `Popen` replaces rather than merges, so a bare {"CODEX_HOME": ...} would drop PATH and
    # the probe would fail closed on every account.
    env = {**os.environ, **account_env(harness, account_dir)}
    try:
        finished = run_local_command_modern_version(
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
