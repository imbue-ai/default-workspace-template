import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from auth_sandbox.cli import (
    EXIT_MISSING_MATERIAL,
    EXIT_REFUSED,
    EXIT_TYPED_CREDENTIALS_NEEDED,
    _finish,
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


_SLACK_INFO = {"type": "built-in", "authOptions": ["browser", "set"], "credentials": {}}
_AWS_INFO = {"type": "built-in", "authOptions": ["set"], "credentials": {}}
_LISTING = {
    "slack": {"me@example.com": {"credentialType": "browser"}},
    "linear": {"": {"credentialType": "rawCurl"}},
}


def _fake_latchkey(
    tmp_path: Path,
    services_info: str,
    auth_list: str = json.dumps(_LISTING),
    auth_browser_exit: int = 0,
) -> Path:
    """A stand-in ``latchkey`` that logs each invocation (and whether the store held a browser state then) and
    answers ``services info``, ``auth list`` and ``auth browser`` as told."""
    executable = tmp_path / "bin" / "latchkey"
    executable.parent.mkdir()
    log = tmp_path / "argv.log"
    executable.write_text(
        "#!/bin/sh\n"
        'state=absent; [ -e "$LATCHKEY_DIRECTORY/browser_state.json.enc" ] && state=present\n'
        f'printf "%s [%s]\\n" "$*" "$state" >> "{log}"\n'
        'case "$1 $2" in\n'
        f"  'services info') {services_info} ;;\n"
        f"  'auth list') echo '{auth_list}' ;;\n"
        f"  'auth browser') echo 'Error: Login cancelled by the user' >&2; exit {auth_browser_exit} ;;\n"
        "esac\n"
    )
    executable.chmod(0o755)
    return executable


def _invocations(tmp_path: Path) -> list[str]:
    return (tmp_path / "argv.log").read_text().splitlines()


def _sign_in(runtime_dir: Path, latchkey: Path, *options: str) -> tuple[int, str, str]:
    # The command puts the sign-in environment into this process; naming those variables in `env` has the
    # runner restore each of them afterwards, set or not before.
    sign_in_variables = sign_in_environment(
        {},
        sandbox_identity({}, SessionSettings(workspace_host_id=None, timezone=None)),
        runtime_dir,
        SecretStr("key"),
    )
    result = CliRunner().invoke(
        sign_in_command,
        [
            *options,
            "--runtime-dir",
            str(runtime_dir),
            "--latchkey-binary",
            str(latchkey),
        ],
        env={**dict.fromkeys(sign_in_variables), "LATCHKEY_GATEWAY": None},
    )
    return result.exit_code, result.stdout, result.stderr


def _prepared_with_browser_state(runtime_dir: Path) -> Path:
    _prepared(runtime_dir)
    state = runtime_dir / "latchkey" / "browser_state.json.enc"
    state.parent.mkdir()
    state.write_bytes(b"machine session")
    return state


def test_a_browser_sign_in_prints_the_accounts_and_the_description_as_the_last_line(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    state = _prepared_with_browser_state(runtime_dir)
    latchkey = _fake_latchkey(tmp_path, f"echo '{json.dumps(_SLACK_INFO)}'")

    exit_code, stdout, _ = _sign_in(
        runtime_dir, latchkey, "slack", "--account", "me@example.com"
    )

    assert exit_code == 0
    # The chrome parses the last stdout line to name the account it transfers and how the service signs in.
    assert json.loads(stdout.splitlines()[-1]) == {
        "accounts": {"me@example.com": {"credentialType": "browser"}},
        "services_info": _SLACK_INFO,
    }
    # One description, the sign-in with the machine's session in place and without the picked account (slack
    # detects who logged in), the listing; no `latchkey --version`.
    assert _invocations(tmp_path) == [
        "services info slack --offline [present]",
        "auth browser slack --strict [present]",
        "auth list --offline [present]",
    ]
    assert state.read_bytes() == b"machine session"
    # The sign-in merged minds' part into the config, which the boot script copies into an empty directory here.
    assert "registeredServices" in json.loads(
        (runtime_dir / "latchkey" / "config.json").read_text()
    )


def test_a_fresh_sign_in_starts_without_the_machines_session(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    state = _prepared_with_browser_state(runtime_dir)
    latchkey = _fake_latchkey(tmp_path, f"echo '{json.dumps(_SLACK_INFO)}'")

    exit_code, _, _ = _sign_in(runtime_dir, latchkey, "slack", "--fresh")

    assert exit_code == 0
    assert "auth browser slack --strict [absent]" in _invocations(tmp_path)
    assert not state.exists()


def test_a_service_that_takes_typed_credentials_exits_4_with_its_description_and_launches_nothing(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    state = _prepared_with_browser_state(runtime_dir)
    latchkey = _fake_latchkey(tmp_path, f"echo '{json.dumps(_AWS_INFO)}'")

    exit_code, stdout, _ = _sign_in(runtime_dir, latchkey, "aws", "--fresh")

    assert exit_code == EXIT_TYPED_CREDENTIALS_NEEDED
    assert json.loads(stdout.splitlines()[-1]) == {"services_info": _AWS_INFO}
    assert _invocations(tmp_path) == ["services info aws --offline [present]"]
    assert state.exists()


def test_a_service_latchkey_cannot_describe_signs_in_through_the_browser(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    _prepared(runtime_dir)
    latchkey = _fake_latchkey(tmp_path, "echo 'Error: Failed to decrypt.' >&2; exit 1")

    exit_code, stdout, stderr = _sign_in(runtime_dir, latchkey, "slack")

    assert exit_code == 0
    assert json.loads(stdout.splitlines()[-1])["services_info"] == {}
    assert "describing slack failed: Error: Failed to decrypt." in stderr
    assert "auth browser slack --strict [absent]" in _invocations(tmp_path)


def test_a_refused_browser_sign_in_exits_1_with_latchkeys_reason_and_prints_nothing(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    _prepared(runtime_dir)
    latchkey = _fake_latchkey(
        tmp_path, f"echo '{json.dumps(_SLACK_INFO)}'", auth_browser_exit=1
    )

    exit_code, stdout, stderr = _sign_in(runtime_dir, latchkey, "slack")

    assert exit_code == EXIT_REFUSED
    assert stdout == ""
    assert stderr.strip().splitlines()[-1] == "Error: Login cancelled by the user"


def test_a_failed_listing_after_a_stored_credential_is_a_refusal_with_its_reason(
    tmp_path: Path,
) -> None:
    # Exit 1 is what the chrome retries in the same sandbox, where the credential is already stored.
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    _prepared(runtime_dir)
    latchkey = _fake_latchkey(
        tmp_path, f"echo '{json.dumps(_SLACK_INFO)}'", auth_list="not json"
    )

    exit_code, stdout, stderr = _sign_in(runtime_dir, latchkey, "slack")

    assert exit_code == EXIT_REFUSED
    assert stdout == ""
    assert "listing the stored accounts printed no JSON" in stderr


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


def test_prepare_without_describe_installs_the_session_and_prints_nothing(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    session_file = runtime_dir / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "latchkey_key": _KEY,
                "browser_state": base64.b64encode(b"machine session").decode("ascii"),
            }
        )
    )
    result = CliRunner().invoke(
        prepare_command,
        [
            "--session-file",
            str(session_file),
            "--runtime-dir",
            str(runtime_dir),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    # Nothing runs latchkey, so the binary it would have run does not matter.
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert (runtime_dir / "latchkey_key").read_text() == _KEY + "\n"
    assert (
        runtime_dir / "latchkey" / "browser_state.json.enc"
    ).read_bytes() == b"machine session"
    assert not session_file.exists()


def test_render_config_writes_the_template_with_the_browser_entry(
    tmp_path: Path,
) -> None:
    output = tmp_path / "opt" / "config.json"
    result = CliRunner().invoke(
        render_config_command,
        [str(output), "--browser-executable", "/opt/auth-sandbox/sign-in-browser.sh"],
    )
    assert result.exit_code == 0, result.output
    config = json.loads(output.read_text())
    assert config["browser"]["executablePath"] == "/opt/auth-sandbox/sign-in-browser.sh"
    assert config["browser"]["source"] == "system"
