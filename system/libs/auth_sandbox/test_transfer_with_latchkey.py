"""The session's key against the real latchkey CLI (the one setup_system.sh installs in the image): prepare lands the
machine's browser state under the dialog's key K, a credential the sandbox stores under K is printed as its account,
and the files the chrome carries to the VM merge there with K as the source key, as the VM's apply-state does."""

import base64
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from auth_sandbox.cli import (
    prepare_command,
    render_config_command,
    set_credentials_command,
)
from auth_sandbox.environment import sandbox_identity, sign_in_environment
from auth_sandbox.runtime_files import SandboxPaths
from auth_sandbox.session import SessionSettings
from auth_sandbox.store_queries import store_environment
from auth_sandbox.testing import (
    fabricate_browser_state,
    latchkey_on_path,
    list_accounts,
    read_browser_state,
    store_credential,
)
from click.testing import CliRunner
from pydantic import SecretStr

_STATE: dict[str, object] = {"cookies": [{"name": "sid", "value": "1"}], "origins": []}
# What the chrome reads from the sandbox's latchkey directory and hands the VM.
_TRANSFERRED_FILES = (
    "credentials.json.enc",
    "data-format-version",
    "browser_state.json.enc",
)


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))


@pytest.fixture
def latchkey_binary() -> str:
    binary = latchkey_on_path()
    if binary is None:
        pytest.skip("no latchkey on PATH")
    return binary


@pytest.fixture
def booted_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SandboxPaths:
    """A booted sandbox's runtime dir before the session: the template config, no key, no store."""
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    paths = SandboxPaths(runtime_dir=tmp_path / "run")
    paths.latchkey_directory.mkdir(parents=True)
    result = CliRunner().invoke(
        render_config_command,
        [
            str(paths.latchkey_directory / "config.json"),
            "--browser-executable",
            "/bin/true",
        ],
    )
    assert result.exit_code == 0, result.output
    return paths


def _prepare(
    paths: SandboxPaths,
    latchkey_binary: str,
    key: SecretStr,
    browser_state: bytes | None,
    service: str,
) -> dict[str, object]:
    session_file = paths.runtime_dir / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "latchkey_key": key.get_secret_value(),
                "browser_state": None
                if browser_state is None
                else base64.b64encode(browser_state).decode("ascii"),
                "workspace_host_id": "host-" + "b" * 32,
                "timezone": "Europe/Paris",
            }
        )
    )
    result = CliRunner().invoke(
        prepare_command,
        [
            "--session-file",
            str(session_file),
            "--describe",
            service,
            "--runtime-dir",
            str(paths.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )
    assert result.exit_code == 0, result.output
    assert not session_file.exists()
    # The chrome parses the whole of stdout as the description.
    assert result.stdout.count("\n") == 1
    return json.loads(result.stdout)


def _machine_browser_state(
    latchkey_binary: str, tmp_path: Path, key: SecretStr
) -> bytes:
    """The workspace machine's browser state as the VM's read-state hands it over: encrypted under K."""
    return fabricate_browser_state(
        latchkey_binary, tmp_path / "machine-state", key, _STATE
    ).read_bytes()


def test_prepare_lands_the_machines_browser_state_under_the_session_key_and_describes_the_service(
    booted_sandbox: SandboxPaths, latchkey_binary: str, tmp_path: Path
) -> None:
    key = _key()

    described = _prepare(
        booted_sandbox,
        latchkey_binary,
        key,
        _machine_browser_state(latchkey_binary, tmp_path, key),
        "slack",
    )

    auth_options = described["authOptions"]
    assert isinstance(auth_options, list)
    assert "browser" in auth_options
    assert described["credentials"] == {}
    assert booted_sandbox.latchkey_key_path.read_text() == key.get_secret_value() + "\n"
    assert (
        read_browser_state(
            latchkey_binary, booted_sandbox.latchkey_browser_state_path, key
        )
        == _STATE
    )
    # latchkey opens the store directory under K with the state in it, and holds no credential yet.
    assert list_accounts(latchkey_binary, booted_sandbox.latchkey_directory, key) == {}


def test_prepare_answers_nothing_for_a_service_latchkey_does_not_know(
    booted_sandbox: SandboxPaths, latchkey_binary: str
) -> None:
    assert (
        _prepare(booted_sandbox, latchkey_binary, _key(), None, "no-such-service") == {}
    )
    assert not booted_sandbox.latchkey_browser_state_path.exists()


def test_a_credential_stored_under_the_session_key_is_printed_and_merges_into_the_vms_store(
    booted_sandbox: SandboxPaths, latchkey_binary: str, tmp_path: Path
) -> None:
    key = _key()
    _prepare(
        booted_sandbox,
        latchkey_binary,
        key,
        _machine_browser_state(latchkey_binary, tmp_path, key),
        "slack",
    )
    values_file = booted_sandbox.runtime_dir / "values.json"
    values_file.write_text(json.dumps({"values": {"token": "xoxc-typed"}}))
    # The command puts the sign-in environment into this process; naming those variables in `env` has the
    # runner restore each of them afterwards.
    sign_in_variables = sign_in_environment(
        {},
        sandbox_identity({}, SessionSettings(workspace_host_id=None, timezone=None)),
        booted_sandbox.latchkey_directory,
        key,
    )

    stored = CliRunner().invoke(
        set_credentials_command,
        [
            "slack",
            "--account",
            "me@example.com",
            "--values-file",
            str(values_file),
            "--runtime-dir",
            str(booted_sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
        env=dict.fromkeys(sign_in_variables),
    )

    assert stored.exit_code == 0, stored.output
    # The chrome reads the last non-empty line of stdout: the service's own account map, keyed by account.
    last_line = [line for line in stored.stdout.splitlines() if line.strip()][-1]
    assert json.loads(last_line) == {
        "accounts": {
            "me@example.com": {
                "credentialType": "rawCurl",
                "credentialStatus": "unknown",
            }
        }
    }
    assert key.get_secret_value() not in stored.output

    # The chrome carries three files of the sandbox's directory to the VM, which merges them into its own store
    # (under the machine's key, holding a credential already) with K as the source key.
    transferred = tmp_path / "transferred"
    transferred.mkdir()
    for name in _TRANSFERRED_FILES:
        shutil.copy2(booted_sandbox.latchkey_directory / name, transferred / name)
    machine_key = _key()
    machine = tmp_path / "machine"
    store_credential(latchkey_binary, machine, machine_key, "linear", None)
    merged = subprocess.run(
        [
            latchkey_binary,
            "auth",
            "re-encrypt",
            str(machine),
            "--services",
            "slack",
            "--browser-state",
        ],
        env=store_environment(os.environ, transferred, key),
        input=machine_key.get_secret_value() + "\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert merged.returncode == 0, merged.stderr
    assert list_accounts(latchkey_binary, machine, machine_key) == {
        "linear": [""],
        "slack": ["me@example.com"],
    }
    assert (
        read_browser_state(
            latchkey_binary, machine / "browser_state.json.enc", machine_key
        )
        == _STATE
    )
