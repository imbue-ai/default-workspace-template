"""``minds-admin wireguard ...`` -- the gen-2 management WireGuard overlay (specs/slice-fleet-gen2).

Operators reach gen-2 boxes over a plain self-hosted WireGuard overlay once
box ``:22`` locks down to the connector's Modal Proxy IPs. The peer list is
the ``[management_plane]`` table of the tier's committed ``deploy.toml`` (operator PUBLIC keys +
addresses; the private halves never leave their machines); each box's own key
material is generated at prep and its public half recorded on the
``bare_metal_servers`` row. ``config`` renders an operator's client config
from those rows; ``sync-peers`` converges the live fleet on the committed
peer list.

These commands address a TIER, not an env: the fleet they operate on is the
tier's box registry (the pool database at the tier's
``secrets/minds/<tier>/neon/DATABASE_URL`` Vault leaf -- the shared tiers'
single pool DB, the dev / ci tiers' standing "infra" registry). ``--tier``
names it explicitly; without the flag the activated env's tier is used.
"""

from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
import psycopg2
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.data_types import ManagementWireguardConfig
from imbue.minds.config.data_types import WireguardOperatorConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds.config.loader import load_deploy_config
from imbue.minds_admin.cli._tier_secrets import resolve_tier_pool_database_url
from imbue.minds_admin.cli.server import box_management_identities
from imbue.minds_admin.cli.server import run_outcome_workers_in_bounded_threads
from imbue.minds_admin.cli.server import run_root_script_over_ssh
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.minds_admin.slices.box_access import activated_management_tier_or_none
from imbue.minds_admin.slices.box_access import resolve_box_management_dial
from imbue.minds_admin.slices.management_plane import build_operator_wireguard_client_config
from imbue.minds_admin.slices.management_plane import render_wireguard_prep_section
from imbue.minds_admin.slices.onetun_install import OnetunInstallError
from imbue.minds_admin.slices.onetun_install import install_pinned_onetun
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.primitives import CI_TIER
from imbue.mngr_imbue_cloud.primitives import DEV_TIER
from imbue.mngr_imbue_cloud.primitives import PRODUCTION_TIER
from imbue.mngr_imbue_cloud.primitives import STAGING_TIER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION

MANAGEMENT_TIERS: Final[tuple[str, ...]] = (DEV_TIER, CI_TIER, STAGING_TIER, PRODUCTION_TIER)

_TIER_DATABASE_URL_HELP: Final[str] = (
    "Pool DSN of the tier's box registry. Optional: read from the tier's Vault entry "
    "(secrets/minds/<tier>/neon/DATABASE_URL) by default; pass explicitly only when overriding."
)

# Boxes are independent (each sync is one tunnel plus one SSH session to its own
# box), so the fan-out is bounded only to keep an operator laptop's open tunnels
# and SSH sessions reasonable on a large fleet.
_DEFAULT_SYNC_MAX_CONCURRENCY: Final[int] = 8


class PeerSyncOutcome(FrozenModel):
    """The result of converging one box's WireGuard peers on the committed operator list."""

    server_id: str = Field(description="The bare_metal_servers row id of the box")
    address: str = Field(
        description="The host the sync dialed: the local tunnel endpoint, else the box's overlay or public address"
    )
    is_synced: bool = Field(description="Whether the box's wg0.conf now carries the committed operator list")
    error: str | None = Field(default=None, description="Why the sync failed (None when it succeeded)")

    def to_report_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {"synced": self.is_synced, "address": self.address}
        if self.error is not None:
            entry["error"] = self.error
        return entry


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


def _tier_option(command: Callable[..., None]) -> Callable[..., None]:
    """The ``--tier`` option shared by the tier-addressed wireguard commands."""
    return click.option(
        "--tier",
        "explicit_tier",
        type=click.Choice(MANAGEMENT_TIERS),
        default=None,
        help=(
            "The tier whose fleet to operate on (its committed [management_plane] operator list, its box "
            "registry, and its SSH CA). Defaults to the activated env's tier, so no activation is needed "
            "when this is passed."
        ),
    )(command)


def resolve_management_tier(explicit_tier: str | None, activated_tier: str | None) -> str:
    """The tier a wireguard command addresses: the explicit flag, else the activated env's tier."""
    if explicit_tier is not None:
        return explicit_tier
    if activated_tier is not None:
        return activated_tier
    raise click.UsageError(
        f"no tier to operate on: pass --tier <{'|'.join(MANAGEMENT_TIERS)}>, or run inside an activated minds "
        'env (`eval "$(uv run minds-admin env activate <name>)"`)'
    )


def _require_management_plane_config(tier: str) -> ManagementPlaneConfig:
    """The tier's [management_plane] config, or a clear refusal when it has none."""
    management_plane_config = load_deploy_config(tier).management_plane
    if management_plane_config is None:
        raise click.ClickException(
            f"the {tier} tier's deploy.toml has no [management_plane] table; add one to "
            f"apps/minds/imbue/minds/config/envs/{tier}/deploy.toml (see the dev tier's file for the schema) "
            "and re-run"
        )
    return management_plane_config


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


def _fetch_gen2_wireguard_boxes(tier: str, database_url: str | None) -> list[BareMetalServer]:
    """Every gen-2 box a wireguard command can address: overlay address assigned, key recorded, reachable."""
    conn = psycopg2.connect(database_url if database_url else resolve_tier_pool_database_url(tier))
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
@_tier_option
@click.option(
    "--operator",
    "operator_name",
    default=None,
    help="Which [[management_plane.wireguard.operators]] entry to render for (defaults to the only one when unambiguous).",
)
@click.option("--database-url", default=None, help=_TIER_DATABASE_URL_HELP)
def wireguard_config(explicit_tier: str | None, operator_name: str | None, database_url: str | None) -> None:
    """Emit the operator's wg-quick client config for the tier's gen-2 fleet.

    One ``[Peer]`` per prepped gen-2 box (endpoint = its public address, allowed
    IPs = its overlay /32). The ``PrivateKey`` placeholder must be replaced with
    the operator's own private key -- it exists only on their machine.
    """
    tier = resolve_management_tier(explicit_tier, activated_management_tier_or_none())
    management_plane_config = _require_management_plane_config(tier)
    operator = _select_operator(management_plane_config, operator_name)
    boxes = _fetch_gen2_wireguard_boxes(tier, database_url)
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


def _sync_one_box_peers(
    *,
    box: BareMetalServer,
    tier: str,
    ssh_user: str,
    private_key_path: Path,
    sync_script: str,
) -> PeerSyncOutcome:
    """Push the rendered wg0 config to one box over its management dial.

    An SSH failure on the box becomes a failed outcome rather than an
    exception, so one unreachable box never aborts the rest of the fan-out.
    Resolving the dial itself (the operator key, the onetun spawn) is not
    guarded: a failure there is the operator machine's, not one box's, and
    aborting the whole run is the right response.
    """
    target_dial = resolve_box_management_dial(
        public_address=str(box.public_address),
        wireguard_address=box.wireguard_address,
        wireguard_public_key=box.wireguard_public_key,
        tier=tier,
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
    except BareMetalProvisioningError as exc:
        logger.warning("Peer sync on box {} ({}) failed: {}", box.id, target_dial.host, exc)
        return PeerSyncOutcome(server_id=str(box.id), address=target_dial.host, is_synced=False, error=str(exc))
    return PeerSyncOutcome(server_id=str(box.id), address=target_dial.host, is_synced=True)


@pure
def _render_box_peer_sync_script(
    box: BareMetalServer, wireguard_settings: ManagementWireguardConfig, overlay_prefix_length: int
) -> str:
    return "#!/bin/bash\nset -euo pipefail\n" + render_wireguard_prep_section(
        # Non-None by _fetch_gen2_wireguard_boxes's filter; str() for the type checker.
        wireguard_address=str(box.wireguard_address),
        listen_port=int(wireguard_settings.listen_port),
        operators=wireguard_settings.operators,
        overlay_prefix_length=overlay_prefix_length,
    )


@pure
def build_peer_sync_report(
    tier: str, operators: Sequence[WireguardOperatorConfig], outcomes: Sequence[PeerSyncOutcome]
) -> dict[str, object]:
    """The JSON the command emits: per-box outcomes keyed by row id, in the order given (``error`` only on failures)."""
    return {
        "tier": tier,
        "operators": [str(operator.name) for operator in operators],
        "boxes": {outcome.server_id: outcome.to_report_entry() for outcome in outcomes},
    }


def _describe_peer_sync_outcome(outcome: PeerSyncOutcome) -> str:
    return f"{outcome.server_id} {'synced' if outcome.is_synced else 'FAILED'}"


@wireguard.command(name="sync-peers")
@_tier_option
@click.option("--database-url", default=None, help=_TIER_DATABASE_URL_HELP)
@click.option("--ssh-user", default="debian", help="Management SSH user on the box (the OS image's sudo user).")
@click.option(
    "--max-concurrency",
    type=click.IntRange(min=1),
    default=_DEFAULT_SYNC_MAX_CONCURRENCY,
    show_default=True,
    help="How many boxes to sync at once (each sync is its own tunnel + SSH session).",
)
def wireguard_sync_peers(
    explicit_tier: str | None, database_url: str | None, ssh_user: str, max_concurrency: int
) -> None:
    """Converge every prepped gen-2 box's WireGuard peers on the committed [management_plane] operator list.

    Idempotent: re-renders each box's ``wg0.conf`` from the committed operator
    list and restarts the interface only when it actually changed (a live
    operator session over an unchanged config is never bounced). Boxes are
    synced in parallel with per-box error capture; exits 1 if any box could not
    be synced (re-run until clean).
    """
    tier = resolve_management_tier(explicit_tier, activated_management_tier_or_none())
    management_plane_config = _require_management_plane_config(tier)
    allocation = management_overlay_for_tier(tier)
    wireguard_settings = management_plane_config.wireguard
    boxes = _fetch_gen2_wireguard_boxes(tier, database_url)
    if not boxes:
        write_human_line("No prepped gen-2 boxes to sync.")
        return
    with box_management_identities(tier=tier) as identities:
        outcomes = run_outcome_workers_in_bounded_threads(
            worker=_sync_one_box_peers,
            worker_kwargs_list=[
                dict(
                    box=box,
                    tier=tier,
                    ssh_user=ssh_user,
                    private_key_path=identities.private_key_path_for(box.box_generation),
                    sync_script=_render_box_peer_sync_script(box, wireguard_settings, allocation.overlay.prefixlen),
                )
                for box in boxes
            ],
            max_concurrency=max_concurrency,
            thread_name_prefix="peer-sync",
            progress_noun="Peer sync",
            describe_outcome=_describe_peer_sync_outcome,
            interruption_exception_types=(),
            on_join_interrupted=None,
        )
    outcome_by_server_id = {outcome.server_id: outcome for outcome in outcomes}
    ordered_outcomes = [outcome_by_server_id[str(box.id)] for box in boxes]
    emit_json(build_peer_sync_report(tier, wireguard_settings.operators, ordered_outcomes))
    if any(not outcome.is_synced for outcome in ordered_outcomes):
        raise SystemExit(1)
