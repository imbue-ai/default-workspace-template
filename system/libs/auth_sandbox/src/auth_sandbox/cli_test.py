import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from auth_sandbox.cli import (
    EXIT_MISSING_MATERIAL,
    EXIT_REFUSED,
    _finish,
    _print_stored_accounts,
    prepare_command,
    render_config_command,
    set_credentials_command,
    sign_in_command,
)
from auth_sandbox.environment import sandbox_identity, sign_in_environment
from auth_sandbox.session import SessionSettings
from auth_sandbox.sign_in import SignInOutcome

_KEY = base64.b64encode(bytes(32)).decode("ascii")
_NO_SETTINGS = '{"workspace_host_id": null, "timezone": null}'


def _prepared(runtime_dir: Path) -> None:
    """What auth-sandbox-prepare leaves for the sign-in commands."""
    (runtime_dir / "session_settings.json").write_text(_NO_SETTINGS)
    (runtime_dir / "latchkey_key").write_text("key\n")


def test_sign_in_before_prepare_names_the_missing_key_and_runs_nothing(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        sign_in_command,
        [
            "slack",
            "--runtime-dir",
            str(tmp_path),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    assert result.exit_code == EXIT_MISSING_MATERIAL
    assert "latchkey_key" in result.output
    assert not (tmp_path / "latchkey").exists()


def test_sign_in_with_a_latchkey_it_cannot_run_is_missing_material_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A binary that is absent is a defect of the sandbox no retry in it cures, so it must not share
    # the refusal's code, which the chrome retries in the same sandbox.
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    _prepared(tmp_path)
    # The command puts the sign-in environment into this process; naming those variables in `env` has the
    # runner restore each of them afterwards, set or not before.
    sign_in_variables = sign_in_environment(
        {},
        sandbox_identity({}, SessionSettings(workspace_host_id=None, timezone=None)),
        tmp_path,
        SecretStr("key"),
    )
    result = CliRunner().invoke(
        sign_in_command,
        [
            "slack",
            "--runtime-dir",
            str(tmp_path),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
        env=dict.fromkeys(sign_in_variables),
    )
    assert result.exit_code == EXIT_MISSING_MATERIAL
    assert "/nonexistent/latchkey" in result.output


def test_a_sandbox_directory_that_cannot_be_written_is_missing_material_not_a_refusal(
    tmp_path: Path,
) -> None:
    _prepared(tmp_path)
    # A file where the sandbox's latchkey directory goes: no retry in the sandbox makes it writable.
    (tmp_path / "latchkey").write_text("not a directory")
    result = CliRunner().invoke(
        sign_in_command,
        [
            "slack",
            "--runtime-dir",
            str(tmp_path),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    assert result.exit_code == EXIT_MISSING_MATERIAL
    assert str(tmp_path / "latchkey") in result.output


def test_a_credential_latchkey_did_not_store_is_a_refusal_with_its_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exit 1 is what the chrome retries in the same sandbox; 3 would end its session.
    with pytest.raises(SystemExit) as refused:
        _finish(SignInOutcome(is_stored=False, detail="login window closed"))
    assert refused.value.code == EXIT_REFUSED
    assert capsys.readouterr().err.strip() == "login window closed"

    with pytest.raises(SystemExit) as unexplained:
        _finish(SignInOutcome(is_stored=False, detail=""))
    assert unexplained.value.code == EXIT_REFUSED
    assert capsys.readouterr().err.strip() == "latchkey stored no credential"

    _finish(SignInOutcome(is_stored=True, detail=""))
    assert capsys.readouterr().err == ""


def _fake_latchkey(tmp_path: Path, body: str) -> Path:
    """A stand-in ``latchkey`` that records its arguments and then runs ``body``."""
    executable = tmp_path / "latchkey"
    executable.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" > "{tmp_path / "argv.log"}"\n{body}\n'
    )
    executable.chmod(0o755)
    return executable


def test_a_stored_credential_prints_the_services_accounts_as_the_last_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The chrome parses the last stdout line to name the account it transfers.
    listing = {
        "slack": {"me@example.com": {"credentialType": "browser"}},
        "linear": {"": {"credentialType": "rawCurl"}},
    }
    latchkey = _fake_latchkey(tmp_path, f"echo '{json.dumps(listing)}'")
    _print_stored_accounts(str(latchkey), "slack")
    captured = capsys.readouterr()
    assert json.loads(captured.out.splitlines()[-1]) == {
        "accounts": {"me@example.com": {"credentialType": "browser"}}
    }
    assert captured.err == ""
    assert (tmp_path / "argv.log").read_text().strip() == "auth list --offline"


def test_a_failed_listing_after_a_stored_credential_is_a_refusal_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit 1 is what the chrome retries in the same sandbox, where the credential is already stored.
    latchkey = _fake_latchkey(tmp_path, "echo 'Error: Failed to decrypt.' >&2\nexit 1")
    with pytest.raises(SystemExit) as refused:
        _print_stored_accounts(str(latchkey), "slack")
    assert refused.value.code == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err.strip()
        == "listing the stored accounts failed: Error: Failed to decrypt."
    )


def test_set_credentials_needs_an_account_and_a_values_file(tmp_path: Path) -> None:
    runner = CliRunner()
    # click's usage-error code, which the missing-material code stays apart from.
    usage_error = runner.invoke(
        set_credentials_command, ["aws", "--values-file", str(tmp_path / "v.json")]
    ).exit_code
    assert usage_error == 2
    assert usage_error != EXIT_MISSING_MATERIAL
    (tmp_path / "v.json").write_text("not json")
    _prepared(tmp_path)
    result = runner.invoke(
        set_credentials_command,
        [
            "aws",
            "--account",
            "work",
            "--values-file",
            str(tmp_path / "v.json"),
            "--runtime-dir",
            str(tmp_path),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    assert result.exit_code == EXIT_MISSING_MATERIAL
    assert "not valid JSON" in result.output
    assert not (tmp_path / "v.json").exists()


def test_a_sign_in_whose_session_settings_are_missing_or_malformed_is_missing_material(
    tmp_path: Path,
) -> None:
    (tmp_path / "latchkey_key").write_text("key\n")
    arguments = [
        "slack",
        "--runtime-dir",
        str(tmp_path),
        "--latchkey-binary",
        "/nonexistent/latchkey",
    ]
    missing = CliRunner().invoke(sign_in_command, arguments)
    assert missing.exit_code == EXIT_MISSING_MATERIAL
    assert "session_settings.json" in missing.output
    (tmp_path / "session_settings.json").write_text("[]")
    malformed = CliRunner().invoke(sign_in_command, arguments)
    assert malformed.exit_code == EXIT_MISSING_MATERIAL
    assert "malformed" in malformed.output


def test_prepare_without_a_session_file_or_with_a_malformed_one_installs_nothing(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    session_file = runtime_dir / "session.json"
    arguments = [
        "--session-file",
        str(session_file),
        "--describe",
        "slack",
        "--runtime-dir",
        str(runtime_dir),
        "--latchkey-binary",
        "/nonexistent/latchkey",
    ]
    absent = CliRunner().invoke(prepare_command, arguments)
    assert absent.exit_code == EXIT_MISSING_MATERIAL
    assert "no session file" in absent.output

    session_file.write_text(json.dumps({"latchkey_key": "short"}))
    malformed = CliRunner().invoke(prepare_command, arguments)
    assert malformed.exit_code == EXIT_MISSING_MATERIAL
    assert "latchkey_key" in malformed.output
    assert not session_file.exists()
    assert sorted(path.name for path in runtime_dir.iterdir()) == []


def test_prepare_installs_the_session_before_it_asks_latchkey(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    session_file = runtime_dir / "session.json"
    session_file.write_text(json.dumps({"latchkey_key": _KEY, "browser_state": None}))
    result = CliRunner().invoke(
        prepare_command,
        [
            "--session-file",
            str(session_file),
            "--describe",
            "slack",
            "--runtime-dir",
            str(runtime_dir),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    # A latchkey that cannot run is missing material; what prepare installs is in place for a retry's sign-in.
    assert result.exit_code == EXIT_MISSING_MATERIAL
    assert "/nonexistent/latchkey" in result.output
    assert (runtime_dir / "latchkey_key").read_text() == _KEY + "\n"
    assert json.loads((runtime_dir / "session_settings.json").read_text()) == {
        "workspace_host_id": None,
        "timezone": None,
    }
    assert not session_file.exists()
    assert _KEY not in result.output


def test_render_config_writes_the_template_with_the_browser_entry(
    tmp_path: Path,
) -> None:
    output = tmp_path / "opt" / "config.json"
    result = CliRunner().invoke(
        render_config_command,
        [str(output), "--browser-executable", "/opt/fortress/tilion-fortress/tilion"],
    )
    assert result.exit_code == 0, result.output
    config = json.loads(output.read_text())
    assert config["browser"]["executablePath"] == "/opt/fortress/tilion-fortress/tilion"
    assert config["browser"]["source"] == "system"
