"""``minds-admin workspaces ...`` -- operator workspace-lifecycle escape hatches.

Authenticated with the fixed ``MINDS_ADMIN_KEY`` API key, like the paid-list
CRUD. ``abandon`` marks a workspace ``crashed`` -- the lever for a row whose
box is permanently dead and whose stop/start transition would otherwise
retry forever. The user recovers by restoring the workspace's backup into a
fresh workspace; artifacts and any surviving VM are reclaimed at release.
``release`` retires a confirmed-abandoned lease through the connector's own
release chain (artifacts, slice VM, workspace record, row), in any lifecycle
status -- the operator counterpart of the user's destroy.
"""

from typing import Final

import click

from imbue.minds_admin.cli._tier_secrets import make_admin_connector_client
from imbue.minds_admin.cli.paid import paid_auth_options
from imbue.minds_admin.cli.paid import resolve_admin_api_key
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind

# The kinds an operator may stamp (specs/workspace-stop-kinds.md); ``owner`` is
# the owner route's alone.
_OPERATOR_STOP_KINDS: Final[tuple[str, ...]] = tuple(
    kind.value
    for kind in (
        WorkspaceStopKind.MAINTENANCE,
        WorkspaceStopKind.IDLE,
        WorkspaceStopKind.SUSPENSION,
        WorkspaceStopKind.RETIRED,
    )
)


@click.group(name="workspaces")
def workspaces_admin() -> None:
    """Operator workspace-lifecycle management (requires MINDS_ADMIN_KEY)."""


@workspaces_admin.command(name="stop")
@click.argument("host_db_id")
@click.option(
    "--kind",
    "kind",
    type=click.Choice(_OPERATOR_STOP_KINDS),
    required=True,
    help=(
        "Why the machine is stopped, which decides who may start it: 'maintenance' is a hold only an "
        "operator start (or 'set-stop-kind idle') ends; 'idle' frees capacity and the user may start it; "
        "'suspension' is the suspend fan-out's kind; 'retired' is final (see `workspaces retire`)."
    ),
)
@paid_auth_options
@handle_imbue_cloud_errors
def admin_stop_workspace(host_db_id: str, kind: str, connector_url: str | None, api_key: str | None) -> None:
    """Force-stop the workspace HOST_DB_ID (halt VM, upload artifact, free the slot) with a stop kind.

    The same data-preserving transition the owner's stop runs, without the
    ownership check: used for suspensions, migrations, and clearing a box.
    Idempotent on the transition -- a workspace already stopping/stopped
    reports its status -- but the kind is always stamped, so an owner-stopped
    workspace takes the hold too.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_stop_workspace(resolve_admin_api_key(api_key), host_db_id, WorkspaceStopKind(kind)))


@workspaces_admin.command(name="retire")
@click.argument("host_db_id")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_retire_workspace(host_db_id: str, connector_url: str | None, api_key: str | None) -> None:
    """Stop the workspace HOST_DB_ID for good: nobody starts it again, its owner included.

    The ``retired`` stop kind for a workspace the gen-2 migration cannot take
    (below the cutover's version floor, or without a release tag), taken
    only after `minds-admin archives create` has archived it and the archive
    has been checked. The owner's start answers 409 workspace_retired (the
    desktop points them at their backups); the operator start refuses it
    too -- `set-stop-kind <id> idle` first is the deliberate way back. Once
    the owner is confirmed covered, `workspaces release` frees the row.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_stop_workspace(resolve_admin_api_key(api_key), host_db_id, WorkspaceStopKind.RETIRED))


@workspaces_admin.command(name="set-stop-kind")
@click.argument("host_db_id")
@click.argument("kind", type=click.Choice(_OPERATOR_STOP_KINDS))
@paid_auth_options
@handle_imbue_cloud_errors
def admin_set_workspace_stop_kind(host_db_id: str, kind: str, connector_url: str | None, api_key: str | None) -> None:
    """Change the kind of the stopping/stopped workspace HOST_DB_ID's stop.

    'idle' hands a held workspace back to its owner without a rollback (a
    row the cutover has parked stays refused by the connector's parked-row
    guard and needs `cutover rollback`); 'maintenance' holds a workspace the
    owner stopped. Refused (409) for a
    running workspace, which has no stop to describe.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(
        client.admin_set_workspace_stop_kind(resolve_admin_api_key(api_key), host_db_id, WorkspaceStopKind(kind))
    )


@workspaces_admin.command(name="start")
@click.argument("host_db_id")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_start_workspace(host_db_id: str, connector_url: str | None, api_key: str | None) -> None:
    """Start the stopped workspace HOST_DB_ID regardless of owner (no quota check).

    The owner's start transition without the ownership and quota checks: the
    operator is bringing back a workspace its user already had running. Used
    by the gen-2 cutover runbook to start every stopped gen-1 workspace before
    the window so the drain can harvest it live. Idempotent -- a workspace
    already running/starting reports its status; a row the cutover has parked
    is refused as under maintenance, a retired one as retired. Ignores a
    maintenance / suspension kind (this is how a held workspace comes back)
    and clears it.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_start_workspace(resolve_admin_api_key(api_key), host_db_id))


@workspaces_admin.command(name="release")
@click.argument("host_db_id")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_release_workspace(host_db_id: str, connector_url: str | None, api_key: str | None) -> None:
    """Release the workspace HOST_DB_ID regardless of owner: artifacts, slice VM, workspace record, row.

    The user's exact destroy chain run by the operator, for a lease its owner
    confirmed abandoned (or can no longer reach). Works on any lifecycle status
    -- including ``stopped`` rows, which ``pool destroy`` cannot claim -- and
    is idempotent: an already-gone row reports ``already_released``.
    """
    client = make_admin_connector_client(connector_url)
    status = client.admin_release_workspace(resolve_admin_api_key(api_key), host_db_id)
    emit_json({"host_db_id": host_db_id, "status": status})


@workspaces_admin.command(name="abandon")
@click.argument("host_db_id")
@click.option("--reason", required=True, help="Why the workspace is being abandoned (recorded on the row)")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_abandon_workspace(host_db_id: str, reason: str, connector_url: str | None, api_key: str | None) -> None:
    """Mark the workspace HOST_DB_ID crashed (its box is permanently dead)."""
    client = make_admin_connector_client(connector_url)
    client.admin_abandon_workspace(resolve_admin_api_key(api_key), host_db_id, reason)
    emit_json({"host_db_id": host_db_id, "status": "crashed", "reason": reason})
