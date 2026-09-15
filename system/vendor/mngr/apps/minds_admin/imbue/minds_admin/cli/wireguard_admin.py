"""``minds-admin wireguard ...`` -- the gen-2 management WireGuard overlay (specs/slice-fleet-gen2).

Operators reach gen-2 boxes over a plain self-hosted WireGuard overlay once
box ``:22`` locks down to the connector's Modal Proxy IPs. The peer list is
the ``[management_plane]`` table of the tier's committed ``deploy.toml`` (operator PUBLIC keys +
addresses; the private halves never leave their machines); each box's own key
material is generated at prep and its public half recorded on the
``bare_metal_servers`` row. ``config`` renders an operator's client config
from those rows; ``sync-peers`` converges the live fleet on the committed
peer list.
"""

from typing import Any

import click
import psycopg2
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import resolve_management_plane_config_or_none
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli.server import box_management_identities
from imbue.minds_admin.cli.server import run_root_script_over_ssh
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.minds_admin.slices.box_access import resolve_box_management_dial
from imbue.minds_admin.slices.management_plane import build_operator_wireguard_client_config
from imbue.minds_admin.slices.management_plane import render_wireguard_prep_section
from imbue.minds_admin.slices.onetun_install import OnetunInstallError
from imbue.minds_admin.slices.onetun_install import install_pinned_onetun
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION


@click.group(name="wireguard")
def wireguard() -> None:
    """Gen-2 management WireGuard overlay: operator client configs and fleet peer sync."""


# Hidden alias for muscle memory: `minds-admin wg ...` keeps working, but only
# `wireguard` appears in the help listing (the no-abbreviations rule).
wireguard_alias = click.Group(
    name="wg",
    hidden=True,
    commands=wireguard.commands,
    help="Hidden alias for `minds-admin wireguard`.",
)


def _require_management_plane_config() -> tuple[str, ManagementPlaneConfig]:
    """The activated tier's name + [management_plane] config, or a clear refusal when it has none."""
    env_name = active_env_name_or_none()
    if env_name is None:
        raise click.ClickException(
            'No minds env is activated in this shell. Run `eval "$(uv run minds-admin env activate <name>)"` first.'
        )
    management_plane_config = resolve_management_plane_config_or_none()
    if management_plane_config is None:
        raise click.ClickException(
            "the activated tier's deploy.toml has no [management_plane] table; add one to "
            "apps/minds/imbue/minds/config/envs/<tier>/deploy.toml (see the dev tier's file for the schema) "
            "and re-run"
        )
    return tier_for_env_name(env_name), management_plane_config


def _select_operator(
    management_plane_config: ManagementPlaneConfig, operator_name: str | None
) -> WireguardOperatorConfig:
    """Pick the requested operator from the committed peer list (or the only one)."""
    operators = management_plane_config.wireguard.operators
    if not operators:
        raise click.ClickException(
            "the tier's deploy.toml lists no [[management_plane.wireguard.operators]]; add your public key "
            "and overlay address there first"
        )
    if operator_name is None:
        if len(operators) == 1:
            return operators[0]
        names = ", ".join(str(operator.name) for operator in operators)
        raise click.UsageError(f"--operator is required when several operators are configured ({names})")
    for operator in operators:
        if str(operator.name) == operator_name:
            return operator
    names = ", ".join(str(operator.name) for operator in operators)
    raise click.UsageError(f"no operator {operator_name!r} in the tier's [management_plane] table (have: {names})")


def _fetch_gen2_wireguard_boxes(database_url: str | None) -> list[BareMetalServer]:
    """Every gen-2 box a wireguard command can address: overlay address assigned, key recorded, reachable."""
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        servers = fetch_servers(conn)
    finally:
        conn.close()
    gen2_boxes = [server for server in servers if server.box_generation >= FIRST_QEMU_BOX_GENERATION]
    ready_boxes = [
        server
        for server in gen2_boxes
        if server.wireguard_address and server.wireguard_public_key and server.public_address
    ]
    skipped_count = len(gen2_boxes) - len(ready_boxes)
    if skipped_count:
        logger.warning(
            "Skipping {} gen-2 box(es) without a recorded wireguard_address / wireguard_public_key / public_address "
            "(run `minds-admin server prep` on them first).",
            skipped_count,
        )
    return ready_boxes


@wireguard.command(name="config")
@click.option(
    "--operator",
    "operator_name",
    default=None,
    help="Which [[management_plane.wireguard.operators]] entry to render for (defaults to the only one when unambiguous).",
)
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
def wireguard_config(operator_name: str | None, database_url: str | None) -> None:
    """Emit the operator's wg-quick client config for the tier's gen-2 fleet.

    One ``[Peer]`` per prepped gen-2 box (endpoint = its public address, allowed
    IPs = its overlay /32). The ``PrivateKey`` placeholder must be replaced with
    the operator's own private key -- it exists only on their machine.
    """
    tier, management_plane_config = _require_management_plane_config()
    operator = _select_operator(management_plane_config, operator_name)
    boxes = _fetch_gen2_wireguard_boxes(database_url)
    config_text = build_operator_wireguard_client_config(
        operator=operator,
        tier=tier,
        boxes=boxes,
        listen_port=int(management_plane_config.wireguard.listen_port),
    )
    write_human_line(config_text)


@wireguard.command(name="install-onetun")
def wireguard_install_onetun() -> None:
    """Install the pinned onetun release (the userspace WireGuard transport) to its well-known path.

    Downloads the exact pinned version for this platform, verifies its sha256
    against the hash recorded in the repo, and installs it to
    ``~/.mindsadmin/bin/onetun`` -- a location the box-management dial
    resolver checks automatically. Idempotent and non-interactive (CI-safe):
    an already-current binary is left alone; any other version is refreshed.
    """
    with ConcurrencyGroup(name="onetun-install") as concurrency_group:
        try:
            result = install_pinned_onetun(concurrency_group)
        except OnetunInstallError as exc:
            raise click.ClickException(str(exc)) from exc
    if result.was_already_current:
        write_human_line(f"onetun {result.version} is already installed at {result.binary_path}")
    else:
        write_human_line(f"Installed onetun {result.version} at {result.binary_path}")


@wireguard.command(name="sync-peers")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option("--ssh-user", default="debian", help="Management SSH user on the box (the OS image's sudo user).")
def wireguard_sync_peers(database_url: str | None, ssh_user: str) -> None:
    """Converge every prepped gen-2 box's WireGuard peers on the committed [management_plane] operator list.

    Idempotent: re-renders each box's ``wg0.conf`` from the committed operator
    list and restarts the interface only when it actually changed (a live
    operator session over an unchanged config is never bounced). Per-box error
    capture; exits 1 if any box could not be synced (re-run until clean).
    """
    tier, management_plane_config = _require_management_plane_config()
    allocation = management_overlay_for_tier(tier)
    wireguard_settings = management_plane_config.wireguard
    boxes = _fetch_gen2_wireguard_boxes(database_url)
    if not boxes:
        write_human_line("No prepped gen-2 boxes to sync.")
        return
    outcome_by_server_id: dict[str, Any] = {}
    failed_count = 0
    with box_management_identities() as identities:
        for box in boxes:
            private_key_path = identities.private_key_path_for(box.box_generation)
            sync_script = "#!/bin/bash\nset -euo pipefail\n" + render_wireguard_prep_section(
                # Non-None by _fetch_gen2_wireguard_boxes's filter; str() for the type checker.
                wireguard_address=str(box.wireguard_address),
                listen_port=int(wireguard_settings.listen_port),
                operators=wireguard_settings.operators,
                overlay_prefix_length=allocation.overlay.prefixlen,
            )
            target_dial = resolve_box_management_dial(
                public_address=str(box.public_address),
                wireguard_address=box.wireguard_address,
                wireguard_public_key=box.wireguard_public_key,
            )
            try:
                run_root_script_over_ssh(
                    target_dial.host,
                    target_dial.port,
                    ssh_user,
                    private_key_path,
                    sync_script,
                    box.box_host_public_key or "",
                )
                outcome_by_server_id[str(box.id)] = {"synced": True, "address": target_dial.host}
            except BareMetalProvisioningError as exc:
                logger.warning("Peer sync on box {} ({}) failed: {}", box.id, target_dial.host, exc)
                outcome_by_server_id[str(box.id)] = {"synced": False, "address": target_dial.host, "error": str(exc)}
                failed_count += 1
    emit_json(
        {
            "operators": [str(operator.name) for operator in wireguard_settings.operators],
            "boxes": outcome_by_server_id,
        }
    )
    if failed_count:
        raise SystemExit(1)
