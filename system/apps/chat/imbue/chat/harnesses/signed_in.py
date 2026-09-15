"""Deciding whether an account folder actually holds a working sign-in.

Used at exactly one moment: just after a sign-in flow finishes, to decide whether to commit
an account row. It is NOT a liveness check -- nothing polls this, and an account that stops
working later is discovered when a turn fails, not by asking here.

The three-way answer matters. Collapsing "could not run the probe" into "signed out" would
delete a folder the user just completed a browser OAuth into, because the probes shell out to
CLIs that fetch over the network and a 20-second hiccup is not evidence of anything.

One account kind is not asked through a CLI at all. agy in API-key mode lists its Gemini
models for ANY key, valid or not, so `agy models` would answer YES to a typo -- and agy
answers an invalid key at turn time by falling back to a browser OAuth flow, which in a
workspace nobody is watching is a chat parked on a sign-in prompt forever. That key is
therefore validated against Google directly.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger as _loguru_logger

from imbue.chat.harnesses.account_scope import account_env
from imbue.chat.harnesses.antigravity.auth import GEMINI_API_KEY_ENV_VAR
from imbue.chat.harnesses.antigravity.auth import gemini_env_path
from imbue.chat.harnesses.antigravity.auth import has_gemini_api_key
from imbue.chat.harnesses.antigravity.auth import read_gemini_api_key
from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version

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


# Google's list-models endpoint, which authenticates on the key header alone -- no project, no
# billing context, no body to send. A key it accepts answers 200 with a `models` list; one it
# does not answers 400 with API_KEY_INVALID.
_GEMINI_MODELS_URL: Final = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_API_KEY_HEADER: Final = "x-goog-api-key"
# The statuses that are a verdict on the KEY. Anything else -- a rate limit, a 5xx, a captive
# portal's error page -- is the service having a bad moment and says nothing about the key.
_KEY_REJECTED_STATUSES: Final = frozenset((400, 401, 403))


def _gemini_key_verdict(api_key: str, http_get: Callable[..., httpx.Response]) -> SignedIn:
    """Ask Google whether it accepts this key, keeping the module's three-way answer."""
    try:
        response = http_get(
            _GEMINI_MODELS_URL,
            headers={_GEMINI_API_KEY_HEADER: api_key},
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except UnicodeEncodeError:
        # httpx encodes a header value as ASCII, and this is a ValueError rather than an
        # httpx error, so the clause below never sees it. A key the request cannot even
        # carry is not one Google could accept, which makes it a verdict and not a blank.
        logger.warning("Gemini key check refused a key carrying a character an HTTP header cannot hold")
        return SignedIn.NO
    except httpx.HTTPError as e:
        logger.warning("Gemini key check could not reach Google: {}", e)
        return SignedIn.UNKNOWN
    if response.status_code == httpx.codes.OK:
        return SignedIn.YES
    if response.status_code in _KEY_REJECTED_STATUSES:
        return SignedIn.NO
    logger.warning("Gemini key check answered {}, which says nothing about the key", response.status_code)
    return SignedIn.UNKNOWN


def is_signed_in(
    harness: HarnessType,
    account_dir: Path,
    runner: Callable[..., Any] = run_local_command_modern_version,
    http_get: Callable[..., httpx.Response] = httpx.get,
) -> SignedIn:
    """Whether this account folder holds a credential its harness can actually use.

    `runner` and `http_get` are injectable so the decision table can be exercised without four
    CLIs on PATH and without a network round trip -- every arm below is a judgement about a
    command's output or a response's status, not about the command or the fetch.
    """
    # The key file is only there when this account signed in by pasting one, so it is also what
    # picks between the two routes on this lane; an agy account on a browser login has none. The
    # test is the file's presence, the same one binding uses to put an agent into key mode: a file
    # that is there but names no key still binds that way, and agy exits before its first turn on
    # it, so answering through the CLI probe -- which says yes to any key -- would call such an
    # account healthy.
    if harness is HarnessType.ANTIGRAVITY and has_gemini_api_key(account_dir):
        api_key = read_gemini_api_key(account_dir)
        if api_key is None:
            logger.warning(
                "{} names no {}, so agy has nothing to run on",
                gemini_env_path(account_dir),
                GEMINI_API_KEY_ENV_VAR,
            )
            return SignedIn.NO
        return _gemini_key_verdict(api_key, http_get)
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
