"""The commands owner-exec runs on the chrome's behalf, each a thin entry point over the modules beside it.

Exit codes: 0 when the command did what it says; 1 when latchkey refused (its reason, condensed, is the last
line on stderr, which the dialog shows); 3 when a material the command needs is missing or malformed: a key
file, the incoming state, the values file, or the latchkey binary and its config themselves (2 is click's own
code for a usage error, which no retry in the sandbox cures). Nothing here prints a key, and no key is ever in
an argument.
"""

import json
import os
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import click
from imbue.mngr_latchkey.core import Latchkey, LatchkeyError
from loguru import logger

from auth_sandbox.environment import identity_from_environ, sign_in_environment
from auth_sandbox.errors import (
    LatchkeyCommandError,
    MissingMaterialError,
    ValuesFileError,
)
from auth_sandbox.latchkey_adapter import LatchkeyAdapter, SubprocessReencryptRunner
from auth_sandbox.primitives import (
    DEFAULT_LATCHKEY_BINARY,
    FORTRESS_LAUNCHER_PATH,
    LATCHKEY_BINARY_ENV_VAR,
    LATCHKEY_GATEWAY_ENV_VAR,
    RUNTIME_DIR,
)
from auth_sandbox.runtime_files import SandboxPaths, read_key
from auth_sandbox.set_credentials import (
    read_and_remove_values_file,
    run_set_credentials,
)
from auth_sandbox.sign_in import SignInOutcome, run_sign_in
from auth_sandbox.template_config import render_template_config
from auth_sandbox.transfer import export_credentials, import_browser_state

EXIT_REFUSED = 1
EXIT_MISSING_MATERIAL = 3

_runtime_dir_option = click.option(
    "--runtime-dir",
    type=click.Path(path_type=Path),
    default=RUNTIME_DIR,
    show_default=True,
    help="The sandbox's tmpfs: the keys, the incoming state, the export and the latchkey directory.",
)
_latchkey_binary_option = click.option(
    "--latchkey-binary",
    default=lambda: os.environ.get(LATCHKEY_BINARY_ENV_VAR, DEFAULT_LATCHKEY_BINARY),
    help=f"The latchkey CLI to run (default: ${LATCHKEY_BINARY_ENV_VAR}, else the one on PATH).",
)


def _quiet_logging() -> None:
    """Only errors on stderr: the dialog shows stderr, and the outcome line below is what it should see."""
    logger.remove()
    logger.add(sys.stderr, level="ERROR")


def _apply_sign_in_environment(environment: Mapping[str, str]) -> None:
    """Put the sign-in environment into this process, which every ``latchkey`` child inherits."""
    os.environ.pop(LATCHKEY_GATEWAY_ENV_VAR, None)
    os.environ.update(environment)


def _build_latchkey(paths: SandboxPaths, latchkey_binary: str) -> LatchkeyAdapter:
    """The desktop's wrapper over the sandbox's directory, with the sandbox's key in the environment."""
    key = read_key(paths.latchkey_key_path)
    paths.latchkey_directory.mkdir(parents=True, exist_ok=True)
    _apply_sign_in_environment(
        sign_in_environment(
            os.environ, identity_from_environ(os.environ), paths.latchkey_directory, key
        )
    )
    return LatchkeyAdapter(
        latchkey=Latchkey(
            latchkey_binary=latchkey_binary, latchkey_directory=paths.latchkey_directory
        )
    )


def _finish(outcome: SignInOutcome) -> None:
    if outcome.is_stored:
        return
    click.echo(outcome.detail or "latchkey stored no credential", err=True)
    sys.exit(EXIT_REFUSED)


@contextmanager
def _exit_on_sandbox_failure() -> Generator[None, None, None]:
    """Turn a command's failure into the exit code of the module docstring, its message on stderr.

    A material that is missing or malformed exits 3: a key file, the incoming state, the values file, and the
    ``latchkey`` the desktop's wrapper cannot run (``Latchkey.initialize`` raises ``LatchkeyError`` for a binary
    that is absent or too old and for a config it cannot write; a refusal of its own it answers as a result, not
    an exception). A ``latchkey`` child that refused (a re-encrypt of a service it has no store for) exits 1.
    """
    try:
        yield
    except (MissingMaterialError, ValuesFileError, LatchkeyError) as e:
        click.echo(str(e), err=True)
        sys.exit(EXIT_MISSING_MATERIAL)
    except LatchkeyCommandError as e:
        click.echo(str(e), err=True)
        sys.exit(EXIT_REFUSED)


@click.command()
@click.argument("service")
@click.option(
    "--account",
    default=None,
    help=(
        "The account to store the credential under, for a service that cannot tell who logged in; a service "
        "that can refuses an account the sandbox's fresh store does not hold, so the chrome names none there."
    ),
)
@_runtime_dir_option
@_latchkey_binary_option
def sign_in_command(
    service: str, account: str | None, runtime_dir: Path, latchkey_binary: str
) -> None:
    """Run latchkey's browser sign-in for SERVICE on the Screen, storing the credential in the sandbox."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        outcome = run_sign_in(_build_latchkey(paths, latchkey_binary), service, account)
    _finish(outcome)


@click.command()
@click.argument("service")
@click.option(
    "--account", required=True, help="The account the credentials are stored under."
)
@click.option(
    "--values-file",
    required=True,
    type=click.Path(path_type=Path),
    help='A JSON object {"values": {placeholder: value}, "set_credentials_example": "..."?}; deleted once read.',
)
@_runtime_dir_option
@_latchkey_binary_option
def set_credentials_command(
    service: str,
    account: str,
    values_file: Path,
    runtime_dir: Path,
    latchkey_binary: str,
) -> None:
    """Store typed credentials for SERVICE from a values file, as the desktop's credential form does."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        values = read_and_remove_values_file(values_file)
        outcome = run_set_credentials(
            _build_latchkey(paths, latchkey_binary), service, account, values
        )
    _finish(outcome)


@click.command()
@_runtime_dir_option
@_latchkey_binary_option
def import_browser_state_command(runtime_dir: Path, latchkey_binary: str) -> None:
    """Re-encrypt the incoming browser state (under the transfer key) into the sandbox's latchkey directory."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        written = import_browser_state(
            SubprocessReencryptRunner(), paths, latchkey_binary, os.environ
        )
    click.echo(json.dumps({"browser_state": str(written)}))


@click.command()
@click.argument("service")
@click.option(
    "--account",
    default=None,
    help="Export only this account's credentials for the service.",
)
@_runtime_dir_option
@_latchkey_binary_option
def export_command(
    service: str, account: str | None, runtime_dir: Path, latchkey_binary: str
) -> None:
    """Re-encrypt SERVICE's credentials and the browser state under the transfer key; print the paths as JSON."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        bundle = export_credentials(
            SubprocessReencryptRunner(),
            paths,
            latchkey_binary,
            os.environ,
            service,
            account,
        )
    click.echo(
        json.dumps(
            {
                "credentials": str(bundle.credentials),
                "data_format_version": str(bundle.data_format_version),
                "browser_state": None
                if bundle.browser_state is None
                else str(bundle.browser_state),
            }
        )
    )


@click.command()
@click.argument("output", type=click.Path(path_type=Path))
@click.option(
    "--browser-executable",
    type=click.Path(path_type=Path),
    default=FORTRESS_LAUNCHER_PATH,
    show_default=True,
    help="The browser latchkey launches for a sign-in.",
)
def render_config_command(output: Path, browser_executable: Path) -> None:
    """Write the template config.json the image carries (run at image build, not in a live sandbox)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_template_config(browser_executable, datetime.now(timezone.utc))
    )
