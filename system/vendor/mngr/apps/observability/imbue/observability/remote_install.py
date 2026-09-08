"""Installing an observability instance's software + config onto a provisioned host over SSH.

The instance boots from ``deploy_assets/cloud-init.yaml`` (packages + the
openobserve systemd unit); this module does everything version- or
config-shaped so a change never needs a reimage: the pinned OpenObserve
binary, the rendered openobserve/caddy/nftables artifacts, the origin TLS
material, the self-monitoring collector, and the unit restarts.
"""

import shlex
from pathlib import Path
from typing import Final

from pydantic import AnyHttpUrl
from pydantic import SecretStr
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.observability.collector_install import render_collector_install_script
from imbue.observability.config_render import CADDYFILE_PATH
from imbue.observability.config_render import NFTABLES_CONF_PATH
from imbue.observability.config_render import OPENOBSERVE_DATA_DIR
from imbue.observability.config_render import OPENOBSERVE_ENV_FILE_PATH
from imbue.observability.config_render import ORIGIN_CERTIFICATE_PATH
from imbue.observability.config_render import ORIGIN_PRIVATE_KEY_PATH
from imbue.observability.config_render import render_all_instance_artifacts
from imbue.observability.data_types import CollectorInstallConfig
from imbue.observability.data_types import ObservabilityInstanceConfig
from imbue.observability.errors import ObservabilityError
from imbue.observability.openobserve_api import build_basic_authorization_header
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import OPENOBSERVE_HTTP_PORT

# Pinned OpenObserve release installed on the instance host (bump deliberately
# via a replace-not-upgrade instance rollover). OpenObserve publishes no
# checksums, so these sha256s were computed from the downloaded tarballs at
# pin time -- they still guarantee every future install byte-matches what was
# reviewed then.
OPENOBSERVE_VERSION: Final[str] = "v0.92.2"
_OPENOBSERVE_SHA256_BY_GOARCH: Final[dict[str, str]] = {
    "amd64": "2b9d35034a6810a6a2043447055cfa493f9302c0402f5a83728efc9f848b68a9",
    "arm64": "efa8d4593a99dbf9d94e26d854c2e7a789e03f7b89eff6c8882b973d09268dec",
}
_OPENOBSERVE_VERSION_STAMP_PATH: Final[str] = "/usr/local/share/openobserve.version"

_SSH_TIMEOUT_SECONDS: Final[float] = 600.0
_SSH_BASE_OPTIONS: Final[tuple[str, ...]] = ("-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes")

# How long a fresh instance gets for sshd to start answering. OVH reports an
# instance ACTIVE before its first boot finishes, so the provisioning recipe's
# immediate deploy can beat sshd to the port (observed live on the production
# bring-up: "Connection refused" seconds after ACTIVE).
_SSH_READY_TIMEOUT_SECONDS: Final[float] = 300.0

# Where each rendered artifact lands on the instance host (staged via /tmp;
# the install script sudo-moves them into place -- the SSH user is not root).
REMOTE_STAGING_DIR: Final[str] = "/tmp/observability-staging"
REMOTE_ARTIFACT_PATHS: Final[dict[str, str]] = {
    "openobserve.env": OPENOBSERVE_ENV_FILE_PATH,
    "Caddyfile": CADDYFILE_PATH,
    "origin.pem": ORIGIN_CERTIFICATE_PATH,
    "origin.key": ORIGIN_PRIVATE_KEY_PATH,
    "nftables.conf": NFTABLES_CONF_PATH,
}


class ObservabilityDeployError(ObservabilityError):
    """Raised when installing software or config on an instance host fails."""


@pure
def self_collector_config(config: ObservabilityInstanceConfig) -> CollectorInstallConfig:
    """The instance host's own collector: pushes to loopback OpenObserve as the root user.

    Loopback skips the Cloudflare round-trip and the public gate entirely; the
    root credential is acceptable here because the exact same material already
    sits in the openobserve.env on the same disk.
    """
    return CollectorInstallConfig(
        role=CollectorRole.INSTANCE,
        tier=config.tier,
        ingest_url=AnyHttpUrl(f"http://127.0.0.1:{OPENOBSERVE_HTTP_PORT}"),
        ingest_authorization_header_value=SecretStr(
            build_basic_authorization_header(config.root_user_email, config.root_user_password.get_secret_value())
        ),
    )


@pure
def render_instance_install_script() -> str:
    """The idempotent remote install script (runs under sudo): pinned OpenObserve, staged config, service restarts.

    Renders ``OPENOBSERVE_VERSION`` directly (no version parameter): the
    embedded checksums are the pins of exactly that version, so version and
    checksums can only ever move together.

    File modes: the env file embeds every instance secret (root password, DSN,
    R2 keys) and is read by systemd as root, so it stays root-only; the origin
    TLS material is group-readable by caddy (which runs as the ``caddy`` user
    the Debian package creates).
    """
    openobserve_version = OPENOBSERVE_VERSION
    amd64_sha256 = _OPENOBSERVE_SHA256_BY_GOARCH["amd64"]
    arm64_sha256 = _OPENOBSERVE_SHA256_BY_GOARCH["arm64"]
    return f"""\
set -euo pipefail

# sshd accepts connections before first boot finishes, and the one-shot
# provisioning recipe deploys immediately -- everything below needs the
# packages (caddy and its service group, nftables, curl) that cloud-init's
# first boot installs. Tolerate a degraded/absent cloud-init (`|| true`):
# any dependency it failed to provide still fails loudly right after.
cloud-init status --wait 2>/dev/null || true

openobserve_arch="$(uname -m)"
case "${{openobserve_arch}}" in
    x86_64) openobserve_goarch="amd64"; openobserve_sha256="{amd64_sha256}" ;;
    aarch64) openobserve_goarch="arm64"; openobserve_sha256="{arm64_sha256}" ;;
    *) echo "Unsupported architecture for openobserve: ${{openobserve_arch}}" >&2; exit 1 ;;
esac

installed_version="$(cat {_OPENOBSERVE_VERSION_STAMP_PATH} 2>/dev/null || true)"
if [ ! -x /usr/local/bin/openobserve ] || [ "${{installed_version}}" != "{openobserve_version}" ]; then
    # Download and verify inside a private root-owned dir: fixed /tmp paths
    # would let a local user pre-plant a symlink or swap the binary between
    # the sha256 check and the mv.
    openobserve_tmp="$(mktemp -d)"
    curl -fsSL "https://downloads.openobserve.ai/releases/openobserve/{openobserve_version}/openobserve-{openobserve_version}-linux-${{openobserve_goarch}}.tar.gz" -o "${{openobserve_tmp}}/openobserve.tar.gz"
    echo "${{openobserve_sha256}}  ${{openobserve_tmp}}/openobserve.tar.gz" | sha256sum -c -
    tar -xzf "${{openobserve_tmp}}/openobserve.tar.gz" -C "${{openobserve_tmp}}" openobserve
    mv -f "${{openobserve_tmp}}/openobserve" /usr/local/bin/openobserve
    chmod 0755 /usr/local/bin/openobserve
    mkdir -p "$(dirname {_OPENOBSERVE_VERSION_STAMP_PATH})"
    echo "{openobserve_version}" > {_OPENOBSERVE_VERSION_STAMP_PATH}
    rm -rf "${{openobserve_tmp}}"
fi

if ! id -u openobserve >/dev/null 2>&1; then
    useradd --system --home-dir {OPENOBSERVE_DATA_DIR} --shell /usr/sbin/nologin openobserve
fi
install -d -m 0750 -o openobserve -g openobserve {OPENOBSERVE_DATA_DIR}
mkdir -p /etc/openobserve /etc/caddy

install -m 0600 -o root -g root "{REMOTE_STAGING_DIR}/openobserve.env" "{OPENOBSERVE_ENV_FILE_PATH}"
install -m 0644 -o root -g root "{REMOTE_STAGING_DIR}/Caddyfile" "{CADDYFILE_PATH}"
install -m 0640 -o root -g caddy "{REMOTE_STAGING_DIR}/origin.pem" "{ORIGIN_CERTIFICATE_PATH}"
install -m 0640 -o root -g caddy "{REMOTE_STAGING_DIR}/origin.key" "{ORIGIN_PRIVATE_KEY_PATH}"
install -m 0644 -o root -g root "{REMOTE_STAGING_DIR}/nftables.conf" "{NFTABLES_CONF_PATH}"
rm -rf "{REMOTE_STAGING_DIR}"

systemctl daemon-reload
systemctl enable nftables caddy openobserve
systemctl restart nftables
systemctl restart openobserve
systemctl restart caddy
"""


def run_ssh_command(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, remote_command: str) -> None:
    try:
        concurrency_group.run_process_to_completion(
            ["ssh", *_SSH_BASE_OPTIONS, f"{ssh_user}@{host}", remote_command],
            timeout=_SSH_TIMEOUT_SECONDS,
            name=f"observability-ssh-{host}",
        )
    except ProcessError as exc:
        raise ObservabilityDeployError(f"ssh to {host} failed: {exc}") from exc


def scp_files(
    concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, local_paths: list[Path], remote_dir: str
) -> None:
    """Copy all files in one scp invocation (one SSH connection); basenames are preserved."""
    try:
        concurrency_group.run_process_to_completion(
            ["scp", *_SSH_BASE_OPTIONS, *(str(path) for path in local_paths), f"{ssh_user}@{host}:{remote_dir}/"],
            timeout=_SSH_TIMEOUT_SECONDS,
            name=f"observability-scp-{host}",
        )
    except ProcessError as exc:
        local_names = ", ".join(path.name for path in local_paths)
        raise ObservabilityDeployError(f"scp of {local_names} to {host} failed: {exc}") from exc


def run_ssh_command_capturing_output(
    concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, remote_command: str
) -> str:
    """Run one remote command over SSH and return its stdout (e.g. a minted token)."""
    try:
        result = concurrency_group.run_process_to_completion(
            ["ssh", *_SSH_BASE_OPTIONS, f"{ssh_user}@{host}", remote_command],
            timeout=_SSH_TIMEOUT_SECONDS,
            name=f"observability-ssh-capture-{host}",
        )
    except ProcessError as exc:
        raise ObservabilityDeployError(f"ssh to {host} failed: {exc}") from exc
    return result.stdout


def run_root_script_over_ssh(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str, script: str) -> None:
    """Run one rendered script under sudo on the host (used for installs on hosts we can plainly SSH)."""
    run_ssh_command(concurrency_group, host, ssh_user, f"sudo bash -c {shlex.quote(script)}")


@retry(
    retry=retry_if_exception_type(ObservabilityDeployError),
    stop=stop_after_delay(_SSH_READY_TIMEOUT_SECONDS),
    wait=wait_fixed(5.0),
    reraise=True,
)
def wait_for_ssh_ready(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str) -> None:
    """Poll until the fresh instance's sshd answers (OVH reports ACTIVE before first boot finishes)."""
    run_ssh_command(concurrency_group, host, ssh_user, "true")


def deploy_instance(
    concurrency_group: ConcurrencyGroup,
    host: str,
    ssh_user: str,
    config: ObservabilityInstanceConfig,
    work_dir: Path,
) -> None:
    """Render config locally, stage everything onto the host, and sudo-install + (re)start the services.

    Also installs the instance's own self-monitoring collector (loopback
    ingest), so a freshly deployed instance is watching its own host from the
    first boot.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    # Every artifact carries secrets (the env file, the origin key), and the
    # default work dir is a fixed path under /tmp, so keep the scratch dir and
    # every staged artifact owner-only. The chmod also fails loudly if another
    # local user pre-created the fixed path.
    work_dir.chmod(0o700)
    artifacts = render_all_instance_artifacts(config)
    for name, content in artifacts.items():
        artifact_path = work_dir / name
        artifact_path.write_text(content)
        artifact_path.chmod(0o600)

    # Stage into /tmp (the SSH user cannot write /etc directly), then run the
    # install script under sudo to move files into place and restart services.
    # No -p on the mkdir: the rm guarantees non-existence, so the mkdir fails
    # loudly if another local user races the fixed /tmp path back into
    # existence (a -p would silently adopt their directory).
    try:
        wait_for_ssh_ready(concurrency_group, host, ssh_user)
        run_ssh_command(
            concurrency_group, host, ssh_user, f"rm -rf {REMOTE_STAGING_DIR} && mkdir -m 700 {REMOTE_STAGING_DIR}"
        )
        scp_files(concurrency_group, host, ssh_user, [work_dir / name for name in artifacts], REMOTE_STAGING_DIR)
        run_root_script_over_ssh(concurrency_group, host, ssh_user, render_instance_install_script())
        run_root_script_over_ssh(
            concurrency_group, host, ssh_user, render_collector_install_script(self_collector_config(config))
        )
    finally:
        # The rendered artifacts are secret material with no value once staged
        # (the renderers are pure, so a preview or retry just re-renders); do
        # not leave them behind on the operator machine's disk.
        for name in artifacts:
            (work_dir / name).unlink(missing_ok=True)
