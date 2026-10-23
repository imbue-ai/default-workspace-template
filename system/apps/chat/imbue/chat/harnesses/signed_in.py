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

import httpx
from loguru import logger as _loguru_logger
from pydantic import SecretStr

from imbue.chat.harnesses.claude.auth import MANAGED_AUTH_ENV_KEYS
from imbue.chat.harnesses.grok.account_binding import read_sole_api_key
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import build_account_binding
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

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
    # The string matters: measured against 0.83.0, 0.84.1 and 0.87.1, an empty dir, an unknown
    # provider id and a malformed auth.json all print "No models available. Use /login ..."
    # and exit 0. Matching anything else -- "No usable API key is configured", which pi
    # prints when asked to take a TURN, not when asked to list -- makes NO unreachable and
    # the probe a network round trip that always says yes.
    HarnessType.PI_CODING: (("pi", "--list-models"), "No models available"),
}

# `grok models` exits 0 whatever it finds; its first line names the session login in auth.json and ignores a
# stored API key.
_GROK_MODELS_COMMAND: Final = ("grok", "models")
_GROK_SIGNED_IN_BANNER: Final = "You are logged in with "
_GROK_SIGNED_OUT_BANNER: Final = "You are not authenticated."
# Where grok checks an API key before using it, and the flags of a 200 that mean the key is unusable.
_XAI_API_KEY_INFO_URL: Final = "https://api.x.ai/v1/api-key"
_XAI_API_KEY_UNUSABLE_FLAGS: Final = ("api_key_blocked", "api_key_disabled", "team_blocked")
# Grok reads its own variables as credentials, endpoints and config overlays.
_GROK_ENV_VAR_PREFIX: Final = "GROK_"
_XAI_API_KEY_ENV_VAR: Final = "XAI_API_KEY"


def _send(request: httpx.Request) -> httpx.Response:
    with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
        return client.send(request)


def is_signed_in(
    harness: HarnessType,
    account_dir: Path,
    runner: Callable[..., Any] = run_local_command_modern_version,
    fetcher: Callable[[httpx.Request], httpx.Response] = _send,
) -> SignedIn:
    """Ask the harness's own CLI whether this account folder is authenticated; a grok API key is asked of xAI.

    `runner` and `fetcher` are injectable so the decision table can be exercised without five
    CLIs on PATH and without a network round trip -- every arm below is a judgement about a
    command's output or a response, not about the command.
    """
    if harness is HarnessType.GROK:
        return _is_grok_signed_in(account_dir, runner, fetcher)
    probe = _PROBES.get(harness)
    if probe is None:
        # Nothing to ask. A file write either happened or raised.
        return SignedIn.YES
    command, unauthenticated_text = probe
    finished = _run_probe(harness, command, account_dir, runner)
    if finished is None:
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


def _is_grok_signed_in(
    account_dir: Path, runner: Callable[..., Any], fetcher: Callable[[httpx.Request], httpx.Response]
) -> SignedIn:
    """A stored API key alone is asked of xAI, since `grok models` never reads it; anything else is grok's banner."""
    api_key = read_sole_api_key(account_dir)
    if api_key is not None:
        return probe_xai_api_key(api_key, fetcher)
    finished = _run_probe(HarnessType.GROK, _GROK_MODELS_COMMAND, account_dir, runner)
    if finished is None:
        return SignedIn.UNKNOWN
    output = (finished.stdout or "") + (finished.stderr or "")
    # The banner comes from the file alone, so a model listing that fails after it does not change the answer.
    if _GROK_SIGNED_IN_BANNER in output:
        return SignedIn.YES
    if _GROK_SIGNED_OUT_BANNER in output:
        return SignedIn.NO
    logger.warning("grok signed-in probe printed neither banner (exit {})", finished.returncode)
    return SignedIn.UNKNOWN


def probe_xai_api_key(api_key: SecretStr, fetcher: Callable[[httpx.Request], httpx.Response] = _send) -> SignedIn:
    """xAI's verdict on an API key, read as grok reads it: only an auth error or a blocked key is a no."""
    key = api_key.get_secret_value()
    # No request can carry a key that is not printable ASCII, and grok refuses a blank one.
    if not key.strip() or not (key.isascii() and key.isprintable()):
        return SignedIn.NO
    request = httpx.Request("GET", _XAI_API_KEY_INFO_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        response = fetcher(request)
    except httpx.RequestError as e:
        logger.warning("grok API key probe could not reach xAI: {}", e)
        return SignedIn.UNKNOWN
    if 400 <= response.status_code <= 403:
        return SignedIn.NO
    if response.status_code != 200:
        logger.warning("grok API key probe got HTTP {} from xAI", response.status_code)
        return SignedIn.UNKNOWN
    try:
        key_info = response.json()
    except ValueError as e:
        logger.warning("grok API key probe took an unparseable 200 from xAI as usable, as grok does: {}", e)
        return SignedIn.YES
    if isinstance(key_info, dict) and any(key_info.get(flag) is True for flag in _XAI_API_KEY_UNUSABLE_FLAGS):
        return SignedIn.NO
    return SignedIn.YES


def _run_probe(
    harness: HarnessType, command: tuple[str, ...], account_dir: Path, runner: Callable[..., Any]
) -> Any | None:
    """The probe command's finished process, or None when it could not run or timed out."""
    # The scoping variable is layered OVER the ambient environment, never used alone:
    # `Popen` replaces rather than merges, so a bare {"CODEX_HOME": ...} would drop PATH and
    # the probe would fail closed on every account.
    #
    # But the ambient environment is the SERVER's, and on a workspace upgraded from the
    # shared-login era it can still carry ANTHROPIC_API_KEY. claude reports `loggedIn: true,
    # apiKeySource: ANTHROPIC_API_KEY` on the strength of that alone -- so an empty account
    # folder would commit as signed in, become the most-recently-used, and launch every later
    # chat with no credential. The question is whether THIS FOLDER is authenticated.
    env = {name: value for name, value in os.environ.items() if not _is_ambient_auth(harness, name)}
    env.update(build_account_binding(harness).account_env(account_dir))
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
        return None
    if finished.is_timed_out:
        logger.warning("{} signed-in probe timed out", harness.value)
        return None
    return finished


@pure
def _is_ambient_auth(harness: HarnessType, name: str) -> bool:
    """Whether the server's own variable ``name`` could answer the probe for the folder."""
    if name in MANAGED_AUTH_ENV_KEYS:
        return True
    return harness is HarnessType.GROK and (name == _XAI_API_KEY_ENV_VAR or name.startswith(_GROK_ENV_VAR_PREFIX))
