import tomllib
from pathlib import Path

from click.testing import CliRunner

from imbue.share_relay.cli import main


def test_render_writes_all_three_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "us1"
    result = CliRunner().invoke(
        main,
        [
            "render",
            "--relay-id",
            "relay-" + "e" * 16,
            "--region",
            "us1",
            "--content-domain",
            "imbueminds.com",
            "--plugin-auth-url",
            "https://connector.example.com/frps/auth",
            "--out-dir",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    frps = (out_dir / "frps.toml").read_text()
    # The rendered frps.toml must be valid TOML and be SNI-passthrough.
    parsed = tomllib.loads(frps)
    assert parsed["vhostHTTPSPort"] == 443
    assert parsed["httpPlugins"][0]["ops"] == ["Login", "NewProxy", "Ping"]
    assert "443" in (out_dir / "nftables.conf").read_text()
    assert "redir https://" in (out_dir / "port80.Caddyfile").read_text()


def test_render_rejects_a_non_dns_region(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "render",
            "--relay-id",
            "relay-" + "e" * 16,
            "--region",
            "US_1",
            "--content-domain",
            "imbueminds.com",
            "--plugin-auth-url",
            "https://connector.example.com/frps/auth",
            "--out-dir",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert not (tmp_path / "out").exists()
