"""Browser state in, credentials out: the two re-encryptions at the sandbox's boundary (spec 6.2 and 6.4).

Both are one ``latchkey auth re-encrypt`` each. The source key is ``LATCHKEY_ENCRYPTION_KEY`` in the child's
environment and the destination key goes on its stdin, so no key is ever in an ``argv``:

- import: source ``incoming/`` under the transfer key, destination the sandbox's directory under the sandbox key;
- export: source the sandbox's directory under the sandbox key, destination ``export/`` under the transfer key.

The pinned CLI merges into a destination that already holds a store under a different key, and exports a
browser state with or without services (verified against 3.19.3; spec section 15).
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.encryption_key import LATCHKEY_ENCRYPTION_KEY_ENV_VAR
from pydantic import Field, SecretStr

from auth_sandbox.errors import LatchkeyCommandError, MissingMaterialError
from auth_sandbox.interfaces import ReencryptRunnerInterface
from auth_sandbox.primitives import (
    LATCHKEY_DIRECTORY_ENV_VAR,
    LATCHKEY_DISABLE_COUNTING_ENV_VAR,
    LATCHKEY_GATEWAY_ENV_VAR,
)
from auth_sandbox.runtime_files import SandboxPaths, read_key


class ExportedBundle(FrozenModel):
    """The files ``auth-sandbox-export`` wrote, which the chrome reads back over owner-exec."""

    credentials: Path = Field(
        description="The service's credentials, encrypted under the transfer key"
    )
    data_format_version: Path = Field(description="The store's format stamp")
    browser_state: Path | None = Field(
        description="The browser state under the transfer key, when the sandbox has one"
    )


@pure
def reencrypt_argv(
    latchkey_binary: str,
    destination: Path,
    services: tuple[str, ...],
    is_browser_state: bool,
    account: str | None,
) -> tuple[str, ...]:
    """The ``latchkey auth re-encrypt`` command line; ``--account`` is latchkey's global option and precedes the
    subcommand. Only service and account names travel here, never a key."""
    argv: list[str] = [latchkey_binary]
    if account is not None:
        argv.extend(["--account", account])
    argv.extend(["auth", "re-encrypt", str(destination)])
    if services:
        argv.extend(["--services", *services])
    if is_browser_state:
        argv.append("--browser-state")
    return tuple(argv)


@pure
def reencrypt_environment(
    base: Mapping[str, str], source_directory: Path, source_key: SecretStr
) -> dict[str, str]:
    """The child's environment: the source store and its key, counting off, and no gateway (a local invocation
    that sees ``LATCHKEY_GATEWAY`` would try to use it)."""
    environment = {
        name: value for name, value in base.items() if name != LATCHKEY_GATEWAY_ENV_VAR
    }
    environment[LATCHKEY_DIRECTORY_ENV_VAR] = str(source_directory)
    environment[LATCHKEY_ENCRYPTION_KEY_ENV_VAR] = source_key.get_secret_value()
    environment[LATCHKEY_DISABLE_COUNTING_ENV_VAR] = "1"
    return environment


def _run_reencrypt(
    runner: ReencryptRunnerInterface,
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    destination_key: SecretStr,
    working_directory: Path,
    what: str,
) -> None:
    finished = runner.run(
        argv, environment, destination_key.get_secret_value() + "\n", working_directory
    )
    if finished.returncode != 0:
        raw = (
            finished.stderr.strip()
            or finished.stdout.strip()
            or f"latchkey auth re-encrypt exited {finished.returncode}"
        )
        raise LatchkeyCommandError(
            f"{what} failed: {summarize_latchkey_failure(raw, fallback=raw)}"
        )


def import_browser_state(
    runner: ReencryptRunnerInterface,
    paths: SandboxPaths,
    latchkey_binary: str,
    base_environment: Mapping[str, str],
) -> Path:
    """Re-encrypt the incoming browser state into the sandbox's directory under the sandbox's key.

    Returns the path of the state in the sandbox's directory. Raises ``MissingMaterialError`` when the transfer
    key, the sandbox key or the incoming state is absent, and ``LatchkeyCommandError`` when latchkey refuses.
    """
    transfer_key = read_key(paths.transfer_key_path)
    sandbox_key = read_key(paths.latchkey_key_path)
    if not paths.incoming_browser_state_path.is_file():
        raise MissingMaterialError(
            f"no incoming browser state at {paths.incoming_browser_state_path}"
        )
    paths.latchkey_directory.mkdir(parents=True, exist_ok=True)
    _run_reencrypt(
        runner,
        reencrypt_argv(latchkey_binary, paths.latchkey_directory, (), True, None),
        reencrypt_environment(base_environment, paths.incoming_dir, transfer_key),
        sandbox_key,
        paths.runtime_dir,
        "importing the browser state",
    )
    if not paths.latchkey_browser_state_path.is_file():
        raise LatchkeyCommandError(
            f"latchkey reported success but wrote no browser state at {paths.latchkey_browser_state_path}"
        )
    return paths.latchkey_browser_state_path


def export_credentials(
    runner: ReencryptRunnerInterface,
    paths: SandboxPaths,
    latchkey_binary: str,
    base_environment: Mapping[str, str],
    service_name: str,
    account: str | None,
) -> ExportedBundle:
    """Re-encrypt one service's credentials (and the browser state, when the sandbox holds one) into the export
    directory under the transfer key, replacing whatever an earlier export left there.

    The browser state is asked for only when the sandbox's directory holds one: a sign-in to a service that
    needs no browser, with no state imported, leaves none, and the export then carries the credentials alone.
    """
    transfer_key = read_key(paths.transfer_key_path)
    sandbox_key = read_key(paths.latchkey_key_path)
    is_browser_state = paths.latchkey_browser_state_path.is_file()
    shutil.rmtree(paths.export_dir, ignore_errors=True)
    paths.export_dir.mkdir(parents=True)
    _run_reencrypt(
        runner,
        reencrypt_argv(
            latchkey_binary,
            paths.export_dir,
            (service_name,),
            is_browser_state,
            account,
        ),
        reencrypt_environment(base_environment, paths.latchkey_directory, sandbox_key),
        transfer_key,
        paths.runtime_dir,
        f"exporting the {service_name} credentials",
    )
    for required in (
        paths.exported_credentials_path,
        paths.exported_data_format_version_path,
    ):
        if not required.is_file():
            raise LatchkeyCommandError(
                f"latchkey reported success but wrote nothing at {required}"
            )
    browser_state = (
        paths.exported_browser_state_path
        if paths.exported_browser_state_path.is_file()
        else None
    )
    if is_browser_state and browser_state is None:
        raise LatchkeyCommandError(
            f"latchkey reported success but wrote no browser state at {paths.exported_browser_state_path}"
        )
    return ExportedBundle(
        credentials=paths.exported_credentials_path,
        data_format_version=paths.exported_data_format_version_path,
        browser_state=browser_state,
    )
