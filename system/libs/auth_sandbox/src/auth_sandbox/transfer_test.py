from pathlib import Path

import pytest
from pydantic import SecretStr

from auth_sandbox.errors import LatchkeyCommandError, MissingMaterialError
from auth_sandbox.mock_latchkey_test import FakeReencryptRunner
from auth_sandbox.runtime_files import SandboxPaths, read_key
from auth_sandbox.transfer import (
    export_credentials,
    import_browser_state,
    reencrypt_argv,
    reencrypt_environment,
)

_BASE_ENV = {
    "PATH": "/usr/bin",
    "LATCHKEY_GATEWAY": "http://host.docker.internal:1989",
    "HOME": "/home/user",
}


def _paths(
    tmp_path: Path, *, is_transfer_key: bool = True, is_sandbox_key: bool = True
) -> SandboxPaths:
    paths = SandboxPaths(runtime_dir=tmp_path / "run")
    paths.runtime_dir.mkdir(parents=True)
    paths.incoming_dir.mkdir()
    paths.latchkey_directory.mkdir()
    if is_transfer_key:
        paths.transfer_key_path.write_text("transfer-key\n")
    if is_sandbox_key:
        paths.latchkey_key_path.write_text("sandbox-key\n")
    return paths


def test_read_key_strips_the_newline_and_names_only_the_path_when_absent(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "latchkey_key"
    key_path.write_text("abc==\n")
    assert read_key(key_path).get_secret_value() == "abc=="
    key_path.write_text("")
    with pytest.raises(MissingMaterialError, match="empty"):
        read_key(key_path)
    with pytest.raises(MissingMaterialError, match="no key at"):
        read_key(tmp_path / "absent")


def test_reencrypt_argv_puts_the_global_account_option_first_and_no_key_anywhere() -> (
    None
):
    assert reencrypt_argv(
        "latchkey", Path("/run/auth-sandbox/export"), ("slack",), True, "me@example.com"
    ) == (
        "latchkey",
        "--account",
        "me@example.com",
        "auth",
        "re-encrypt",
        "/run/auth-sandbox/export",
        "--services",
        "slack",
        "--browser-state",
    )
    assert reencrypt_argv("/opt/lk", Path("/dst"), (), True, None) == (
        "/opt/lk",
        "auth",
        "re-encrypt",
        "/dst",
        "--browser-state",
    )
    assert reencrypt_argv(
        "latchkey", Path("/dst"), ("github", "slack"), False, None
    ) == (
        "latchkey",
        "auth",
        "re-encrypt",
        "/dst",
        "--services",
        "github",
        "slack",
    )


def test_reencrypt_environment_names_the_source_store_and_key_and_drops_the_gateway() -> (
    None
):
    environment = reencrypt_environment(
        _BASE_ENV, Path("/run/auth-sandbox/incoming"), SecretStr("src-key")
    )
    assert environment == {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LATCHKEY_DIRECTORY": "/run/auth-sandbox/incoming",
        "LATCHKEY_ENCRYPTION_KEY": "src-key",
        "LATCHKEY_DISABLE_COUNTING": "1",
    }


def test_import_browser_state_reencrypts_incoming_under_the_transfer_key_into_the_sandbox_store(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.incoming_browser_state_path.write_bytes(b"incoming")
    runner = FakeReencryptRunner()

    written = import_browser_state(runner, paths, "latchkey", _BASE_ENV)

    assert written == paths.latchkey_browser_state_path
    assert written.read_bytes() == b"state:sandbox-key\n"
    (run,) = runner.runs
    assert run.argv == (
        "latchkey",
        "auth",
        "re-encrypt",
        str(paths.latchkey_directory),
        "--browser-state",
    )
    assert run.environment["LATCHKEY_DIRECTORY"] == str(paths.incoming_dir)
    assert run.environment["LATCHKEY_ENCRYPTION_KEY"] == "transfer-key"
    assert "LATCHKEY_GATEWAY" not in run.environment
    assert run.stdin_text == "sandbox-key\n"
    assert run.working_directory == paths.runtime_dir


def test_import_browser_state_needs_both_keys_and_the_incoming_state(
    tmp_path: Path,
) -> None:
    runner = FakeReencryptRunner()
    with pytest.raises(MissingMaterialError, match="no incoming browser state"):
        import_browser_state(runner, _paths(tmp_path), "latchkey", _BASE_ENV)
    without_transfer = _paths(tmp_path / "a", is_transfer_key=False)
    without_transfer.incoming_browser_state_path.write_bytes(b"x")
    with pytest.raises(MissingMaterialError, match="transfer_key"):
        import_browser_state(runner, without_transfer, "latchkey", _BASE_ENV)
    without_sandbox = _paths(tmp_path / "b", is_sandbox_key=False)
    without_sandbox.incoming_browser_state_path.write_bytes(b"x")
    with pytest.raises(MissingMaterialError, match="latchkey_key"):
        import_browser_state(runner, without_sandbox, "latchkey", _BASE_ENV)
    assert runner.runs == []


def test_import_browser_state_reports_latchkeys_refusal_without_the_keys(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.incoming_browser_state_path.write_bytes(b"x")
    runner = FakeReencryptRunner(
        returncode=1,
        stderr="Error: Failed to decrypt. The encryption key may have changed.",
    )
    with pytest.raises(LatchkeyCommandError) as excinfo:
        import_browser_state(runner, paths, "latchkey", _BASE_ENV)
    assert "importing the browser state failed" in str(excinfo.value)
    assert "encryption key may have changed" in str(excinfo.value)
    assert "transfer-key" not in str(excinfo.value)
    assert "sandbox-key" not in str(excinfo.value)


def test_export_credentials_writes_the_bundle_under_the_transfer_key_with_the_browser_state(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.latchkey_browser_state_path.write_bytes(b"signed-in")
    paths.export_dir.mkdir()
    (paths.export_dir / "stale").write_text("from an earlier export")
    runner = FakeReencryptRunner()

    bundle = export_credentials(
        runner, paths, "latchkey", _BASE_ENV, "slack", "me@example.com"
    )

    assert bundle.credentials == paths.exported_credentials_path
    assert bundle.data_format_version == paths.exported_data_format_version_path
    assert bundle.browser_state == paths.exported_browser_state_path
    assert bundle.credentials.read_bytes() == b"enc:transfer-key\n"
    assert bundle.data_format_version.read_text() == "4"
    assert not (paths.export_dir / "stale").exists()
    (run,) = runner.runs
    assert run.argv == (
        "latchkey",
        "--account",
        "me@example.com",
        "auth",
        "re-encrypt",
        str(paths.export_dir),
        "--services",
        "slack",
        "--browser-state",
    )
    assert run.environment["LATCHKEY_DIRECTORY"] == str(paths.latchkey_directory)
    assert run.environment["LATCHKEY_ENCRYPTION_KEY"] == "sandbox-key"
    assert run.stdin_text == "transfer-key\n"


def test_export_credentials_leaves_the_browser_state_out_when_the_sandbox_holds_none(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    runner = FakeReencryptRunner(has_browser_state=False)

    bundle = export_credentials(runner, paths, "latchkey", _BASE_ENV, "aws", None)

    assert bundle.browser_state is None
    (run,) = runner.runs
    assert run.argv == (
        "latchkey",
        "auth",
        "re-encrypt",
        str(paths.export_dir),
        "--services",
        "aws",
    )


def test_export_credentials_refuses_a_run_that_wrote_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(
        LatchkeyCommandError, match="exporting the slack credentials failed"
    ):
        export_credentials(
            FakeReencryptRunner(
                returncode=1, stderr="Error: No stored credentials found to re-encrypt."
            ),
            paths,
            "latchkey",
            _BASE_ENV,
            "slack",
            None,
        )
    with pytest.raises(MissingMaterialError, match="transfer_key"):
        export_credentials(
            FakeReencryptRunner(),
            _paths(tmp_path / "b", is_transfer_key=False),
            "latchkey",
            _BASE_ENV,
            "slack",
            None,
        )
