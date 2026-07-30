"""Operator CLI for the self-hosted sharing relays.

Renders a region's on-disk relay config (frps, nftables, the :80 redirector)
and runs the liveness endpoint. Provisioning the VPS itself and copying these
files onto it is driven by the justfile recipes (OVH Public Cloud API); this
CLI is the source of truth for what those files contain, so it stays testable
and the deploy step is a dumb copy.
"""

import json
import os
import sys
from pathlib import Path
from typing import Final

import click
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import setup_logging
from imbue.share_relay.config_render import render_frps_toml
from imbue.share_relay.config_render import render_nftables_conf
from imbue.share_relay.config_render import render_port_80_redirect_caddyfile
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.dns_records import upsert_relay_dns_records
from imbue.share_relay.healthcheck import serve_healthcheck
from imbue.share_relay.primitives import DEFAULT_HEALTHCHECK_PORT
from imbue.share_relay.primitives import DEFAULT_TUNNEL_CONTROL_PORT
from imbue.share_relay.primitives import DEFAULT_VHOST_HTTPS_PORT
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayPort
from imbue.share_relay.provisioning import DEFAULT_RELAY_FLAVOR_NAME
from imbue.share_relay.provisioning import DEFAULT_RELAY_IMAGE_NAME
from imbue.share_relay.provisioning import OvhPublicCloudRelayProvisioner
from imbue.share_relay.provisioning import build_relay_instance_name
from imbue.share_relay.provisioning import cloud_project_id_from_env
from imbue.share_relay.provisioning import make_ovh_client_from_env
from imbue.share_relay.provisioning import pick_public_ipv4
from imbue.share_relay.remote_install import deploy_relay

# Basenames the rendered artifacts are written under (consumed by the deploy
# recipes, which drop them into their well-known host locations).
_FRPS_TOML_NAME: Final[str] = "frps.toml"
_NFTABLES_CONF_NAME: Final[str] = "nftables.conf"
_PORT_80_CADDYFILE_NAME: Final[str] = "port80.Caddyfile"


@click.group()
def main() -> None:
    """Render and serve config for the self-hosted sharing relays."""
    setup_logging(level="INFO")


@main.command()
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. imbueminds.com)")
@click.option("--plugin-auth-url", required=True, help="Connector URL the frps plugin calls to authorize tunnels")
@click.option(
    "--out-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the rendered config artifacts into",
)
def render(region: str, content_domain: str, plugin_auth_url: str, out_dir: Path) -> None:
    """Render a region's frps / nftables / :80-redirect config into OUT_DIR."""
    config = RelayConfiguration(
        region=RegionCode(region),
        content_domain=content_domain,
        plugin_auth_url=plugin_auth_url,  # ty: ignore[invalid-argument-type]
        vhost_https_port=DEFAULT_VHOST_HTTPS_PORT,
        tunnel_control_port=DEFAULT_TUNNEL_CONTROL_PORT,
        healthcheck_port=DEFAULT_HEALTHCHECK_PORT,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        _FRPS_TOML_NAME: render_frps_toml(config),
        _NFTABLES_CONF_NAME: render_nftables_conf(config),
        _PORT_80_CADDYFILE_NAME: render_port_80_redirect_caddyfile(config),
    }
    for name, content in artifacts.items():
        (out_dir / name).write_text(content)
        logger.info("Wrote {}", out_dir / name)


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


def _relay_configuration(region: str, content_domain: str, plugin_auth_url: str) -> RelayConfiguration:
    return RelayConfiguration(
        region=RegionCode(region),
        content_domain=content_domain,
        plugin_auth_url=plugin_auth_url,  # ty: ignore[invalid-argument-type]
        vhost_https_port=DEFAULT_VHOST_HTTPS_PORT,
        tunnel_control_port=DEFAULT_TUNNEL_CONTROL_PORT,
        healthcheck_port=DEFAULT_HEALTHCHECK_PORT,
    )


@main.command()
@click.option("--env-name", required=True, help="Minds env this relay serves (e.g. dev-josh-1, staging, production)")
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
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
def provision(env_name: str, region: str, ovh_region: str, flavor: str, image: str, ssh_public_key_file: Path) -> None:
    """Create one relay instance on OVH Public Cloud and print its name, id, and IP.

    Reads OVH_APPLICATION_KEY / OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY
    (+ optional OVH_ENDPOINT) and OVH_CLOUD_PROJECT_ID from the environment.
    """
    provisioner = OvhPublicCloudRelayProvisioner(
        client=make_ovh_client_from_env(),
        project_id=cloud_project_id_from_env(),
    )
    instance_name = build_relay_instance_name(env_name, RegionCode(region))
    cloud_init_user_data = (Path(__file__).parent.parent.parent / "deploy" / "cloud-init.yaml").read_text()
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
    # Machine-readable stdout for the justfile recipes; write_human_line lives
    # in imbue-mngr, which this package deliberately does not depend on.
    sys.stdout.write(json.dumps({"name": instance_name, "instance_id": str(created["id"]), "ip": ip_address}) + "\n")


@main.command()
@click.option("--host", required=True, help="Relay host IP or DNS name to deploy onto")
@click.option("--ssh-user", default="debian", show_default=True, help="SSH user on the relay host")
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. imbueminds.com)")
@click.option(
    "--plugin-auth-url",
    required=True,
    help="Connector frps-auth URL INCLUDING the shared secret path segment (https://<connector>/frps/auth/<secret>)",
)
@click.option(
    "--work-dir",
    default=Path("/tmp/share-relay-deploy"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Local scratch directory for the rendered artifacts",
)
def deploy(host: str, ssh_user: str, region: str, content_domain: str, plugin_auth_url: str, work_dir: Path) -> None:
    """Render the relay's config, install the pinned frps + healthcheck, and (re)start its services."""
    config = _relay_configuration(region, content_domain, plugin_auth_url)
    with ConcurrencyGroup(name="share-relay-deploy") as concurrency_group:
        deploy_relay(
            concurrency_group=concurrency_group, host=host, ssh_user=ssh_user, config=config, work_dir=work_dir
        )
    logger.info("Deployed relay config for {} to {}", config.region_domain, host)


@main.command()
@click.option("--region", required=True, help="Region code, the label under the content apex (e.g. us1)")
@click.option("--content-domain", required=True, help="Content domain apex (e.g. minds-dev.com)")
@click.option("--ip", "ip_address", required=True, help="The relay instance's public IPv4")
def dns(region: str, content_domain: str, ip_address: str) -> None:
    """Point the region's DNS at a relay: relay.<region-domain> and *.<region-domain> A records.

    Reads CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID (the content domain's
    zone) from the environment. Records are gray-cloud (DNS only) -- the relay
    does SNI passthrough, so Cloudflare must not sit in front of it.
    """
    config = _relay_configuration(region, content_domain, "https://placeholder.invalid/frps/auth")
    record_ids = upsert_relay_dns_records(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
        config=config,
        ip_address=ip_address,
    )
    logger.info("Upserted {} DNS records for {}", len(record_ids), config.region_domain)


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
    sys.stdout.write(json.dumps(rows, indent=2) + "\n")


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
