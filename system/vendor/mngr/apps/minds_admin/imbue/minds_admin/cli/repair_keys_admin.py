"""``minds-admin repair-keys`` -- operator sweep for the slice authorized_keys wipe.

Fleet mode sweeps every slice VM on every box (see
``imbue.minds_admin.slices.key_repair``); ``--server-id`` / ``--vm-name``
scope it down to one box or one VM (the break-glass mode). The pool SSH key
and pool DSN resolve from the activated env, exactly like the other
``minds-admin server`` fleet commands.
"""

import click
import psycopg2
from loguru import logger

from imbue.minds_admin.cli._tier_secrets import DATABASE_URL_HELP
from imbue.minds_admin.cli._tier_secrets import resolve_pool_database_url
from imbue.minds_admin.cli._tier_secrets import resolve_pool_private_key_pem
from imbue.minds_admin.cli.server import pool_private_key_path
from imbue.minds_admin.slices.bare_metal_db import fetch_server_capacities
from imbue.minds_admin.slices.key_repair import SliceKeyRepairOutcome
from imbue.minds_admin.slices.key_repair import build_key_repair_report
from imbue.minds_admin.slices.key_repair import repair_slice_keys_on_box
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_lima.errors import LimaCommandError


@click.command(name="repair-keys")
@click.option("--database-url", default=None, help=DATABASE_URL_HELP)
@click.option(
    "--server-id",
    "server_ids",
    multiple=True,
    help="Restrict the sweep to these bare_metal_servers row ids (repeatable; default: every box).",
)
@click.option(
    "--vm-name",
    "vm_names",
    multiple=True,
    help="Restrict the sweep to these lima instance names (repeatable; the single-VM break-glass mode).",
)
@click.option(
    "--dry-run",
    "is_dry_run",
    is_flag=True,
    default=False,
    help="List the slice VMs that would be repaired (and whether their lima.yaml needs the patch).",
)
def repair_keys(
    database_url: str | None, server_ids: tuple[str, ...], vm_names: tuple[str, ...], is_dry_run: bool
) -> None:
    """Repair slice VMs hit by the cidata authorized_keys wipe, fleet-wide.

    For every slice VM: patch its stored lima.yaml provision block so restarts
    stop truncating the VM root's authorized_keys, and restore any key lines
    the wipe dropped by copying the workspace container's own authorized_keys
    upward (the owner's key survived there). Copies only -- the sweep never
    injects key material of its own, so it can restore access the owner
    already had but never mint anything new. Safe to re-run; the pool SSH key
    resolves from the activated tier's Vault entry (or $POOL_SSH_PRIVATE_KEY).
    """
    conn = psycopg2.connect(resolve_pool_database_url(database_url))
    try:
        capacities = fetch_server_capacities(conn)
    finally:
        conn.close()
    selected = [c.server for c in capacities if not server_ids or str(c.server.id) in set(server_ids)]
    if server_ids:
        unknown_ids = set(server_ids) - {str(s.id) for s in selected}
        if unknown_ids:
            raise click.ClickException(f"Unknown --server-id value(s): {sorted(unknown_ids)}")
    only_vm_names = frozenset(vm_names) if vm_names else None
    outcomes: list[SliceKeyRepairOutcome] = []
    unreadable_boxes: list[str] = []
    with pool_private_key_path(resolve_pool_private_key_pem()) as private_key_path:
        for box_server in selected:
            if not box_server.public_address:
                logger.warning("Box {} has no public_address; skipping (state unknown)", box_server.id)
                unreadable_boxes.append(str(box_server.id))
                continue
            client = LimaSliceVpsClient(
                box_address=str(box_server.public_address),
                box_ssh_user=box_server.lima_service_user or "limahost",
                private_key_path=str(private_key_path),
                box_host_public_key=box_server.box_host_public_key,
            )
            # One unreachable box must not cost the rest of the fleet its sweep
            # (the same resilience rule as the occupancy audit).
            try:
                outcomes.extend(
                    repair_slice_keys_on_box(
                        client, str(box_server.id), is_dry_run=is_dry_run, only_vm_names=only_vm_names
                    )
                )
            except (LimaCommandError, BareMetalProvisioningError, OSError) as exc:
                logger.warning("Could not sweep box {} ({}): {}", box_server.id, box_server.public_address, exc)
                unreadable_boxes.append(str(box_server.id))
    report = build_key_repair_report(outcomes, unreadable_boxes)
    emit_json(report.model_dump(mode="json"))
    if report.failed or report.unreadable_boxes:
        raise click.ClickException(
            f"{report.failed} VM(s) failed to repair and {len(report.unreadable_boxes)} box(es) were "
            "unreachable; see the JSON above and re-run against them once fixed."
        )
