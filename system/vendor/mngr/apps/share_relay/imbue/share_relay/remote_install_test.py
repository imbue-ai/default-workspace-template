import os
import stat
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.primitives import ContentDomain
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayId
from imbue.share_relay.remote_install import FRP_VERSION
from imbue.share_relay.remote_install import REMOTE_ARTIFACT_PATHS
from imbue.share_relay.remote_install import REMOTE_STAGING_DIR
from imbue.share_relay.remote_install import RelayDeployError
from imbue.share_relay.remote_install import deploy_relay
from imbue.share_relay.remote_install import render_relay_install_script


def test_install_script_pins_and_verifies_the_frp_release() -> None:
    script = render_relay_install_script(FRP_VERSION)

    assert script.startswith("set -euo pipefail")
    assert f"releases/download/v{FRP_VERSION}/" in script
    assert "sha256sum -c -" in script
    assert 'x86_64) frp_goarch="amd64"' in script
    assert 'aarch64) frp_goarch="arm64"' in script
    # The download/extract happens inside a private mktemp dir, never at a
    # fixed predictable /tmp path a local user could pre-plant.
    assert 'frp_tmp="$(mktemp -d)"' in script
    assert "/tmp/frp.tar.gz" not in script


def test_install_script_moves_every_staged_artifact_into_place() -> None:
    script = render_relay_install_script(FRP_VERSION)

    for name, destination in REMOTE_ARTIFACT_PATHS.items():
        mode = "0640" if name == "frps.toml" else "0644"
        assert f'install -m {mode} "{REMOTE_STAGING_DIR}/{name}" "{destination}"' in script
    # The secret-bearing frps.toml is installed root-only.
    assert 'install -m 0640 "' in script and "frps.toml" in script
    assert "systemctl enable frps share-relay-healthcheck nftables caddy" in script
    assert "systemctl restart frps" in script


def test_standalone_healthcheck_script_is_stdlib_only() -> None:
    script_path = Path(__file__).parent / "deploy_assets" / "healthcheck_standalone.py"
    text = script_path.read_text()
    import_lines = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    allowed_modules = {"os", "socket", "http.server"}
    for line in import_lines:
        module = line.split()[1]
        assert module in allowed_modules, f"non-stdlib-minimal import in standalone healthcheck: {line}"
    assert "/healthz" in text


def _deploy_config() -> RelayConfiguration:
    return RelayConfiguration(
        relay_id=RelayId("relay-" + "e" * 16),
        region=RegionCode("us1"),
        content_domain=ContentDomain("imbueminds.com"),
        plugin_auth_url=AnyHttpUrl("https://connector.example.com/frps/auth"),
        plugin_auth_secret=SecretStr("f0e1d2c3b4a5968788796a5b4c3d2e1f"),
    )


def _write_recording_tool(bin_dir: Path, name: str, log_path: Path, exit_code: int = 0) -> None:
    """A fake ssh/scp that appends its argv to ``log_path`` and exits with ``exit_code``."""
    tool = bin_dir / name
    tool.write_text(f'#!/bin/sh\necho "{name} $@" >> "{log_path}"\nexit {exit_code}\n')
    tool.chmod(0o755)


def test_deploy_relay_stages_artifacts_owner_only_and_installs_over_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full deploy: render locally with tight modes, stage via scp, sudo-install, clean up."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    _write_recording_tool(bin_dir, "ssh", call_log)
    _write_recording_tool(bin_dir, "scp", call_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    work_dir = tmp_path / "work"

    with ConcurrencyGroup(name="deploy-relay-test") as concurrency_group:
        deploy_relay(concurrency_group, "203.0.113.7", "debian", _deploy_config(), work_dir)

    # Local staging is owner-only: the frps.toml artifact embeds the plugin-auth secret.
    assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700
    for name in ("frps.toml", "nftables.conf", "port80.Caddyfile", "healthcheck_standalone.py"):
        artifact = work_dir / name
        assert artifact.exists(), name
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600, name

    calls = call_log.read_text().splitlines()
    ssh_calls = [line for line in calls if line.startswith("ssh ")]
    scp_calls = [line for line in calls if line.startswith("scp ")]
    # Staging dir is recreated fresh (no -p: a raced pre-existing dir must fail
    # loudly), one scp per artifact, then the sudo install, then cleanup.
    assert "rm -rf /tmp/share-relay-staging && mkdir -m 700 /tmp/share-relay-staging" in ssh_calls[0]
    assert len(scp_calls) == 4
    assert all("debian@203.0.113.7:/tmp/share-relay-staging/" in line for line in scp_calls)
    assert any("sudo bash -c " in line for line in ssh_calls)
    assert ssh_calls[-1].endswith("rm -rf /tmp/share-relay-staging")


def test_deploy_relay_wraps_ssh_failures_in_relay_deploy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    _write_recording_tool(bin_dir, "ssh", call_log, exit_code=1)
    _write_recording_tool(bin_dir, "scp", call_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    with ConcurrencyGroup(name="deploy-relay-failure-test") as concurrency_group:
        with pytest.raises(RelayDeployError, match="ssh to 203.0.113.9 failed"):
            deploy_relay(concurrency_group, "203.0.113.9", "debian", _deploy_config(), tmp_path / "work")
