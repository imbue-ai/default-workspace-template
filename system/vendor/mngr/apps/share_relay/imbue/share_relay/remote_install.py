"""Installing a relay's software + config onto a provisioned host over SSH.

The instance boots from ``deploy_assets/cloud-init.yaml`` (packages + systemd units);
this module does everything version- or config-shaped so a change never needs
a reimage: the pinned, checksummed frps binary, the standalone healthcheck
script, the rendered frps/nftables/Caddyfile artifacts, and the unit restarts.
"""

import shlex
from pathlib import Path
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.share_relay.config_render import render_all_artifacts
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.errors import ShareRelayError

# Pinned frp release installed on every relay (bump deliberately; the sha256s
# are for the linux tarballs of this exact version).
FRP_VERSION: Final[str] = "0.70.1"
_FRP_SHA256_BY_GOARCH: Final[dict[str, str]] = {
    "amd64": "333da23d1b9009d7c01638e9ba38cf4600f7d37d393f854e96ee1396adefa9a6",
    "arm64": "3990f396a9a490ee7f0e5f355287750ed41520064ed999eab443b5e9a78d773d",
}


def pinned_frp_release(goarch: str) -> tuple[str, str]:
    """The pinned frp release download URL and its sha256 for one Go architecture."""
    sha256 = _FRP_SHA256_BY_GOARCH[goarch]
    url = f"https://github.com/fatedier/frp/releases/download/v{FRP_VERSION}/frp_{FRP_VERSION}_linux_{goarch}.tar.gz"
    return (url, sha256)


_SSH_TIMEOUT_SECONDS: Final[float] = 300.0
_SSH_BASE_OPTIONS: Final[tuple[str, ...]] = ("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")

# Where each rendered artifact lands on the relay host (staged via /tmp; the
# install script sudo-moves them into place -- the SSH user is not root).
REMOTE_STAGING_DIR: Final[str] = "/tmp/share-relay-staging"
REMOTE_ARTIFACT_PATHS: Final[dict[str, str]] = {
    "frps.toml": "/etc/frp/frps.toml",
    "nftables.conf": "/etc/nftables.conf",
    "port80.Caddyfile": "/etc/caddy/Caddyfile",
    "healthcheck_standalone.py": "/usr/local/bin/share-relay-healthcheck.py",
}


class RelayDeployError(ShareRelayError):
    """Raised when installing software or config on a relay host fails."""


@pure
def render_relay_install_script(frp_version: str) -> str:
    """The idempotent remote install script (runs under sudo): pinned frps, staged config, service restarts."""
    amd64_sha256 = _FRP_SHA256_BY_GOARCH["amd64"]
    arm64_sha256 = _FRP_SHA256_BY_GOARCH["arm64"]
    # frps.toml embeds the connector plugin-auth secret (in the plugin addr's
    # userinfo), so it is installed root-only (0640) rather than the
    # world-readable 0644 the other artifacts get.
    move_lines = "\n".join(
        f'install -m {"0640" if name == "frps.toml" else "0644"} "{REMOTE_STAGING_DIR}/{name}" "{destination}"'
        for name, destination in REMOTE_ARTIFACT_PATHS.items()
    )
    return f"""\
set -euo pipefail

frp_arch="$(uname -m)"
case "${{frp_arch}}" in
    x86_64) frp_goarch="amd64"; frp_sha256="{amd64_sha256}" ;;
    aarch64) frp_goarch="arm64"; frp_sha256="{arm64_sha256}" ;;
    *) echo "Unsupported architecture for frp: ${{frp_arch}}" >&2; exit 1 ;;
esac
if [ ! -x /usr/local/bin/frps ] || ! /usr/local/bin/frps --version | grep -q "^{frp_version}$"; then
    # Download and extract inside a private root-owned dir: fixed /tmp paths
    # would let a local user pre-plant a symlink (curl -o clobbers what it
    # points at as root) or swap the binary between the sha256 check and the mv.
    frp_tmp="$(mktemp -d)"
    curl -fsSL "https://github.com/fatedier/frp/releases/download/v{frp_version}/frp_{frp_version}_linux_${{frp_goarch}}.tar.gz" -o "${{frp_tmp}}/frp.tar.gz"
    echo "${{frp_sha256}}  ${{frp_tmp}}/frp.tar.gz" | sha256sum -c -
    tar -xzf "${{frp_tmp}}/frp.tar.gz" -C "${{frp_tmp}}" "frp_{frp_version}_linux_${{frp_goarch}}/frps"
    mv -f "${{frp_tmp}}/frp_{frp_version}_linux_${{frp_goarch}}/frps" /usr/local/bin/frps
    chmod 0755 /usr/local/bin/frps
    rm -rf "${{frp_tmp}}"
fi
mkdir -p /etc/frp /etc/caddy
{move_lines}
rm -rf "{REMOTE_STAGING_DIR}"
systemctl daemon-reload
systemctl enable frps share-relay-healthcheck nftables caddy
systemctl restart nftables
systemctl restart caddy
systemctl restart frps
systemctl restart share-relay-healthcheck
"""


def _run_ssh(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, remote_command: str) -> None:
    try:
        concurrency_group.run_process_to_completion(
            ["ssh", *_SSH_BASE_OPTIONS, f"{ssh_user}@{host}", remote_command],
            timeout=_SSH_TIMEOUT_SECONDS,
            name=f"relay-ssh-{host}",
        )
    except ProcessError as exc:
        raise RelayDeployError(f"ssh to {host} failed: {exc}") from exc


def _scp_file(
    concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, local_path: Path, remote_path: str
) -> None:
    try:
        concurrency_group.run_process_to_completion(
            ["scp", *_SSH_BASE_OPTIONS, str(local_path), f"{ssh_user}@{host}:{remote_path}"],
            timeout=_SSH_TIMEOUT_SECONDS,
            name=f"relay-scp-{host}",
        )
    except ProcessError as exc:
        raise RelayDeployError(f"scp {local_path} to {host} failed: {exc}") from exc


def deploy_relay(
    concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, config: RelayConfiguration, work_dir: Path
) -> None:
    """Render config locally, stage everything onto the host, and sudo-install + (re)start the services."""
    work_dir.mkdir(parents=True, exist_ok=True)
    # frps.toml embeds the connector auth secret, and the default work dir is a
    # fixed path under /tmp, so keep the scratch dir and every staged artifact
    # owner-only. The chmod also fails loudly if another local user pre-created
    # the fixed path.
    work_dir.chmod(0o700)
    healthcheck_script = Path(__file__).parent / "deploy_assets" / "healthcheck_standalone.py"
    artifacts = {
        **render_all_artifacts(config),
        "healthcheck_standalone.py": healthcheck_script.read_text(),
    }
    for name, content in artifacts.items():
        artifact_path = work_dir / name
        artifact_path.write_text(content)
        artifact_path.chmod(0o600)

    # Stage into /tmp (the SSH user cannot write /etc directly), then run the
    # install script under sudo to move files into place and restart services.
    # The staging dir is owner-only for the same secret-bearing reason as the
    # local work dir, and is removed once the install has copied files into
    # their root-owned destinations.
    # No -p on the mkdir: the rm guarantees non-existence, so the mkdir fails
    # loudly if another local user races the fixed /tmp path back into
    # existence (a -p would silently adopt their directory).
    _run_ssh(concurrency_group, host, ssh_user, f"rm -rf {REMOTE_STAGING_DIR} && mkdir -m 700 {REMOTE_STAGING_DIR}")
    for name in artifacts:
        _scp_file(concurrency_group, host, ssh_user, work_dir / name, f"{REMOTE_STAGING_DIR}/{name}")
    _run_ssh(
        concurrency_group, host, ssh_user, f"sudo bash -c {shlex.quote(render_relay_install_script(FRP_VERSION))}"
    )
    _run_ssh(concurrency_group, host, ssh_user, f"rm -rf {REMOTE_STAGING_DIR}")
