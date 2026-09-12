"""``minds-admin repair-home-layout`` -- move slow-path-rebuilt slice workspaces onto the ``home/`` volume layout.

Targets are leased slice pool hosts named by ``--host-id`` (or, with
``--all-leased``, every leased slice in the pool, probe only); each is probed
(the default), migrated, or rolled back through one script run as root in its
VM (see ``imbue.minds_admin.slices.home_layout``). Gen-1 boxes only: the
script runs through the lima client's ``run_in_vm_as_root``, so a target on a
gen-2 box is skipped and reported as unreachable. The pool SSH key and pool
DSN resolve from the activated env, exactly like ``repair-keys``.
"""

import click
import psycopg2
from loguru import logger

from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.slices.bare_metal_db import POOL_HOST_STATUS_LEASED
from imbue.minds_admin.slices.bare_metal_db import fetch_leased_slice_hosts
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_hosts_by_host_id
from imbue.minds_admin.slices.box_access import resolve_server_management_dial
from imbue.minds_admin.slices.home_layout import HomeLayoutAction
from imbue.minds_admin.slices.home_layout import HomeLayoutOutcome
from imbue.minds_admin.slices.home_layout import HomeLayoutTarget
from imbue.minds_admin.slices.home_layout import build_home_layout_report
from imbue.minds_admin.slices.home_layout import repair_home_layout_on_target
from imbue.minds_admin.slices.operator_identity import pool_private_key_path
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.bare_metal import box_service_user
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_lima.errors import LimaCommandError


@click.command(name="repair-home-layout")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option(
    "--host-id",
    "host_ids",
    multiple=True,
    help="pool_hosts.host_id of a leased slice workspace to act on (repeatable).",
)
@click.option(
    "--all-leased",
    "is_all_leased",
    is_flag=True,
    default=False,
    help="Probe every leased slice in the pool instead of named hosts (probe only; how the affected set is found).",
)
@click.option(
    "--migrate",
    "is_migrate",
    is_flag=True,
    default=False,
    help="Move each legacy-layout workspace onto the home/ layout (default: probe only, nothing changes).",
)
@click.option(
    "--rollback",
    "is_rollback",
    is_flag=True,
    default=False,
    help="Undo a migration this command made while its aside copy and rollback snapshot still exist.",
)
def repair_home_layout(
    database_url: str | None, host_ids: tuple[str, ...], is_all_leased: bool, is_migrate: bool, is_rollback: bool
) -> None:
    """Probe, migrate, or roll back the home-tree layout of leased slice workspaces.

    A workspace the imbue_cloud slow path rebuilt on the legacy layout keeps
    /home/user (the workspace checkout, apps, skills, dotfiles) in the
    container's writable layer, where neither the volume nor host_backup
    covers it. --migrate copies that tree onto the volume as home/, moves the
    mngr host_dir under it, and symlinks /home/user onto it -- the layout a
    bake produces -- after stopping the workspace's chats and services for the
    duration (minutes). A read-only btrfs snapshot of the volume and the
    container's old home tree are kept for --rollback. Without a flag the
    command only reports each workspace's layout. Gen-1 boxes only: a target
    on a gen-2 box is skipped (the repair runs through the lima client) and
    counted as unreachable in the report.
    """
    if is_migrate and is_rollback:
        raise click.ClickException("--migrate and --rollback are mutually exclusive")
    if is_all_leased and (is_migrate or is_rollback):
        raise click.ClickException(
            "--all-leased only probes; name the hosts to --migrate or --rollback with --host-id"
        )
    if is_all_leased == bool(host_ids):
        raise click.ClickException("Pass either --host-id (repeatable) or --all-leased")
    action = (
        HomeLayoutAction.MIGRATE
        if is_migrate
        else HomeLayoutAction.ROLLBACK
        if is_rollback
        else HomeLayoutAction.PROBE
    )
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        rows = fetch_leased_slice_hosts(conn) if is_all_leased else fetch_slice_hosts_by_host_id(conn, host_ids)
    finally:
        conn.close()
    unknown_ids = set(host_ids) - {row.host_id for row in rows}
    if unknown_ids:
        raise click.ClickException(
            f"Not leased slice pool hosts (unknown, or VPS rows this command does not handle): {sorted(unknown_ids)}"
        )
    targets = [
        HomeLayoutTarget(
            host_id=row.host_id,
            host_name=row.host_name,
            status=row.status,
            vm_name=row.slice_instance_name,
            server=row.server,
        )
        for row in rows
    ]
    outcomes: list[HomeLayoutOutcome] = []
    unreachable: list[str] = []
    with pool_private_key_path(resolve_pool_private_key_pem()) as private_key_path:
        for target in targets:
            if target.status != POOL_HOST_STATUS_LEASED:
                logger.warning(
                    "Skipping {} ({}): status is {}, and only a running (leased) VM can be repaired",
                    target.host_name,
                    target.host_id,
                    target.status,
                )
                unreachable.append(target.host_id)
                continue
            if not target.server.public_address:
                logger.warning("Box {} has no public_address; skipping {}", target.server.id, target.host_id)
                unreachable.append(target.host_id)
                continue
            if target.server.box_generation >= FIRST_QEMU_BOX_GENERATION:
                # The in-VM script runs through the lima client's
                # ``run_in_vm_as_root`` (``limactl shell``), which a raw-qemu
                # slice has no counterpart for.
                logger.warning(
                    "Skipping {} ({}): its box {} is gen-2, which this lima-based repair cannot reach",
                    target.host_name,
                    target.host_id,
                    target.server.id,
                )
                unreachable.append(target.host_id)
                continue
            dial = resolve_server_management_dial(target.server)
            client = LimaSliceVpsClient(
                box_address=dial.host,
                box_ssh_port=dial.port,
                box_ssh_user=box_service_user(target.server),
                private_key_path=str(private_key_path),
                box_host_public_key=target.server.box_host_public_key,
            )
            # One unreachable box must not cost the other targets their repair.
            try:
                outcomes.append(repair_home_layout_on_target(client, target, action))
            except (LimaCommandError, BareMetalProvisioningError, OSError) as exc:
                logger.warning("Could not reach {} on box {}: {}", target.host_name, target.server.public_address, exc)
                unreachable.append(target.host_id)
    report = build_home_layout_report(outcomes, unreachable)
    emit_json(report.model_dump(mode="json"))
    if report.failed or report.unreachable:
        raise click.ClickException(
            f"{report.failed} workspace(s) failed and {len(report.unreachable)} were unreachable, not leased, or on "
            "a gen-2 box; see the JSON above."
        )
