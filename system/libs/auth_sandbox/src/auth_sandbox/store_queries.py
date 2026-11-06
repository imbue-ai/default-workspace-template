"""The two read-only questions the commands answer the chrome with, in latchkey's own JSON: what latchkey knows of a
service (``auth-sandbox-sign-in``, and ``auth-sandbox-prepare --describe``), and which accounts the sandbox's store
holds for it (printed by the sign-in commands on success, so the chrome knows which account it is transferring).

Both run against the sandbox's store under the session's key, offline, so neither waits on the network.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.encryption_key import LATCHKEY_ENCRYPTION_KEY_ENV_VAR
from pydantic import JsonValue, SecretStr

from auth_sandbox.errors import LatchkeyCommandError
from auth_sandbox.interfaces import FinishedCommand, LatchkeyRunnerInterface
from auth_sandbox.primitives import (
    LATCHKEY_DIRECTORY_ENV_VAR,
    LATCHKEY_DISABLE_COUNTING_ENV_VAR,
    LATCHKEY_GATEWAY_ENV_VAR,
)

# How ``latchkey services info`` (3.21) refuses a service it has neither built in nor registered.
_UNKNOWN_SERVICE_MARKER: Final[str] = "Unknown service:"


@pure
def store_environment(
    base: Mapping[str, str], latchkey_directory: Path, latchkey_key: SecretStr
) -> dict[str, str]:
    """A ``latchkey`` child's environment for the sandbox's store: its directory and key, counting off, and no
    gateway (a local invocation that sees ``LATCHKEY_GATEWAY`` would try to use it)."""
    environment = {
        name: value for name, value in base.items() if name != LATCHKEY_GATEWAY_ENV_VAR
    }
    environment[LATCHKEY_DIRECTORY_ENV_VAR] = str(latchkey_directory)
    environment[LATCHKEY_ENCRYPTION_KEY_ENV_VAR] = latchkey_key.get_secret_value()
    environment[LATCHKEY_DISABLE_COUNTING_ENV_VAR] = "1"
    return environment


@pure
def _json_object(finished: FinishedCommand, what: str) -> dict[str, JsonValue]:
    """The JSON object a ``latchkey`` query printed.

    Raises:
        LatchkeyCommandError: when it failed, naming its reason, or printed something other than an object.
    """
    if finished.returncode != 0:
        raw = (
            finished.stderr.strip()
            or finished.stdout.strip()
            or f"exited {finished.returncode}"
        )
        raise LatchkeyCommandError(
            f"{what} failed: {summarize_latchkey_failure(raw, fallback=raw)}"
        )
    try:
        payload = json.loads(finished.stdout)
    except json.JSONDecodeError as e:
        raise LatchkeyCommandError(f"{what} printed no JSON: {e.msg}") from e
    if not isinstance(payload, dict):
        raise LatchkeyCommandError(f"{what} printed JSON that is not an object")
    return payload


def describe_service(
    runner: LatchkeyRunnerInterface,
    latchkey_binary: str,
    environment: Mapping[str, str],
    service_name: str,
) -> dict[str, JsonValue]:
    """``latchkey services info --offline`` for the service, or ``{}`` when latchkey knows no such service.

    Raises:
        LatchkeyCommandError: when latchkey fails for any other reason, or prints something other than an object.
    """
    finished = runner.run(
        (latchkey_binary, "services", "info", service_name, "--offline"), environment
    )
    if finished.returncode != 0 and _UNKNOWN_SERVICE_MARKER in finished.stderr:
        return {}
    return _json_object(finished, f"describing {service_name}")


def stored_accounts(
    runner: LatchkeyRunnerInterface,
    latchkey_binary: str,
    environment: Mapping[str, str],
    service_name: str,
) -> dict[str, JsonValue]:
    """The sandbox store's accounts for the service, as ``latchkey auth list --offline`` prints them
    (``{account: {"credentialType": ..., "credentialStatus": ...}}``, the default account under ``""``); ``{}`` when
    the store holds none.

    Raises:
        LatchkeyCommandError: when latchkey fails, or its listing is not the shape above.
    """
    listing = _json_object(
        runner.run((latchkey_binary, "auth", "list", "--offline"), environment),
        "listing the stored accounts",
    )
    accounts = listing.get(service_name, {})
    if not isinstance(accounts, dict):
        raise LatchkeyCommandError(
            f"latchkey listed {service_name}'s accounts as something other than an object"
        )
    return accounts
