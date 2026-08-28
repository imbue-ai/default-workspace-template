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

import click

from imbue.minds_admin.cli._tier_secrets import make_admin_connector_client
from imbue.minds_admin.cli.paid import paid_auth_options
from imbue.minds_admin.cli.paid import resolve_admin_api_key
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors


@click.group(name="workspaces")
def workspaces_admin() -> None:
    """Operator workspace-lifecycle management (requires MINDS_ADMIN_KEY)."""


@workspaces_admin.command(name="stop")
@click.argument("host_db_id")
@paid_auth_options
@handle_imbue_cloud_errors
def admin_stop_workspace(host_db_id: str, connector_url: str | None, api_key: str | None) -> None:
    """Force-stop the workspace HOST_DB_ID (halt VM, upload artifact, free the slot).

    The same data-preserving transition the owner's stop runs, without the
    ownership check: used for suspensions, migrations, and clearing a box.
    Idempotent -- a workspace already stopping/stopped reports its status.
    """
    client = make_admin_connector_client(connector_url)
    emit_json(client.admin_stop_workspace(resolve_admin_api_key(api_key), host_db_id))


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
