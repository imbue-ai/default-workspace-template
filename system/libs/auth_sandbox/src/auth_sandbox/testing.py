"""Helpers for the tests that run a real ``latchkey``: fabricated stores and browser states."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from imbue.mngr_latchkey.core import BROWSER_STATE_FILENAME
from pydantic import SecretStr

from auth_sandbox.errors import LatchkeyCommandError

# Each program takes its arguments after the module path, whatever node put ahead of them in ``argv``.
_FABRICATE_STATE_PROGRAM = """
const args = process.argv.slice(process.argv.findIndex((arg) => arg.endsWith("encryptedStorage.js")));
const { EncryptedStorage } = await import(args[0]);
new EncryptedStorage(args[1]).writeFile(args[2], args[3]);
"""
_READ_STATE_PROGRAM = """
const args = process.argv.slice(process.argv.findIndex((arg) => arg.endsWith("encryptedStorage.js")));
const { EncryptedStorage } = await import(args[0]);
process.stdout.write(new EncryptedStorage(args[1]).readFile(args[2]));
"""


def latchkey_on_path() -> str | None:
    """The ``latchkey`` the environment offers, or None when there is none to test against."""
    return shutil.which("latchkey")


def _encrypted_storage_module(latchkey_binary: str) -> Path:
    """The CLI's own ``EncryptedStorage`` module, found from the binary's real location."""
    resolved = Path(os.path.realpath(latchkey_binary))
    for parent in resolved.parents:
        if parent.name == "latchkey" and (parent / "package.json").is_file():
            module = parent / "dist" / "src" / "encryptedStorage.js"
            if module.is_file():
                return module
    raise LatchkeyCommandError(
        f"could not find latchkey's package beside {latchkey_binary}"
    )


def _run_node(program: str, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", program, "--", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise LatchkeyCommandError(
            f"node exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def fabricate_browser_state(
    latchkey_binary: str, directory: Path, key: SecretStr, state: dict[str, object]
) -> Path:
    """Write ``state`` as ``browser_state.json.enc`` under ``key``, exactly as a completed sign-in would."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / BROWSER_STATE_FILENAME
    _run_node(
        _FABRICATE_STATE_PROGRAM,
        [
            str(_encrypted_storage_module(latchkey_binary)),
            key.get_secret_value(),
            str(path),
            json.dumps(state),
        ],
    )
    return path


def read_browser_state(
    latchkey_binary: str, path: Path, key: SecretStr
) -> dict[str, object]:
    """Decrypt a browser state file with ``key`` through the CLI's own storage module."""
    text = _run_node(
        _READ_STATE_PROGRAM,
        [
            str(_encrypted_storage_module(latchkey_binary)),
            key.get_secret_value(),
            str(path),
        ],
    )
    return json.loads(text)


def store_credential(
    latchkey_binary: str,
    directory: Path,
    key: SecretStr,
    service: str,
    account: str | None,
) -> None:
    """``latchkey auth set`` a raw header credential into ``directory`` under ``key``, as the harness does."""
    argv = [latchkey_binary]
    if account is not None:
        argv.extend(["--account", account])
    argv.extend(
        ["auth", "set", service, "-H", f"Authorization: Bearer {service}-token"]
    )
    completed = subprocess.run(
        argv,
        env={
            **os.environ,
            "LATCHKEY_DIRECTORY": str(directory),
            "LATCHKEY_ENCRYPTION_KEY": key.get_secret_value(),
            "LATCHKEY_DISABLE_COUNTING": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise LatchkeyCommandError(
            f"latchkey auth set exited {completed.returncode}: {completed.stderr.strip()}"
        )


def list_accounts(
    latchkey_binary: str, directory: Path, key: SecretStr
) -> dict[str, list[str]]:
    """The accounts per service a store holds, read with ``latchkey auth list`` under ``key``."""
    completed = subprocess.run(
        [latchkey_binary, "auth", "list"],
        env={
            **os.environ,
            "LATCHKEY_DIRECTORY": str(directory),
            "LATCHKEY_ENCRYPTION_KEY": key.get_secret_value(),
            "LATCHKEY_DISABLE_COUNTING": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise LatchkeyCommandError(
            f"latchkey auth list exited {completed.returncode}: {completed.stderr.strip()}"
        )
    listed = json.loads(completed.stdout)
    return {service: sorted(accounts) for service, accounts in listed.items()}
