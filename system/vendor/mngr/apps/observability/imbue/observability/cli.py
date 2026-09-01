"""Operator CLI for the per-tier observability instances (OpenObserve on an OVH VPS).

Drives the instance lifecycle from the justfile recipes: ``provision`` creates
the VPS on OVH Public Cloud (reusing the share-relay provisioner), ``deploy``
installs the pinned OpenObserve + rendered config + self-monitoring collector
over SSH, ``dns`` upserts the Cloudflare-proxied ingest record,
``provision-accounts`` mints the per-sender-class ingest credentials and
applies log-stream retention through an SSH tunnel, and
``render-collector-install`` / ``install-collector`` produce the fleet
collector installs (``minds-admin server prep`` / ``setup`` render the box
install in-process; ``render-collector-install`` remains the ad-hoc
``--extra-prep-script`` escape hatch, and relays are installed directly over
SSH).

Secrets always arrive via environment variables or files, never argv (argv is
visible in the process table); the justfile recipes source them from the
tier's Vault entry via ``scripts/provision_observability_config.py``.
"""

import json
import os
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import click
import httpx
from loguru import logger
from pydantic import AnyHttpUrl
from pydantic import SecretStr
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.logging import setup_logging
from imbue.observability.bugsink_api import mint_bugsink_api_token_over_ssh
from imbue.observability.bugsink_api import provision_bugsink_projects
from imbue.observability.bugsink_remote_install import await_bugsink_serving
from imbue.observability.bugsink_remote_install import bugsink_cloud_init_path
from imbue.observability.bugsink_remote_install import deploy_bugsink_instance
from imbue.observability.collector_install import render_collector_install_script
from imbue.observability.dashboards import dashboard_definitions_dir
from imbue.observability.dashboards import ensure_dashboards
from imbue.observability.dashboards import load_dashboard_definitions
from imbue.observability.data_types import BugsinkInstanceConfig
from imbue.observability.data_types import CollectorInstallConfig
from imbue.observability.data_types import ObservabilityInstanceConfig
from imbue.observability.dns_records import reconcile_ingest_dns_record
from imbue.observability.errors import ObservabilityError
from imbue.observability.openobserve_api import OpenObserveHttpApi
from imbue.observability.openobserve_api import apply_log_stream_retention
from imbue.observability.openobserve_api import ensure_sender_credentials
from imbue.observability.primitives import BUGSINK_HTTP_PORT
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import ErrorsHostname
from imbue.observability.primitives import LOGS_RETENTION_DAYS
from imbue.observability.primitives import OPENOBSERVE_HTTP_PORT
from imbue.observability.primitives import ObservabilityTierName
from imbue.observability.primitives import PublicIngestHostname
from imbue.observability.primitives import SenderClass
from imbue.observability.primitives import TelemetryHostname
from imbue.observability.remote_install import deploy_instance
from imbue.observability.remote_install import run_root_script_over_ssh
from imbue.share_relay.provisioning import OvhPublicCloudRelayProvisioner
from imbue.share_relay.provisioning import cloud_project_id_from_env
from imbue.share_relay.provisioning import make_ovh_client_from_env
from imbue.share_relay.provisioning import pick_public_ipv4

INSTANCE_NAME_PREFIX: Final[str] = "observability-"
BUGSINK_INSTANCE_NAME_PREFIX: Final[str] = "bugsink-"

# The environment variables the bugsink subcommands read their secrets from
# (sourced from the tier's `bugsink` Vault entry by the justfile recipes via
# scripts/provision_bugsink_config.py).
BUGSINK_SECRET_KEY_ENV_VAR: Final[str] = "BUGSINK_SECRET_KEY"
BUGSINK_DATABASE_URL_ENV_VAR: Final[str] = "BUGSINK_DATABASE_URL"
BUGSINK_CREATE_SUPERUSER_ENV_VAR: Final[str] = "BUGSINK_CREATE_SUPERUSER"
BUGSINK_ORIGIN_TLS_CERT_FILE_ENV_VAR: Final[str] = "BUGSINK_ORIGIN_TLS_CERT_FILE"
BUGSINK_ORIGIN_TLS_KEY_FILE_ENV_VAR: Final[str] = "BUGSINK_ORIGIN_TLS_KEY_FILE"

# The environment variables ``deploy`` / ``provision-accounts`` read their
# secrets from (sourced from the tier's Vault entry by the justfile recipes).
ROOT_EMAIL_ENV_VAR: Final[str] = "OBSERVABILITY_ROOT_EMAIL"
ROOT_PASSWORD_ENV_VAR: Final[str] = "OBSERVABILITY_ROOT_PASSWORD"
META_DSN_ENV_VAR: Final[str] = "OBSERVABILITY_META_DSN"
R2_ENDPOINT_ENV_VAR: Final[str] = "OBSERVABILITY_R2_ENDPOINT"
R2_BUCKET_ENV_VAR: Final[str] = "OBSERVABILITY_R2_BUCKET"
R2_ACCESS_KEY_ID_ENV_VAR: Final[str] = "OBSERVABILITY_R2_ACCESS_KEY_ID"
R2_SECRET_ACCESS_KEY_ENV_VAR: Final[str] = "OBSERVABILITY_R2_SECRET_ACCESS_KEY"
ORIGIN_TLS_CERT_FILE_ENV_VAR: Final[str] = "OBSERVABILITY_ORIGIN_TLS_CERT_FILE"
ORIGIN_TLS_KEY_FILE_ENV_VAR: Final[str] = "OBSERVABILITY_ORIGIN_TLS_KEY_FILE"
CLOUDFLARE_API_TOKEN_ENV_VAR: Final[str] = "CLOUDFLARE_API_TOKEN"
CLOUDFLARE_ZONE_ID_ENV_VAR: Final[str] = "CLOUDFLARE_ZONE_ID"
_INGEST_CREDENTIAL_ENV_VAR_BY_SENDER: Final[dict[SenderClass, str]] = {
    SenderClass.MODAL: "INGEST_CREDENTIAL_MODAL",
    SenderClass.BOXES: "INGEST_CREDENTIAL_BOXES",
    SenderClass.RELAYS: "INGEST_CREDENTIAL_RELAYS",
}

_DEFAULT_FLAVOR_NAME: Final[str] = "d2-4"
_DEFAULT_IMAGE_NAME: Final[str] = "Debian 13"
_SSH_TUNNEL_READY_TIMEOUT_SECONDS: Final[float] = 60.0

# How long ``provision-accounts`` waits for the OpenObserve API to answer
# through the tunnel. Generous because a first boot runs metadata migrations
# against Neon before the HTTP server comes up.
_API_READY_TIMEOUT_SECONDS: Final[float] = 120.0
_API_READY_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0


class MissingSecretEnvVarError(ObservabilityError):
    """Raised when a required secret environment variable is unset or empty."""


@click.group()
def main() -> None:
    """Provision and operate the per-tier observability instances."""
    setup_logging(level="INFO")


def _emit_json(payload: object) -> None:
    """Machine-readable JSON on stdout for the justfile recipes (parsed with jq or piped to the Vault glue script)."""
    sys.stdout.write(json.dumps(payload) + "\n")


def _require_env(env_var_name: str) -> str:
    value = os.environ.get(env_var_name, "")
    if not value:
        raise MissingSecretEnvVarError(
            f"{env_var_name} is not set; source the tier's Vault-resolved environment first "
            "(see scripts/provision_observability_config.py)."
        )
    return value


def _instance_config_from_env(tier: str, telemetry_hostname: str) -> ObservabilityInstanceConfig:
    """Assemble the instance config from flags (non-secrets) + environment (secrets)."""
    root_user_email = _require_env(ROOT_EMAIL_ENV_VAR)
    origin_cert_path = Path(_require_env(ORIGIN_TLS_CERT_FILE_ENV_VAR))
    origin_key_path = Path(_require_env(ORIGIN_TLS_KEY_FILE_ENV_VAR))
    return ObservabilityInstanceConfig(
        tier=ObservabilityTierName(tier),
        telemetry_hostname=TelemetryHostname(telemetry_hostname),
        root_user_email=root_user_email,
        root_user_password=SecretStr(_require_env(ROOT_PASSWORD_ENV_VAR)),
        meta_postgres_dsn=SecretStr(_require_env(META_DSN_ENV_VAR)),
        r2_endpoint_url=AnyHttpUrl(_require_env(R2_ENDPOINT_ENV_VAR)),
        r2_bucket_name=_require_env(R2_BUCKET_ENV_VAR),
        r2_access_key_id=_require_env(R2_ACCESS_KEY_ID_ENV_VAR),
        r2_secret_access_key=SecretStr(_require_env(R2_SECRET_ACCESS_KEY_ENV_VAR)),
        origin_tls_certificate_pem=origin_cert_path.read_text(),
        origin_tls_private_key_pem=SecretStr(origin_key_path.read_text()),
    )


def _collector_config_from_options(
    role: str, tier: str, ingest_url: str, credential_env_var: str
) -> CollectorInstallConfig:
    return CollectorInstallConfig(
        role=CollectorRole(role.upper()),
        tier=ObservabilityTierName(tier),
        ingest_url=AnyHttpUrl(ingest_url),
        ingest_authorization_header_value=SecretStr(_require_env(credential_env_var)),
    )


def _make_provisioner() -> OvhPublicCloudRelayProvisioner:
    """The OVH Public Cloud provisioner, credentialed from the environment (OVH_* / OVH_CLOUD_PROJECT_ID)."""
    return OvhPublicCloudRelayProvisioner(
        client=make_ovh_client_from_env(),
        project_id=cloud_project_id_from_env(),
    )


@main.command()
@click.option("--tier", required=True, help="Tier this instance serves (production / staging / dev)")
@click.option(
    "--ordinal",
    default=1,
    show_default=True,
    type=int,
    help="Replacement generation counter (a replacement instance coexists briefly with its predecessor)",
)
@click.option(
    "--ovh-region", required=True, help="OVH Public Cloud region to create the instance in (e.g. US-EAST-VA-1)"
)
@click.option("--flavor", default=_DEFAULT_FLAVOR_NAME, show_default=True, help="OVH flavor name")
@click.option("--image", default=_DEFAULT_IMAGE_NAME, show_default=True, help="OVH image name")
@click.option(
    "--ssh-public-key-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="SSH public key authorized on the instance (deploys run over SSH)",
)
def provision(tier: str, ordinal: int, ovh_region: str, flavor: str, image: str, ssh_public_key_file: Path) -> None:
    """Create one instance VPS on OVH Public Cloud and print its name, id, and IP.

    Reads OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY
    (+ optional OVH_ENDPOINT) and OVH_CLOUD_PROJECT_ID from the environment.
    """
    tier_name = ObservabilityTierName(tier)
    _provision_instance_vps(
        instance_name=f"{INSTANCE_NAME_PREFIX}{tier_name}-{ordinal}",
        ovh_region=ovh_region,
        flavor=flavor,
        image=image,
        cloud_init_user_data=(Path(__file__).parent / "deploy_assets" / "cloud-init.yaml").read_text(),
        ssh_public_key_file=ssh_public_key_file,
    )


def _provision_instance_vps(
    *,
    instance_name: str,
    ovh_region: str,
    flavor: str,
    image: str,
    cloud_init_user_data: str,
    ssh_public_key_file: Path,
) -> None:
    """Order one instance VPS, wait for ACTIVE, and emit its name/id/IP as JSON."""
    provisioner = _make_provisioner()
    ssh_key_id = provisioner.ensure_ssh_key(
        key_name=instance_name,
        public_key=ssh_public_key_file.read_text().strip(),
        ovh_region=ovh_region,
    )
    created = provisioner.create_relay_instance(
        instance_name=instance_name,
        ovh_region=ovh_region,
        flavor_name=flavor,
        image_name=image,
        cloud_init_user_data=cloud_init_user_data,
        ssh_key_id=ssh_key_id,
    )
    logger.info("Created instance {} ({}); waiting for it to become ACTIVE...", instance_name, created["id"])
    active = provisioner.wait_for_instance_active(str(created["id"]))
    ip_address = pick_public_ipv4(active)
    logger.info("Instance ready: name={} id={} ip={}", instance_name, created["id"], ip_address)
    _emit_json({"name": instance_name, "instance_id": str(created["id"]), "ip": ip_address})


@main.command()
@click.option("--host", required=True, help="Instance host IP to deploy onto")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the instance host")
@click.option("--tier", required=True, help="Tier this instance serves (production / staging / dev)")
@click.option("--telemetry-hostname", required=True, help="Public ingest hostname (e.g. telemetry.minds-dev.com)")
@click.option(
    "--work-dir",
    default=Path("/tmp/observability-deploy"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Local scratch directory for the rendered artifacts",
)
def deploy(host: str, ssh_user: str, tier: str, telemetry_hostname: str, work_dir: Path) -> None:
    """Install the pinned OpenObserve, rendered config, and self-monitoring collector, then (re)start services.

    Secrets come from the OBSERVABILITY_* environment variables (sourced from
    the tier's Vault entry by the justfile recipe).
    """
    config = _instance_config_from_env(tier, telemetry_hostname)
    with ConcurrencyGroup(name="observability-deploy") as concurrency_group:
        deploy_instance(
            concurrency_group=concurrency_group, host=host, ssh_user=ssh_user, config=config, work_dir=work_dir
        )
    logger.info("Deployed observability instance config for {} (tier {}) to {}", telemetry_hostname, tier, host)


@main.command()
@click.option("--hostname", required=True, help="Public ingest hostname (e.g. telemetry.minds-dev.com)")
@click.option("--ip", required=True, help="The instance's public IPv4")
def dns(hostname: str, ip: str) -> None:
    """Upsert the Cloudflare-proxied A record for the ingest hostname.

    Reads CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID (the tier zone) from the
    environment. Proxied (orange cloud) on purpose: the origin firewall admits
    only Cloudflare's ranges, so the proxy is the sole way in. Shared by the
    OpenObserve (telemetry.<domain>) and Bugsink (errors.<domain>) flows.
    """
    is_changed = reconcile_ingest_dns_record(
        api_token=_require_env(CLOUDFLARE_API_TOKEN_ENV_VAR),
        zone_id=_require_env(CLOUDFLARE_ZONE_ID_ENV_VAR),
        hostname=PublicIngestHostname(hostname),
        ip=ip,
    )
    logger.info("Reconciled proxied ingest record {} -> {} (changed: {})", hostname, ip, is_changed)


class _TunnelNotReadyYetError(ObservabilityError):
    """Internal: the SSH tunnel's local port is not accepting yet; tenacity retries the probe."""


class SshTunnelExitedError(ObservabilityError):
    """Raised when the SSH tunnel process exits before its local port starts accepting."""


@retry(
    retry=retry_if_exception_type(_TunnelNotReadyYetError),
    stop=stop_after_delay(_SSH_TUNNEL_READY_TIMEOUT_SECONDS),
    wait=wait_fixed(0.5),
    reraise=True,
)
def _wait_for_local_port(port: int, tunnel_process: RunningProcess) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
    except OSError as exc:
        # A dead tunnel never starts accepting: surface ssh's own failure
        # (BatchMode makes auth problems exit immediately) instead of spinning
        # out the full ready window and reporting a misleading port timeout.
        if tunnel_process.returncode is not None:
            raise SshTunnelExitedError(
                f"SSH tunnel exited with code {tunnel_process.returncode} before local port {port} "
                f"started accepting: {tunnel_process.read_stderr().strip()}"
            ) from exc
        # The message is only ever observed when the ready window expires and
        # tenacity reraises the final probe's exception, so it states the
        # terminal condition rather than the per-probe one.
        raise _TunnelNotReadyYetError(
            f"local port {port} of the SSH tunnel did not start accepting within "
            f"{_SSH_TUNNEL_READY_TIMEOUT_SECONDS:.0f}s (the tunnel process is still running)"
        ) from exc


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return int(probe_socket.getsockname()[1])


class OpenObserveNotReadyError(ObservabilityError):
    """Raised when the OpenObserve API does not become ready within the wait window."""


def _probe_openobserve_ready(client: httpx.Client, base_url: str) -> None:
    """One unauthenticated ``/healthz`` probe; raises when the API is not answering (with 200) yet."""
    try:
        response = client.get(f"{base_url}/healthz")
    except httpx.HTTPError as exc:
        raise OpenObserveNotReadyError(f"OpenObserve API at {base_url} is not answering yet: {exc}") from exc
    if response.status_code != 200:
        raise OpenObserveNotReadyError(
            f"OpenObserve API at {base_url} is not ready yet (healthz answered {response.status_code})"
        )


@retry(
    retry=retry_if_exception_type(OpenObserveNotReadyError),
    stop=stop_after_delay(_API_READY_TIMEOUT_SECONDS),
    wait=wait_fixed(2.0),
    reraise=True,
)
def _wait_for_openobserve_ready(client: httpx.Client, base_url: str) -> None:
    _probe_openobserve_ready(client, base_url)


@contextmanager
def _openobserve_api_over_ssh_tunnel(ssh_host: str, ssh_user: str, group_name: str) -> Iterator[OpenObserveHttpApi]:
    """Yield a root-authenticated API client for one instance, through a short-lived SSH tunnel.

    The API is only reachable on the instance's loopback (the public gate
    exposes ingest routes only). ssh binds the -L port as soon as it
    authenticates, which says nothing about the remote service -- and the
    provisioning recipe runs seconds after ``deploy`` (re)started openobserve
    (whose first boot migrates the metadata store before its HTTP server
    answers) -- so the API itself is waited for before yielding.
    """
    root_email = _require_env(ROOT_EMAIL_ENV_VAR)
    root_password = _require_env(ROOT_PASSWORD_ENV_VAR)
    local_port = _find_free_local_port()
    with ConcurrencyGroup(name=group_name) as concurrency_group:
        tunnel_process = concurrency_group.run_process_in_background(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "BatchMode=yes",
                "-N",
                "-L",
                f"{local_port}:127.0.0.1:{OPENOBSERVE_HTTP_PORT}",
                f"{ssh_user}@{ssh_host}",
            ],
            is_checked_by_group=False,
            name=f"observability-tunnel-{ssh_host}",
        )
        try:
            _wait_for_local_port(local_port, tunnel_process)
            base_url = f"http://127.0.0.1:{local_port}"
            with httpx.Client(timeout=_API_READY_PROBE_TIMEOUT_SECONDS) as ready_client:
                _wait_for_openobserve_ready(ready_client, base_url)
            yield OpenObserveHttpApi(
                base_url=base_url,
                root_user_email=root_email,
                root_user_password=SecretStr(root_password),
            )
        finally:
            # ``ssh -N`` never exits on its own: tearing it down on every path
            # keeps the group exit from stalling on (and mis-reporting) a
            # still-running tunnel when the API work fails.
            tunnel_process.terminate()


@main.command(name="provision-accounts")
@click.option("--ssh-host", required=True, help="Instance host IP (the API is reached over an SSH tunnel)")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the instance host")
def provision_accounts(ssh_host: str, ssh_user: str) -> None:
    """Mint the per-sender-class ingest users and apply log-stream retention; print the result as JSON.

    Idempotent: senders whose INGEST_CREDENTIAL_* environment variable already
    carries a value are left alone; retention overrides on streams that do not
    exist yet are reported as skipped (re-run after data flows). The API is
    only reachable on the instance's loopback (the public gate exposes ingest
    routes only), so the calls run through a short-lived SSH tunnel.
    """
    existing_credential_by_sender = {
        sender_class: os.environ.get(env_var_name, "")
        for sender_class, env_var_name in _INGEST_CREDENTIAL_ENV_VAR_BY_SENDER.items()
    }
    with _openobserve_api_over_ssh_tunnel(ssh_host, ssh_user, "observability-provision-accounts") as api:
        credential_by_sender = ensure_sender_credentials(api, existing_credential_by_sender)
        is_applied_by_stream = apply_log_stream_retention(api, LOGS_RETENTION_DAYS)
    _emit_json(
        {
            "credential_by_sender": {
                str(sender_class): {
                    "sender_email": credential.sender_email,
                    "authorization_header_value": credential.authorization_header_value.get_secret_value(),
                    "is_newly_minted": credential.is_newly_minted,
                }
                for sender_class, credential in credential_by_sender.items()
            },
            "log_stream_retention_applied": is_applied_by_stream,
        }
    )


@main.command(name="import-dashboards")
@click.option("--ssh-host", required=True, help="Instance host IP (the API is reached over an SSH tunnel)")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the instance host")
def import_dashboards(ssh_host: str, ssh_user: str) -> None:
    """Import every committed dashboard definition into the instance; print the result as JSON.

    Replace-by-title: an existing dashboard with a committed definition's
    title is deleted and recreated from the repo, so re-running converges on
    exactly what the repo holds. Iterate in the UI, export back into
    ``imbue/observability/dashboards/``, then re-import everywhere.
    """
    definitions = load_dashboard_definitions(dashboard_definitions_dir())
    with _openobserve_api_over_ssh_tunnel(ssh_host, ssh_user, "observability-import-dashboards") as api:
        actions = ensure_dashboards(api, definitions)
    _emit_json(
        {
            "imported": [
                {
                    "title": action.title,
                    "replaced_dashboard_ids": list(action.replaced_dashboard_ids),
                }
                for action in actions
            ],
        }
    )


@main.command(name="render-collector-install")
@click.option("--role", required=True, type=click.Choice(["box", "relay", "instance"]), help="Machine class")
@click.option("--tier", required=True, help="Tier whose instance the collector reports to")
@click.option("--ingest-url", required=True, help="Base ingest URL (https://telemetry.<domain>)")
@click.option(
    "--credential-env-var",
    required=True,
    help="Name of the environment variable holding the sender-class Authorization header value",
)
@click.option(
    "--out",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where to write the self-contained root install script",
)
def render_collector_install(role: str, tier: str, ingest_url: str, credential_env_var: str, out: Path) -> None:
    """Render the self-contained collector install script (the ad-hoc --extra-prep-script escape hatch)."""
    config = _collector_config_from_options(role, tier, ingest_url, credential_env_var)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The embedded config carries the ingest credential, so the file is
    # owner-only BEFORE any content lands in it (touch applies the mode only
    # on creation; the chmod covers a pre-existing file).
    out.touch(mode=0o600)
    out.chmod(0o600)
    out.write_text(render_collector_install_script(config))
    logger.info("Wrote collector install script for role {} (tier {}) to {}", role, tier, out)


@main.command(name="install-collector")
@click.option("--host", required=True, help="Host IP or DNS name to install the collector onto")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the host")
@click.option("--role", required=True, type=click.Choice(["box", "relay", "instance"]), help="Machine class")
@click.option("--tier", required=True, help="Tier whose instance the collector reports to")
@click.option("--ingest-url", required=True, help="Base ingest URL (https://telemetry.<domain>)")
@click.option(
    "--credential-env-var",
    required=True,
    help="Name of the environment variable holding the sender-class Authorization header value",
)
def install_collector(
    host: str, ssh_user: str, role: str, tier: str, ingest_url: str, credential_env_var: str
) -> None:
    """Install (or converge) the pinned collector on one plainly-SSH-able host (e.g. a share relay).

    Bare-metal boxes are NOT installed this way -- their SSH is host-key-pinned
    by the prep flow (``minds-admin server prep`` / ``setup``), which renders
    and appends the install script in-process instead.
    """
    config = _collector_config_from_options(role, tier, ingest_url, credential_env_var)
    script = render_collector_install_script(config)
    with ConcurrencyGroup(name="observability-install-collector") as concurrency_group:
        run_root_script_over_ssh(concurrency_group, host, ssh_user, script)
    logger.info("Installed collector (role {}, tier {}) on {}", role, tier, host)


@main.command(name="list")
@click.option("--name-prefix", default=INSTANCE_NAME_PREFIX, show_default=True, help="Instance name prefix to list")
def list_instances(name_prefix: str) -> None:
    """List observability instances in the OVH Public Cloud project."""
    provisioner = _make_provisioner()
    rows = [
        {
            "name": instance.get("name"),
            "instance_id": instance.get("id"),
            "status": instance.get("status"),
            "region": instance.get("region"),
        }
        for instance in provisioner.list_relay_instances(name_prefix)
    ]
    _emit_json(rows)


@main.command()
@click.option("--instance-id", required=True, help="OVH instance id to delete (from `observability list`)")
def destroy(instance_id: str) -> None:
    """Delete one instance VPS.

    Stop the openobserve service first and wait out the WAL quiesce window
    (see the spec's "Replacing an instance") so the tail flushes to R2.
    """
    provisioner = _make_provisioner()
    provisioner.delete_instance(instance_id)
    logger.info("Deleted observability instance {}", instance_id)


@main.group()
def bugsink() -> None:
    """Provision and operate the per-tier Bugsink error-tracker instances.

    Same VPS + split-plane pattern as the OpenObserve instances (see the
    top-level commands); the generic ``dns`` / ``list`` / ``destroy``
    commands are shared (pass ``--name-prefix bugsink-`` to ``list``).
    """


def _bugsink_config_from_env(tier: str, errors_hostname: str) -> BugsinkInstanceConfig:
    """Assemble the instance config from flags (non-secrets) + environment (secrets)."""
    origin_cert_path = Path(_require_env(BUGSINK_ORIGIN_TLS_CERT_FILE_ENV_VAR))
    origin_key_path = Path(_require_env(BUGSINK_ORIGIN_TLS_KEY_FILE_ENV_VAR))
    return BugsinkInstanceConfig(
        tier=ObservabilityTierName(tier),
        errors_hostname=ErrorsHostname(errors_hostname),
        secret_key=SecretStr(_require_env(BUGSINK_SECRET_KEY_ENV_VAR)),
        database_url=SecretStr(_require_env(BUGSINK_DATABASE_URL_ENV_VAR)),
        create_superuser=SecretStr(_require_env(BUGSINK_CREATE_SUPERUSER_ENV_VAR)),
        origin_tls_certificate_pem=origin_cert_path.read_text(),
        origin_tls_private_key_pem=SecretStr(origin_key_path.read_text()),
    )


@bugsink.command(name="provision")
@click.option("--tier", required=True, help="Tier this instance serves (production / staging / dev)")
@click.option(
    "--ordinal",
    default=1,
    show_default=True,
    type=int,
    help="Replacement generation counter (a replacement instance coexists briefly with its predecessor)",
)
@click.option(
    "--ovh-region",
    required=True,
    help="OVH Public Cloud region to create the instance in; pick the one nearest the tier's Neon project "
    "(Bugsink's digestion issues ~70 sequential DB queries per event, so ingest throughput scales with ~1/RTT)",
)
@click.option("--flavor", default=_DEFAULT_FLAVOR_NAME, show_default=True, help="OVH flavor name")
@click.option("--image", default=_DEFAULT_IMAGE_NAME, show_default=True, help="OVH image name")
@click.option(
    "--ssh-public-key-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="SSH public key authorized on the instance (deploys run over SSH)",
)
def bugsink_provision(
    tier: str, ordinal: int, ovh_region: str, flavor: str, image: str, ssh_public_key_file: Path
) -> None:
    """Create one Bugsink instance VPS on OVH Public Cloud and print its name, id, and IP.

    Reads OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY
    (+ optional OVH_ENDPOINT) and OVH_CLOUD_PROJECT_ID from the environment.
    """
    tier_name = ObservabilityTierName(tier)
    _provision_instance_vps(
        instance_name=f"{BUGSINK_INSTANCE_NAME_PREFIX}{tier_name}-{ordinal}",
        ovh_region=ovh_region,
        flavor=flavor,
        image=image,
        cloud_init_user_data=bugsink_cloud_init_path().read_text(),
        ssh_public_key_file=ssh_public_key_file,
    )


@bugsink.command(name="deploy")
@click.option("--host", required=True, help="Instance host IP to deploy onto")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the instance host")
@click.option("--tier", required=True, help="Tier this instance serves (production / staging / dev)")
@click.option("--errors-hostname", required=True, help="Public ingest hostname (e.g. errors.minds-dev.com)")
@click.option(
    "--work-dir",
    default=Path("/tmp/bugsink-deploy"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Local scratch directory for the rendered artifacts",
)
def bugsink_deploy(host: str, ssh_user: str, tier: str, errors_hostname: str, work_dir: Path) -> None:
    """Install the hash-locked Bugsink venv and rendered config, (re)start services, and wait until serving.

    Secrets come from the BUGSINK_* environment variables (sourced from the
    tier's `bugsink` Vault entry by the justfile recipe). The post-install
    wait polls the loopback login page over SSH -- a first boot on a fresh
    database runs all migrations before gunicorn answers.
    """
    config = _bugsink_config_from_env(tier, errors_hostname)
    with ConcurrencyGroup(name="bugsink-deploy") as concurrency_group:
        deploy_bugsink_instance(
            concurrency_group=concurrency_group, host=host, ssh_user=ssh_user, config=config, work_dir=work_dir
        )
        await_bugsink_serving(concurrency_group, host, ssh_user)
    logger.info("Deployed bugsink instance config for {} (tier {}) to {}", errors_hostname, tier, host)


@bugsink.command(name="provision-projects")
@click.option("--ssh-host", required=True, help="Instance host IP (the REST API is reached over an SSH tunnel)")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the instance host")
@click.option("--tier", required=True, help="Tier this instance serves (production / staging / dev)")
def bugsink_provision_projects(ssh_host: str, ssh_user: str, tier: str) -> None:
    """Mint a REST API token and get-or-create the team + per-service projects; print the result as JSON.

    Idempotent (teams/projects are get-or-create by name, so a re-run
    returns the same DSNs; each run mints a fresh bearer token). The
    canonical REST API is loopback-only behind the caddy gate, so the calls
    run through a short-lived SSH tunnel; the token itself is minted by
    running ``bugsink-manage create_auth_token`` on the host. The JSON
    (``{"api_token": ..., "dsn_by_vault_key": {...}}``) is consumed by
    ``scripts/provision_bugsink_config.py store-dsns``.
    """
    tier_name = ObservabilityTierName(tier)
    local_port = _find_free_local_port()
    with ConcurrencyGroup(name="bugsink-provision-projects") as concurrency_group:
        token = mint_bugsink_api_token_over_ssh(concurrency_group, ssh_host, ssh_user)
        tunnel_process = concurrency_group.run_process_in_background(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "BatchMode=yes",
                "-N",
                "-L",
                f"{local_port}:127.0.0.1:{BUGSINK_HTTP_PORT}",
                f"{ssh_user}@{ssh_host}",
            ],
            is_checked_by_group=False,
            name=f"bugsink-tunnel-{ssh_host}",
        )
        try:
            _wait_for_local_port(local_port, tunnel_process)
            with httpx.Client() as client:
                dsn_by_vault_key = provision_bugsink_projects(
                    client, base_url=f"http://127.0.0.1:{local_port}", token=token, tier=tier_name
                )
        finally:
            # ``ssh -N`` never exits on its own: tearing it down on every path
            # keeps the group exit from stalling on (and mis-reporting) a
            # still-running tunnel when the API work fails.
            tunnel_process.terminate()
    _emit_json({"api_token": token, "dsn_by_vault_key": dsn_by_vault_key})


if __name__ == "__main__":
    main()
