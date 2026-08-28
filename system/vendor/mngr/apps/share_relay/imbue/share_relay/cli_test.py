import tomllib
from pathlib import Path

from click.testing import CliRunner

from imbue.share_relay.cli import main

_PLUGIN_SECRET_ENV = {"FRPS_AUTH_SECRET": "f0e1d2c3b4a5968788796a5b4c3d2e1f"}


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
        env=_PLUGIN_SECRET_ENV,
    )
    assert result.exit_code == 0, result.output
    frps = (out_dir / "frps.toml").read_text()
    # The rendered frps.toml must be valid TOML and be SNI-passthrough.
    parsed = tomllib.loads(frps)
    assert parsed["vhostHTTPSPort"] == 443
    assert parsed["httpPlugins"][0]["ops"] == ["Login", "NewProxy", "Ping"]
    # The env-supplied secret lands in the plugin addr's userinfo, never the path.
    assert (
        parsed["httpPlugins"][0]["addr"] == f"https://{_PLUGIN_SECRET_ENV['FRPS_AUTH_SECRET']}@connector.example.com"
    )
    assert parsed["httpPlugins"][0]["path"] == "/frps/auth/relay-" + "e" * 16
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
        env=_PLUGIN_SECRET_ENV,
    )
    assert result.exit_code != 0
    assert not (tmp_path / "out").exists()


def test_render_requires_the_plugin_secret_env_var(tmp_path: Path) -> None:
    # The secret deliberately travels via the environment, not argv (shell
    # history / ps exposure); a missing value must fail before rendering.
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
            str(tmp_path / "out"),
        ],
        env={"FRPS_AUTH_SECRET": ""},
    )
    assert result.exit_code != 0
    assert "FRPS_AUTH_SECRET" in result.output
    assert not (tmp_path / "out").exists()
