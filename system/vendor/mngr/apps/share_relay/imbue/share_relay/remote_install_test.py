from pathlib import Path

from imbue.share_relay.remote_install import FRP_VERSION
from imbue.share_relay.remote_install import REMOTE_ARTIFACT_PATHS
from imbue.share_relay.remote_install import REMOTE_STAGING_DIR
from imbue.share_relay.remote_install import render_relay_install_script


def test_install_script_pins_and_verifies_the_frp_release() -> None:
    script = render_relay_install_script(FRP_VERSION)

    assert script.startswith("set -euo pipefail")
    assert f"releases/download/v{FRP_VERSION}/" in script
    assert "sha256sum -c -" in script
    assert 'x86_64) frp_goarch="amd64"' in script
    assert 'aarch64) frp_goarch="arm64"' in script


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
    script_path = Path(__file__).parent.parent.parent / "deploy" / "healthcheck_standalone.py"
    text = script_path.read_text()
    import_lines = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    allowed_modules = {"os", "socket", "http.server"}
    for line in import_lines:
        module = line.split()[1]
        assert module in allowed_modules, f"non-stdlib-minimal import in standalone healthcheck: {line}"
    assert "/healthz" in text
