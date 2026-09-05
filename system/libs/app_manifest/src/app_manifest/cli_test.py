from pathlib import Path

from click.testing import CliRunner

from app_manifest.cli import app_manifest_cli

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


def test_validate_manifest_accepts_a_valid_manifest(tmp_path: Path) -> None:
    (tmp_path / "icon.svg").write_text(_ICON)
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n')

    result = CliRunner().invoke(app_manifest_cli, ["validate-manifest", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ok: news (News)"


def test_validate_manifest_reports_the_failing_field_and_exits_non_zero(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\nbogus = 1\n')

    result = CliRunner().invoke(app_manifest_cli, ["validate-manifest", str(manifest_path)])

    assert result.exit_code != 0
    assert "bogus" in result.output
