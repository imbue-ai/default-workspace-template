"""Installing a Bugsink instance's software + config onto a provisioned host over SSH.

The instance boots from ``deploy_assets/cloud-init-bugsink.yaml`` (packages +
the bugsink systemd unit); this module does everything version- or
config-shaped so a change never needs a reimage: the hash-locked pip set into
``/opt/bugsink/venv``, the vendored ``bugsink_conf.py`` settings module, the
rendered bugsink.env / Caddyfile / nftables artifacts, the origin TLS
material, and the unit restarts. Unlike OpenObserve (a checksummed release
binary), Bugsink is a pip package: supply-chain pinning comes from the
committed ``bugsink_requirements.txt`` (every transitive ``==``-pinned with
sha256 hashes, compiled under the repo's supply-chain cooldown -- see
``bugsink_requirements.in``) installed with ``--require-hashes``.
"""

import hashlib
from pathlib import Path
from typing import Final

from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.observability.bugsink_render import BUGSINK_ENV_FILE_PATH
from imbue.observability.bugsink_render import render_all_bugsink_artifacts
from imbue.observability.config_render import CADDYFILE_PATH
from imbue.observability.config_render import NFTABLES_CONF_PATH
from imbue.observability.config_render import ORIGIN_CERTIFICATE_PATH
from imbue.observability.config_render import ORIGIN_PRIVATE_KEY_PATH
from imbue.observability.data_types import BugsinkInstanceConfig
from imbue.observability.errors import ObservabilityError
from imbue.observability.primitives import BUGSINK_HTTP_PORT
from imbue.observability.remote_install import ObservabilityDeployError
from imbue.observability.remote_install import run_root_script_over_ssh
from imbue.observability.remote_install import run_ssh_command
from imbue.observability.remote_install import scp_files
from imbue.observability.remote_install import wait_for_ssh_ready

# Where the Bugsink venv and the vendored settings module live on the host.
BUGSINK_HOME_DIR: Final[str] = "/opt/bugsink"

# Stamp recording the sha256 of the installed requirements export, so a
# re-deploy on a converged host skips the pip install and a pin bump
# re-installs (the pip-package analog of the OpenObserve version stamp).
_REQUIREMENTS_STAMP_PATH: Final[str] = "/usr/local/share/bugsink-requirements.sha256"

# Staging dir on the host (the SSH user cannot write /etc or /opt directly;
# the install script sudo-moves everything into place).
BUGSINK_REMOTE_STAGING_DIR: Final[str] = "/tmp/bugsink-staging"

# On-disk destination of every staged artifact: the rendered configs (keys of
# ``render_all_bugsink_artifacts``) plus the two committed deploy assets.
BUGSINK_REMOTE_ARTIFACT_PATHS: Final[dict[str, str]] = {
    "bugsink.env": BUGSINK_ENV_FILE_PATH,
    "Caddyfile": CADDYFILE_PATH,
    "origin.pem": ORIGIN_CERTIFICATE_PATH,
    "origin.key": ORIGIN_PRIVATE_KEY_PATH,
    "nftables.conf": NFTABLES_CONF_PATH,
    "bugsink_conf.py": f"{BUGSINK_HOME_DIR}/bugsink_conf.py",
    "bugsink_requirements.txt": f"{BUGSINK_HOME_DIR}/bugsink_requirements.txt",
}

# How long the post-deploy readiness poll waits for gunicorn to answer on
# loopback: a first boot on a fresh database runs all of Bugsink's migrations
# against Neon inside the unit's ExecStartPre (~1-2 minutes cross-region;
# steady-state boots are seconds).
_READY_TIMEOUT_SECONDS: Final[float] = 300.0
_READY_POLL_INTERVAL_SECONDS: Final[float] = 5.0


class BugsinkNotReadyError(ObservabilityError):
    """Raised when the deployed Bugsink instance does not start serving within the wait window."""


def _deploy_assets_dir() -> Path:
    return Path(__file__).parent / "deploy_assets"


def bugsink_requirements_path() -> Path:
    """The committed hash-locked pip set the instance venv installs."""
    return _deploy_assets_dir() / "bugsink_requirements.txt"


def bugsink_conf_path() -> Path:
    """The vendored Django settings module shipped to the instance."""
    return _deploy_assets_dir() / "bugsink_conf.py"


def bugsink_cloud_init_path() -> Path:
    """The cloud-init user data the instance VPS is provisioned with."""
    return _deploy_assets_dir() / "cloud-init-bugsink.yaml"


@pure
def render_bugsink_install_script(requirements_sha256: str) -> str:
    """The idempotent remote install script (runs under sudo): venv + hash-locked pip set, staged config, restarts.

    ``requirements_sha256`` is the digest of the staged requirements export;
    it doubles as the converged-version stamp so a re-deploy with unchanged
    pins skips the pip install entirely. The pip install itself runs with
    ``--require-hashes``, so only the exact reviewed artifacts can ever land
    in the venv, whatever the resolver or index state.

    File modes: the env file embeds every instance secret (Django secret key,
    DSN, break-glass credentials) and is read by systemd as root, so it stays
    root-only; the origin TLS material is group-readable by caddy (which runs
    as the ``caddy`` user the Debian package creates).
    """
    return f"""\
set -euo pipefail

# sshd accepts connections before first boot finishes, and the one-shot
# provisioning recipe deploys immediately -- everything below needs the
# packages (caddy and its service group, nftables, python3-venv) that
# cloud-init's first boot installs. Tolerate a degraded/absent cloud-init
# (`|| true`): any dependency it failed to provide still fails loudly right
# after.
cloud-init status --wait 2>/dev/null || true

if ! id -u bugsink >/dev/null 2>&1; then
    useradd --system --home-dir {BUGSINK_HOME_DIR} --shell /usr/sbin/nologin bugsink
fi
install -d -m 0755 -o root -g root {BUGSINK_HOME_DIR}
mkdir -p /etc/bugsink /etc/caddy

install -m 0644 -o root -g root "{BUGSINK_REMOTE_STAGING_DIR}/bugsink_requirements.txt" "{BUGSINK_HOME_DIR}/bugsink_requirements.txt"
installed_sha="$(cat {_REQUIREMENTS_STAMP_PATH} 2>/dev/null || true)"
if [ ! -x {BUGSINK_HOME_DIR}/venv/bin/gunicorn ] || [ "${{installed_sha}}" != "{requirements_sha256}" ]; then
    python3 -m venv --clear {BUGSINK_HOME_DIR}/venv
    {BUGSINK_HOME_DIR}/venv/bin/pip install --require-hashes --no-deps -r "{BUGSINK_HOME_DIR}/bugsink_requirements.txt"
    mkdir -p "$(dirname {_REQUIREMENTS_STAMP_PATH})"
    echo "{requirements_sha256}" > {_REQUIREMENTS_STAMP_PATH}
fi

install -m 0644 -o root -g root "{BUGSINK_REMOTE_STAGING_DIR}/bugsink_conf.py" "{BUGSINK_HOME_DIR}/bugsink_conf.py"
install -m 0640 -o root -g bugsink "{BUGSINK_REMOTE_STAGING_DIR}/bugsink.env" "{BUGSINK_ENV_FILE_PATH}"
install -m 0644 -o root -g root "{BUGSINK_REMOTE_STAGING_DIR}/Caddyfile" "{CADDYFILE_PATH}"
install -m 0640 -o root -g caddy "{BUGSINK_REMOTE_STAGING_DIR}/origin.pem" "{ORIGIN_CERTIFICATE_PATH}"
install -m 0640 -o root -g caddy "{BUGSINK_REMOTE_STAGING_DIR}/origin.key" "{ORIGIN_PRIVATE_KEY_PATH}"
install -m 0644 -o root -g root "{BUGSINK_REMOTE_STAGING_DIR}/nftables.conf" "{NFTABLES_CONF_PATH}"
rm -rf "{BUGSINK_REMOTE_STAGING_DIR}"

systemctl daemon-reload
systemctl enable nftables caddy bugsink
systemctl restart nftables
systemctl restart bugsink
systemctl restart caddy
"""


@pure
def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def deploy_bugsink_instance(
    concurrency_group: ConcurrencyGroup,
    host: str,
    ssh_user: str,
    config: BugsinkInstanceConfig,
    work_dir: Path,
) -> None:
    """Render config locally, stage everything onto the host, and sudo-install + (re)start the services."""
    work_dir.mkdir(parents=True, exist_ok=True)
    # Every rendered artifact carries secrets (the env file, the origin key),
    # and the default work dir is a fixed path under /tmp, so keep the scratch
    # dir and every staged artifact owner-only. The chmod also fails loudly if
    # another local user pre-created the fixed path.
    work_dir.chmod(0o700)
    artifacts = render_all_bugsink_artifacts(config)
    for name, content in artifacts.items():
        artifact_path = work_dir / name
        artifact_path.write_text(content)
        artifact_path.chmod(0o600)
    requirements_text = bugsink_requirements_path().read_text()
    staged_paths = [work_dir / name for name in artifacts] + [bugsink_conf_path(), bugsink_requirements_path()]

    # Stage into /tmp (the SSH user cannot write /etc or /opt directly), then
    # run the install script under sudo to move files into place and restart
    # services. No -p on the mkdir: the rm guarantees non-existence, so the
    # mkdir fails loudly if another local user races the fixed /tmp path back
    # into existence (a -p would silently adopt their directory).
    try:
        # OVH reports an instance ACTIVE before its first boot finishes, so
        # the one-shot recipe's immediate deploy can beat sshd to the port
        # (observed live on the observability production bring-up).
        wait_for_ssh_ready(concurrency_group, host, ssh_user)
        run_ssh_command(
            concurrency_group,
            host,
            ssh_user,
            f"rm -rf {BUGSINK_REMOTE_STAGING_DIR} && mkdir -m 700 {BUGSINK_REMOTE_STAGING_DIR}",
        )
        scp_files(concurrency_group, host, ssh_user, staged_paths, BUGSINK_REMOTE_STAGING_DIR)
        run_root_script_over_ssh(
            concurrency_group, host, ssh_user, render_bugsink_install_script(_sha256_of_text(requirements_text))
        )
    finally:
        # The rendered artifacts are secret material with no value once staged
        # (the renderers are pure, so a preview or retry just re-renders); do
        # not leave them behind on the operator machine's disk.
        for name in artifacts:
            (work_dir / name).unlink(missing_ok=True)


class _BugsinkStillStartingError(ObservabilityError):
    """Internal: the loopback login page is not answering 200 yet; tenacity retries the probe."""


@retry(
    retry=retry_if_exception_type(_BugsinkStillStartingError),
    stop=stop_after_delay(_READY_TIMEOUT_SECONDS),
    wait=wait_fixed(_READY_POLL_INTERVAL_SECONDS),
    reraise=True,
)
def _probe_bugsink_ready_over_ssh(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str) -> None:
    # The login page is Bugsink's only stable unauthenticated 200, and a 200
    # from it proves the unit's ExecStartPre migrations finished and gunicorn
    # is serving -- the same signal the Modal-era health check used, probed
    # on loopback because the public gate deliberately hides it.
    try:
        run_ssh_command(
            concurrency_group,
            host,
            ssh_user,
            f"curl -fsS -o /dev/null --max-time 10 http://127.0.0.1:{BUGSINK_HTTP_PORT}/accounts/login/",
        )
    except ObservabilityDeployError as exc:
        raise _BugsinkStillStartingError(
            f"Bugsink on {host} did not answer 200 on its loopback login page within "
            f"{_READY_TIMEOUT_SECONDS:.0f}s (first boot runs migrations; check "
            f"`journalctl -u bugsink` on the host): {exc}"
        ) from exc


def await_bugsink_serving(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str) -> None:
    """Poll the instance's loopback login page over SSH until it answers 200.

    Raises :class:`BugsinkNotReadyError` when the wait window elapses (the
    window is sized for a first boot's migrations).
    """
    try:
        _probe_bugsink_ready_over_ssh(concurrency_group, host, ssh_user)
    except _BugsinkStillStartingError as exc:
        raise BugsinkNotReadyError(str(exc)) from exc
