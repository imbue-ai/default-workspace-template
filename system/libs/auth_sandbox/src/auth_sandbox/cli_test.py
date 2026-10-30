import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from auth_sandbox.cli import (
    EXIT_MISSING_MATERIAL,
    export_command,
    import_browser_state_command,
    render_config_command,
    set_credentials_command,
    sign_in_command,
)


def test_sign_in_without_a_sandbox_key_names_the_missing_file_and_runs_nothing(
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
    (tmp_path / "latchkey_key").write_text("key\n")
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
    assert "/nonexistent/latchkey" in result.output


def test_set_credentials_needs_an_account_and_a_values_file(tmp_path: Path) -> None:
    runner = CliRunner()
    # click's usage-error code, which the missing-material code stays apart from.
    usage_error = runner.invoke(
        set_credentials_command, ["aws", "--values-file", str(tmp_path / "v.json")]
    ).exit_code
    assert usage_error == 2
    assert usage_error != EXIT_MISSING_MATERIAL
    (tmp_path / "v.json").write_text("not json")
    (tmp_path / "latchkey_key").write_text("key\n")
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


def test_import_and_export_report_missing_materials_before_touching_latchkey(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    imported = runner.invoke(
        import_browser_state_command,
        ["--runtime-dir", str(tmp_path), "--latchkey-binary", "/nonexistent/latchkey"],
    )
    assert imported.exit_code == EXIT_MISSING_MATERIAL
    assert "transfer_key" in imported.output
    exported = runner.invoke(
        export_command,
        [
            "slack",
            "--runtime-dir",
            str(tmp_path),
            "--latchkey-binary",
            "/nonexistent/latchkey",
        ],
    )
    assert exported.exit_code == EXIT_MISSING_MATERIAL
    assert "transfer_key" in exported.output


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
