"""The commands owner-exec runs on the chrome's behalf, and the claim watcher the boot script runs, each a thin entry
point over the modules beside it.

Exit codes: 0 when the command did what it says; 1 when latchkey refused (its reason, condensed, is the last line on
stderr, which the dialog shows); 3 when a material the command needs is missing or malformed (the session file, the
key and settings prepare keeps from it, the values file, or the latchkey binary and its config themselves), or a
file or directory of the sandbox's cannot be read or written (2 is click's own code for a usage error, which no retry
in the sandbox cures); 4 when the browser sign-in was asked of a service that takes typed credentials instead.
Nothing here prints a key, and no key is ever in an argument.
"""

import json
import os
import shutil
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import click
from imbue.mngr_latchkey.core import Latchkey, LatchkeyError
from loguru import logger
from pydantic import JsonValue

from auth_sandbox.claim import CLAIM_POLL_INTERVAL_SECONDS, ClaimPaths, serve_claim
from auth_sandbox.environment import sandbox_identity, sign_in_environment
from auth_sandbox.errors import (
    LatchkeyCommandError,
    MissingMaterialError,
    SessionFileError,
    ValuesFileError,
)
from auth_sandbox.latchkey_adapter import (
    LatchkeyAdapter,
    SubprocessLatchkeyRunner,
    SystemClock,
)
from auth_sandbox.primitives import (
    AUTHORIZED_KEYS_PATH,
    CLAIM_PATH,
    DEFAULT_LATCHKEY_BINARY,
    LATCHKEY_BINARY_ENV_VAR,
    LATCHKEY_GATEWAY_ENV_VAR,
    RUNTIME_DIR,
    SIGN_IN_BROWSER_PATH,
)
from auth_sandbox.runtime_files import SandboxPaths, read_key
from auth_sandbox.session import (
    install_session,
    read_and_remove_session_file,
    read_session_settings,
)
from auth_sandbox.set_credentials import (
    read_and_remove_values_file,
    run_set_credentials,
)
from auth_sandbox.sign_in import (
    SignInOutcome,
    account_for_browser_sign_in,
    offers_browser_sign_in,
    run_sign_in,
)
from auth_sandbox.store_queries import (
    describe_service,
    store_environment,
    stored_accounts,
)
from auth_sandbox.template_config import render_template_config

EXIT_REFUSED = 1
EXIT_MISSING_MATERIAL = 3
EXIT_TYPED_CREDENTIALS_NEEDED = 4

_runtime_dir_option = click.option(
    "--runtime-dir",
    type=click.Path(path_type=Path),
    default=RUNTIME_DIR,
    show_default=True,
    help="The sandbox's tmpfs: the session's key and settings, the owner token digest and the latchkey directory.",
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
    """Put the sign-in environment into this process, which every ``latchkey`` child inherits.

    A deliberate exception to never mutating ``os.environ``: the desktop's ``Latchkey`` wrapper builds every
    child's environment from ``os.environ`` and takes none of its own, and this process is a one-shot command.
    """
    os.environ.pop(LATCHKEY_GATEWAY_ENV_VAR, None)
    os.environ.update(environment)


def _build_latchkey(paths: SandboxPaths, latchkey_binary: str) -> LatchkeyAdapter:
    """The desktop's wrapper over the sandbox's directory, with the session's key and fingerprint in the
    environment.

    The binary is only looked up, not run: the wrapper would let a missing one escape its flows as a process
    error rather than a ``LatchkeyError``, and the image build has already checked its version.
    """
    key = read_key(paths.latchkey_key_path)
    settings = read_session_settings(paths.session_settings_path)
    paths.latchkey_directory.mkdir(parents=True, exist_ok=True)
    if shutil.which(latchkey_binary) is None:
        raise MissingMaterialError(f"no latchkey at {latchkey_binary}")
    _apply_sign_in_environment(
        sign_in_environment(
            os.environ,
            sandbox_identity(os.environ, settings),
            paths.latchkey_directory,
            key,
        )
    )
    return LatchkeyAdapter(
        latchkey=Latchkey(
            latchkey_binary=latchkey_binary, latchkey_directory=paths.latchkey_directory
        )
    )


def _stored_accounts(latchkey_binary: str, service: str) -> dict[str, JsonValue]:
    """The store's accounts for the service, which a sign-in command prints on success to name the account the
    chrome transfers. Run in the environment ``_build_latchkey`` put into this process."""
    with _exit_on_sandbox_failure():
        return stored_accounts(
            SubprocessLatchkeyRunner(), latchkey_binary, os.environ, service
        )


def _describe_for_sign_in(latchkey_binary: str, service: str) -> dict[str, JsonValue]:
    """What latchkey knows of the service, in the environment ``_build_latchkey`` put into this process.

    A description latchkey fails to give is ``{}``, which signs in through the browser, as the desktop does when
    ``services info`` has no answer; its reason goes to stderr.
    """
    try:
        return describe_service(
            SubprocessLatchkeyRunner(), latchkey_binary, os.environ, service
        )
    except LatchkeyCommandError as e:
        click.echo(f"{e}; signing in through the browser", err=True)
        return {}


def _finish(outcome: SignInOutcome) -> None:
    if outcome.is_stored:
        return
    click.echo(outcome.detail or "latchkey stored no credential", err=True)
    sys.exit(EXIT_REFUSED)


@contextmanager
def _exit_on_sandbox_failure() -> Generator[None, None, None]:
    """Turn a command's failure into the exit code of the module docstring, its message on stderr.

    A material that is missing or malformed exits 3: the session file and what prepare keeps from it, the values
    file, a ``latchkey`` binary that is not there, and a latchkey config the merge cannot parse (``LatchkeyError``;
    a refusal of its own the desktop's wrapper answers as a result, not an exception). So does a file or directory of the sandbox's that cannot be read or written (an ``OSError``,
    the config's included), which no retry in the sandbox cures either. A ``latchkey`` query that failed (the
    description, or the listing after a stored credential) exits 1 with its reason.
    """
    try:
        yield
    except (
        MissingMaterialError,
        ValuesFileError,
        SessionFileError,
        LatchkeyError,
        OSError,
    ) as e:
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
        "The account the user picked; named to latchkey only for a service that cannot tell who logged in (a "
        "service that can stores under the account it detects and refuses one the sandbox's fresh store lacks)."
    ),
)
@click.option(
    "--fresh",
    "is_fresh",
    is_flag=True,
    help=(
        "Start the browser without the machine's session (adding a new account): the browser state prepare "
        "installed is removed first."
    ),
)
@_runtime_dir_option
@_latchkey_binary_option
def sign_in_command(
    service: str,
    account: str | None,
    is_fresh: bool,
    runtime_dir: Path,
    latchkey_binary: str,
) -> None:
    """Run latchkey's browser sign-in for SERVICE on the Screen, storing the credential in the sandbox; print
    {"accounts": <the store's accounts for SERVICE>, "services_info": <what latchkey knows of SERVICE>}.

    A service that takes typed credentials instead launches nothing: the command prints {"services_info": ...}
    and exits 4.
    """
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        latchkey = _build_latchkey(paths, latchkey_binary)
        services_info = _describe_for_sign_in(latchkey_binary, service)
    if not offers_browser_sign_in(services_info):
        click.echo(json.dumps({"services_info": services_info}))
        sys.exit(EXIT_TYPED_CREDENTIALS_NEEDED)
    with _exit_on_sandbox_failure():
        if is_fresh:
            paths.latchkey_browser_state_path.unlink(missing_ok=True)
        outcome = run_sign_in(
            latchkey, service, account_for_browser_sign_in(services_info, account)
        )
    _finish(outcome)
    accounts = _stored_accounts(latchkey_binary, service)
    click.echo(json.dumps({"accounts": accounts, "services_info": services_info}))


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
    """Store typed credentials for SERVICE from a values file, as the desktop's credential form does; print the
    store's accounts for SERVICE as {"accounts": ...}."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        values = read_and_remove_values_file(values_file)
        outcome = run_set_credentials(
            _build_latchkey(paths, latchkey_binary), service, account, values
        )
    _finish(outcome)
    click.echo(json.dumps({"accounts": _stored_accounts(latchkey_binary, service)}))


@click.command()
@click.option(
    "--session-file",
    required=True,
    type=click.Path(path_type=Path),
    help="The session file the chrome wrote (deleted once read).",
)
@click.option(
    "--describe",
    "service",
    default=None,
    help="A service whose `latchkey services info --offline` JSON to print once the session is installed.",
)
@_runtime_dir_option
@_latchkey_binary_option
def prepare_command(
    session_file: Path, service: str | None, runtime_dir: Path, latchkey_binary: str
) -> None:
    """Install the session file's key, browser state and settings; with --describe, print what latchkey knows of
    the service, or {} when it knows no such service."""
    _quiet_logging()
    paths = SandboxPaths(runtime_dir=runtime_dir)
    with _exit_on_sandbox_failure():
        material = read_and_remove_session_file(session_file)
        install_session(material, paths)
        if service is None:
            return
        description = describe_service(
            SubprocessLatchkeyRunner(),
            latchkey_binary,
            store_environment(
                os.environ, paths.latchkey_directory, material.latchkey_key
            ),
            service,
        )
    click.echo(json.dumps(description))


@click.command()
@click.option(
    "--claim-file",
    type=click.Path(path_type=Path),
    default=CLAIM_PATH,
    show_default=True,
    help="The claim the connector writes.",
)
@click.option(
    "--authorized-keys",
    type=click.Path(path_type=Path),
    default=AUTHORIZED_KEYS_PATH,
    show_default=True,
    help="owner-exec's authorized_keys, which the claim's key replaces.",
)
@_runtime_dir_option
def await_claim_command(
    claim_file: Path, authorized_keys: Path, runtime_dir: Path
) -> None:
    """Wait for the connector's claim, admit the user it names, and return when their session expires (the boot
    script then ends the sandbox)."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    paths = ClaimPaths(
        claim_path=claim_file,
        authorized_keys_path=authorized_keys,
        screen_token_sha256_path=SandboxPaths(
            runtime_dir=runtime_dir
        ).screen_token_sha256_path,
    )
    with _exit_on_sandbox_failure():
        serve_claim(paths, SystemClock(), CLAIM_POLL_INTERVAL_SECONDS)


@click.command()
@click.argument("output", type=click.Path(path_type=Path))
@click.option(
    "--browser-executable",
    type=click.Path(path_type=Path),
    default=SIGN_IN_BROWSER_PATH,
    show_default=True,
    help="The browser latchkey launches for a sign-in.",
)
def render_config_command(output: Path, browser_executable: Path) -> None:
    """Write the template config.json the image carries (run at image build, not in a live sandbox)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_template_config(browser_executable, datetime.now(timezone.utc))
    )
