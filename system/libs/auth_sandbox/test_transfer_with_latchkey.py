"""The transfer commands against the real latchkey CLI (the one setup_system.sh installs in the image):
the sandbox-side import and export of spec 6.2 and 6.4, and the VM-side merge they feed."""

import base64
import json
import os
import secrets
import subprocess
from pathlib import Path

import pytest
from auth_sandbox.cli import (
    EXIT_REFUSED,
    export_command,
    import_browser_state_command,
    render_config_command,
)
from auth_sandbox.runtime_files import SandboxPaths
from auth_sandbox.testing import (
    fabricate_browser_state,
    latchkey_on_path,
    list_accounts,
    read_browser_state,
    store_credential,
)
from auth_sandbox.transfer import reencrypt_argv, reencrypt_environment
from click.testing import CliRunner
from pydantic import SecretStr

_STATE: dict[str, object] = {"cookies": [{"name": "sid", "value": "1"}], "origins": []}


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))


@pytest.fixture
def latchkey_binary() -> str:
    binary = latchkey_on_path()
    if binary is None:
        pytest.skip("no latchkey on PATH")
    return binary


@pytest.fixture
def booted_sandbox(
    tmp_path: Path, latchkey_binary: str, monkeypatch: pytest.MonkeyPatch
) -> SandboxPaths:
    """A booted sandbox's runtime dir before any sign-in: the keys and the template config, no store."""
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    paths = SandboxPaths(runtime_dir=tmp_path / "run")
    paths.incoming_dir.mkdir(parents=True)
    paths.latchkey_directory.mkdir()
    paths.latchkey_key_path.write_text(_key().get_secret_value() + "\n")
    paths.transfer_key_path.write_text(_key().get_secret_value() + "\n")
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


@pytest.fixture
def sandbox(booted_sandbox: SandboxPaths, latchkey_binary: str) -> SandboxPaths:
    """The booted sandbox with a sign-in's worth of credentials stored."""
    sandbox_key = _sandbox_key(booted_sandbox)
    store_credential(
        latchkey_binary, booted_sandbox.latchkey_directory, sandbox_key, "github", None
    )
    store_credential(
        latchkey_binary,
        booted_sandbox.latchkey_directory,
        sandbox_key,
        "slack",
        "me@example.com",
    )
    return booted_sandbox


def _transfer_key(paths: SandboxPaths) -> SecretStr:
    return SecretStr(paths.transfer_key_path.read_text().strip())


def _sandbox_key(paths: SandboxPaths) -> SecretStr:
    return SecretStr(paths.latchkey_key_path.read_text().strip())


def test_import_lands_the_machines_browser_state_in_the_sandbox_store_under_the_sandbox_key(
    sandbox: SandboxPaths, latchkey_binary: str
) -> None:
    fabricate_browser_state(
        latchkey_binary, sandbox.incoming_dir, _transfer_key(sandbox), _STATE
    )

    result = CliRunner().invoke(
        import_browser_state_command,
        [
            "--runtime-dir",
            str(sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "browser_state": str(sandbox.latchkey_browser_state_path)
    }
    assert (
        read_browser_state(
            latchkey_binary, sandbox.latchkey_browser_state_path, _sandbox_key(sandbox)
        )
        == _STATE
    )
    # The credentials the sign-in stored beside it are untouched by the merge.
    assert list_accounts(
        latchkey_binary, sandbox.latchkey_directory, _sandbox_key(sandbox)
    ) == {
        "github": [""],
        "slack": ["me@example.com"],
    }


def test_import_into_a_store_less_sandbox_then_sign_in_then_export_as_every_real_sign_in_does(
    booted_sandbox: SandboxPaths, latchkey_binary: str
) -> None:
    # Spec 6.2: the import runs before any sign-in, into a directory holding only the template config.
    fabricate_browser_state(
        latchkey_binary,
        booted_sandbox.incoming_dir,
        _transfer_key(booted_sandbox),
        _STATE,
    )
    assert sorted(
        path.name for path in booted_sandbox.latchkey_directory.iterdir()
    ) == ["config.json"]

    imported = CliRunner().invoke(
        import_browser_state_command,
        [
            "--runtime-dir",
            str(booted_sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )

    assert imported.exit_code == 0, imported.output
    assert (
        read_browser_state(
            latchkey_binary,
            booted_sandbox.latchkey_browser_state_path,
            _sandbox_key(booted_sandbox),
        )
        == _STATE
    )

    # The sign-in then stores the service's credential beside the imported state, and the export
    # carries both.
    store_credential(
        latchkey_binary,
        booted_sandbox.latchkey_directory,
        _sandbox_key(booted_sandbox),
        "slack",
        "me@example.com",
    )
    exported = CliRunner().invoke(
        export_command,
        [
            "slack",
            "--runtime-dir",
            str(booted_sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )

    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["browser_state"] == str(
        booted_sandbox.exported_browser_state_path
    )
    assert list_accounts(
        latchkey_binary, booted_sandbox.export_dir, _transfer_key(booted_sandbox)
    ) == {"slack": ["me@example.com"]}


def test_export_writes_one_services_bundle_the_machine_merges_under_its_own_key(
    sandbox: SandboxPaths, latchkey_binary: str, tmp_path: Path
) -> None:
    fabricate_browser_state(
        latchkey_binary, sandbox.latchkey_directory, _sandbox_key(sandbox), _STATE
    )

    result = CliRunner().invoke(
        export_command,
        [
            "slack",
            "--account",
            "me@example.com",
            "--runtime-dir",
            str(sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )

    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)
    assert answer == {
        "credentials": str(sandbox.exported_credentials_path),
        "data_format_version": str(sandbox.exported_data_format_version_path),
        "browser_state": str(sandbox.exported_browser_state_path),
    }
    assert list_accounts(
        latchkey_binary, sandbox.export_dir, _transfer_key(sandbox)
    ) == {"slack": ["me@example.com"]}
    assert (
        read_browser_state(
            latchkey_binary, sandbox.exported_browser_state_path, _transfer_key(sandbox)
        )
        == _STATE
    )

    # The VM side (spec 6.4 step 3): the bundle under the transfer key merges into a machine store that already
    # holds a credential under the machine's own key, which then reads everything.
    machine_key = _key()
    machine = tmp_path / "machine"
    store_credential(latchkey_binary, machine, machine_key, "linear", None)
    merged = subprocess.run(
        reencrypt_argv(latchkey_binary, machine, ("slack",), True, None),
        env=reencrypt_environment(
            os.environ, sandbox.export_dir, _transfer_key(sandbox)
        ),
        input=machine_key.get_secret_value() + "\n",
        capture_output=True,
        text=True,
        check=False,
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


def test_export_without_a_browser_state_answers_null_and_an_unknown_service_is_refused(
    sandbox: SandboxPaths, latchkey_binary: str
) -> None:
    runner = CliRunner()
    exported = runner.invoke(
        export_command,
        [
            "github",
            "--runtime-dir",
            str(sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output)["browser_state"] is None
    assert list_accounts(
        latchkey_binary, sandbox.export_dir, _transfer_key(sandbox)
    ) == {"github": [""]}

    refused = runner.invoke(
        export_command,
        [
            "gitlab",
            "--runtime-dir",
            str(sandbox.runtime_dir),
            "--latchkey-binary",
            latchkey_binary,
        ],
    )
    assert refused.exit_code == EXIT_REFUSED
    assert "gitlab" in refused.output
    assert _transfer_key(sandbox).get_secret_value() not in refused.output
