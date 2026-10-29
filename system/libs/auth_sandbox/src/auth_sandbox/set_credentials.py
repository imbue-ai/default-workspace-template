"""Typed credentials for a service without a browser sign-in (spec 6.3, the ``authOptions`` of ``set`` case).

The chrome renders the form the desktop renders, from the same parsed ``setCredentialsExample``, and writes the
values the user typed to a tmpfs file; the command reads that file, builds the ``latchkey auth set`` argv the
desktop would build, and runs it. Values travel in a file and in the child's argv only, never in this command's
own argv or its logs.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.credential_commands import (
    CredentialCommandError,
    build_credential_command_argv,
    parse_credential_command_example,
    set_credentials_example_for,
)
from pydantic import Field

from auth_sandbox.errors import ValuesFileError
from auth_sandbox.interfaces import SignInLatchkeyInterface
from auth_sandbox.sign_in import SignInOutcome

VALUES_KEY = "values"
EXAMPLE_KEY = "set_credentials_example"


class CredentialValues(FrozenModel):
    """What the values file carries: the user's values by placeholder name, and optionally the example they fill."""

    value_by_parameter_name: Mapping[str, str] = Field(
        description="The typed values, keyed by placeholder name"
    )
    set_credentials_example: str | None = Field(
        description="The service's example invocation when the chrome already holds it; else asked of latchkey"
    )


@pure
def parse_values_file(text: str) -> CredentialValues:
    """Parse the values file: a JSON object ``{"values": {name: value, ...}, "set_credentials_example": "..."?}``.

    Raises:
        ValuesFileError: when the text is not that shape; the message never quotes a value.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValuesFileError(
            f"the values file is not valid JSON: {e.msg} at line {e.lineno}"
        ) from e
    if not isinstance(document, dict):
        raise ValuesFileError("the values file must hold a JSON object")
    raw_values = document.get(VALUES_KEY)
    if not isinstance(raw_values, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in raw_values.items()
    ):
        raise ValuesFileError(
            f"the values file's {VALUES_KEY!r} must be an object of string values"
        )
    example = document.get(EXAMPLE_KEY)
    if example is not None and not isinstance(example, str):
        raise ValuesFileError(
            f"the values file's {EXAMPLE_KEY!r} must be a string when present"
        )
    return CredentialValues(
        value_by_parameter_name=dict(raw_values), set_credentials_example=example
    )


def run_set_credentials(
    latchkey: SignInLatchkeyInterface,
    service_name: str,
    account: str,
    values: CredentialValues,
) -> SignInOutcome:
    """Build the service's credential command from its example and the typed values, and store the result.

    The example comes from the values file when the chrome passed one, else from latchkey's own description of
    the service (offline), else the generic bearer form every service accepts. A command that cannot be built
    (an unknown placeholder, a blank value) is a failed outcome with the parser's reason, not an exception: the
    dialog shows it beside the form, as the desktop does.
    """
    latchkey.initialize()
    reported_example = values.set_credentials_example
    if reported_example is None:
        info = latchkey.services_info(service_name)
        reported_example = info.set_credentials_example if info is not None else None
    example = set_credentials_example_for(service_name, reported_example, None)
    try:
        parsed = parse_credential_command_example(example)
        argv = build_credential_command_argv(
            parsed, values.value_by_parameter_name, account
        )
    except CredentialCommandError as e:
        return SignInOutcome(is_stored=False, detail=str(e))
    is_stored, detail = latchkey.auth_set_credentials(service_name, argv)
    return SignInOutcome(is_stored=is_stored, detail=detail)


def read_and_remove_values_file(path: Path) -> CredentialValues:
    """Read the values file and delete it: the values are needed once, and the tmpfs should not keep them."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ValuesFileError(f"no values file at {path}") from e
    path.unlink(missing_ok=True)
    return parse_values_file(text)
