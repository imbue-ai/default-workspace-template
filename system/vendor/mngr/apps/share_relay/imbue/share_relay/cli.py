"""Operator CLI for the self-hosted sharing relays.

Renders a region's on-disk relay config (frps, nftables, the :80 redirector),
runs the liveness endpoint, and drives the relay's whole operational lifecycle:
``provision`` creates the VPS on OVH Public Cloud, ``deploy`` installs the
pinned frps + rendered config over SSH and (re)starts the services, ``dns``
points the region's records at the instance, and ``list`` / ``destroy`` manage
existing instances. The justfile recipes are thin wrappers over these commands;
the pure render step stays separate so the config is unit-testable.
"""

import json
import os
import sys
from pathlib import Path

import click
from loguru import logger
from pydantic import AnyHttpUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import setup_logging
from imbue.share_relay.config_render import render_all_artifacts
from imbue.share_relay.connector_registration import deregister_relay
from imbue.share_relay.connector_registration import register_relay
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.dns_records import reconcile_relay_dns_records
from imbue.share_relay.healthcheck import serve_healthcheck
from imbue.share_relay.primitives import ContentDomain
from imbue.share_relay.primitives import DEFAULT_HEALTHCHECK_PORT
from imbue.share_relay.primitives import DEFAULT_TUNNEL_CONTROL_PORT
from imbue.share_relay.primitives import DEFAULT_VHOST_HTTPS_PORT
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayId
from imbue.share_relay.primitives import RelayPort
from imbue.share_relay.provisioning import DEFAULT_RELAY_FLAVOR_NAME
from imbue.share_relay.provisioning import DEFAULT_RELAY_IMAGE_NAME
from imbue.share_relay.provisioning import OvhPublicCloudRelayProvisioner
from imbue.share_relay.provisioning import build_relay_instance_name
from imbue.share_relay.provisioning import cloud_project_id_from_env
from imbue.share_relay.provisioning import make_ovh_client_from_env
from imbue.share_relay.provisioning import pick_public_ipv4
from imbue.share_relay.remote_install import deploy_relay


@click.group()
def main() -> None:
    """Render and serve config for the self-hosted sharing relays."""
    setup_logging(level="INFO")


def _emit_json(payload: object, indent: int | None = None) -> None:
    """Machine-readable JSON on stdout for the justfile recipes (which parse it with jq).

    The one sanctioned stdout write in this package: write_human_line lives in
    imbue-mngr, which this package deliberately does not depend on, and logs go
    to stderr via loguru.
    """
    sys.stdout.write(json.dumps(payload, indent=indent) + "\n")


def _admin_key_from_env() -> str:
    admin_key = os.environ.get("MINDS_ADMIN_KEY", "")
    if not admin_key:
        raise click.UsageError("MINDS_ADMIN_KEY is not set (the connector admin API key, from the tier's Vault)")
    return admin_key


def _plugin_auth_secret_from_env() -> SecretStr:
    # From the environment rather than argv so the secret never lands in shell
    # history or `ps` output (mirrors how MINDS_ADMIN_KEY is passed).
    plugin_auth_secret = os.environ.get("FRPS_AUTH_SECRET", "")
    if not plugin_auth_secret:
        raise click.UsageError(
            "FRPS_AUTH_SECRET is not set (the relay -> connector plugin auth secret, "
            "from the tier's Vault entry secrets/minds/<tier>/sharing)"
        )
    return SecretStr(plugin_auth_secret)


def _relay_configuration(
    relay_id: str, region: str, content_domain: str, plugin_auth_url: str, plugin_auth_secret: SecretStr
) -> RelayConfiguration:
    return RelayConfiguration(
        relay_id=RelayId(relay_id),
        region=RegionCode(region),
        content_domain=ContentDomain(content_domain),
        plugin_auth_url=AnyHttpUrl(plugin_auth_url),
        plugin_auth_secret=plugin_auth_secret,
        vhost_https_port=DEFAULT_VHOST_HTTPS_PORT,
        tunnel_control_port=DEFAULT_TUNNEL_CONTROL_PORT,
        healthcheck_port=DEFAULT_HEALTHCHECK_PORT,
    )


@main.command()
@click.option(
    "--relay-id", required=True, help="This relay's registered id (relay-<hex>, from `share-relay register`)"
)
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. imbueminds.com)")
@click.option(
    "--plugin-auth-url",
    required=True,
    help=(
        "Connector frps-auth endpoint WITHOUT any secret (https://<connector>/frps/auth); "
        "the plugin secret is read from FRPS_AUTH_SECRET in the environment"
    ),
)
@click.option(
    "--out-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the rendered config artifacts into",
)
def render(relay_id: str, region: str, content_domain: str, plugin_auth_url: str, out_dir: Path) -> None:
    """Render a relay's frps / nftables / :80-redirect config into OUT_DIR."""
    config = _relay_configuration(relay_id, region, content_domain, plugin_auth_url, _plugin_auth_secret_from_env())
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in render_all_artifacts(config).items():
        artifact_path = out_dir / name
        artifact_path.write_text(content)
        # frps.toml embeds the connector auth secret in the plugin addr's
        # userinfo; keep every rendered artifact owner-only (same hardening as
        # deploy).
        artifact_path.chmod(0o600)
        logger.info("Wrote {}", artifact_path)


@main.command()
@click.option("--healthcheck-port", default=int(DEFAULT_HEALTHCHECK_PORT), show_default=True, help="Port to serve on")
@click.option(
    "--tunnel-control-port",
    default=int(DEFAULT_TUNNEL_CONTROL_PORT),
    show_default=True,
    help="frps tunnel-control port to probe for liveness",
)
def healthcheck(healthcheck_port: int, tunnel_control_port: int) -> None:
    """Serve the relay liveness endpoint (GET /healthz)."""
    serve_healthcheck(RelayPort(healthcheck_port), RelayPort(tunnel_control_port))


@main.command()
@click.option("--env-name", required=True, help="Minds env this relay serves (e.g. dev-josh-1, staging, production)")
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option(
    "--ordinal",
    default=1,
    show_default=True,
    type=int,
    help="Which relay of the region this is (regions run several; names the instance share-relay-<env>-<region>-<n>)",
)
@click.option(
    "--ovh-region", required=True, help="OVH Public Cloud region to create the instance in (e.g. US-WEST-OR-1)"
)
@click.option("--flavor", default=DEFAULT_RELAY_FLAVOR_NAME, show_default=True, help="OVH flavor name")
@click.option("--image", default=DEFAULT_RELAY_IMAGE_NAME, show_default=True, help="OVH image name")
@click.option(
    "--ssh-public-key-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="SSH public key authorized on the instance (deploys run over SSH as root/debian)",
)
def provision(
    env_name: str, region: str, ordinal: int, ovh_region: str, flavor: str, image: str, ssh_public_key_file: Path
) -> None:
    """Create one relay instance on OVH Public Cloud and print its name, id, and IP.

    Reads OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY
    (+ optional OVH_ENDPOINT) and OVH_CLOUD_PROJECT_ID from the environment.
    """
    provisioner = OvhPublicCloudRelayProvisioner(
        client=make_ovh_client_from_env(),
        project_id=cloud_project_id_from_env(),
    )
    instance_name = build_relay_instance_name(env_name, RegionCode(region), ordinal)
    cloud_init_user_data = (Path(__file__).parent / "deploy_assets" / "cloud-init.yaml").read_text()
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
    logger.info("Relay instance ready: name={} id={} ip={}", instance_name, created["id"], ip_address)
    _emit_json({"name": instance_name, "instance_id": str(created["id"]), "ip": ip_address})


@main.command()
@click.option("--host", required=True, help="Relay host IP or DNS name to deploy onto")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the relay host")
@click.option(
    "--relay-id", required=True, help="This relay's registered id (relay-<hex>, from `share-relay register`)"
)
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. imbueminds.com)")
@click.option(
    "--plugin-auth-url",
    required=True,
    help=(
        "Connector frps-auth endpoint WITHOUT any secret (https://<connector>/frps/auth); "
        "the plugin secret is read from FRPS_AUTH_SECRET in the environment"
    ),
)
@click.option(
    "--work-dir",
    default=Path("/tmp/share-relay-deploy"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Local scratch directory for the rendered artifacts",
)
def deploy(
    host: str, ssh_user: str, relay_id: str, region: str, content_domain: str, plugin_auth_url: str, work_dir: Path
) -> None:
    """Render the relay's config, install the pinned frps + healthcheck, and (re)start its services."""
    config = _relay_configuration(relay_id, region, content_domain, plugin_auth_url, _plugin_auth_secret_from_env())
    with ConcurrencyGroup(name="share-relay-deploy") as concurrency_group:
        deploy_relay(
            concurrency_group=concurrency_group, host=host, ssh_user=ssh_user, config=config, work_dir=work_dir
        )
    logger.info("Deployed relay config for {} ({}) to {}", config.region_domain, config.relay_id, host)


@main.command()
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. minds-dev.com)")
@click.option(
    "--ip",
    "ip_addresses",
    required=True,
    multiple=True,
    help="A relay instance's public IPv4; pass once per relay in the region (the record SET is reconciled)",
)
def dns(region: str, content_domain: str, ip_addresses: tuple[str, ...]) -> None:
    """Reconcile the region's DNS record set: relay.<region-domain> and *.<region-domain> A records.

    Bring-up / disaster-recovery path; in steady state the connector's
    relay_health_sweep cron maintains the same records (health-filtered) from
    the relays table. Reads CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID (the
    content domain's zone) from the environment. Records are gray-cloud (DNS
    only) -- the relays do SNI passthrough, so Cloudflare must not sit in
    front of them.
    """
    region_domain = f"{RegionCode(region)}.{ContentDomain(content_domain)}"
    record_names = reconcile_relay_dns_records(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
        region_domain=region_domain,
        ip_addresses=list(ip_addresses),
    )
    logger.info("Reconciled {} DNS record sets for {} to {}", len(record_names), region_domain, list(ip_addresses))


@main.command()
@click.option("--connector-url", required=True, help="The tier's connector base URL")
@click.option("--relay-id", default=None, help="Existing relay id to update/revive; omit to mint a fresh one")
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--tunnel-endpoint", required=True, help="host:port the workspaces' frpc dials (typically <ip>:7000)")
@click.option("--ip", "ip_address", required=True, help="The relay instance's public IPv4")
@click.option("--instance-name", default="", help="Human-readable OVH instance name")
def register(
    connector_url: str, relay_id: str | None, region: str, tunnel_endpoint: str, ip_address: str, instance_name: str
) -> None:
    """Register this relay in the connector's fleet inventory (reads MINDS_ADMIN_KEY from the environment).

    The final provisioning step: registration makes the relay share-eligible
    (assignment + frps auth) immediately; DNS converges on the connector's
    next health-sweep pass. Prints the relay record (including the minted
    relay_id) as JSON on stdout.
    """
    record = register_relay(
        connector_url=connector_url,
        admin_key=_admin_key_from_env(),
        relay_id=RelayId(relay_id) if relay_id else None,
        region=RegionCode(region),
        tunnel_endpoint=tunnel_endpoint,
        ip_address=ip_address,
        instance_name=instance_name,
    )
    logger.info("Registered relay {} for region {}", record.get("relay_id"), region)
    _emit_json(record)


@main.command()
@click.option("--connector-url", required=True, help="The tier's connector base URL")
@click.option("--relay-id", required=True, help="The relay id to retire (from `minds-admin relays list`)")
def deregister(connector_url: str, relay_id: str) -> None:
    """Retire this relay from the connector's fleet inventory (reads MINDS_ADMIN_KEY from the environment)."""
    record = deregister_relay(
        connector_url=connector_url,
        admin_key=_admin_key_from_env(),
        relay_id=RelayId(relay_id),
    )
    logger.info("Deregistered relay {}", relay_id)
    _emit_json(record)


@main.command(name="list")
@click.option("--name-prefix", default="share-relay-", show_default=True, help="Instance name prefix to list")
def list_relays(name_prefix: str) -> None:
    """List relay instances in the OVH Public Cloud project."""
    provisioner = OvhPublicCloudRelayProvisioner(
        client=make_ovh_client_from_env(),
        project_id=cloud_project_id_from_env(),
    )
    rows = [
        {
            "name": instance.get("name"),
            "instance_id": instance.get("id"),
            "status": instance.get("status"),
            "region": instance.get("region"),
        }
        for instance in provisioner.list_relay_instances(name_prefix)
    ]
    _emit_json(rows, indent=2)


@main.command()
@click.option("--instance-id", required=True, help="OVH instance id to delete (from `share-relay list`)")
def destroy(instance_id: str) -> None:
    """Delete one relay instance."""
    provisioner = OvhPublicCloudRelayProvisioner(
        client=make_ovh_client_from_env(),
        project_id=cloud_project_id_from_env(),
    )
    provisioner.delete_instance(instance_id)
    logger.info("Deleted relay instance {}", instance_id)


if __name__ == "__main__":
    main()
