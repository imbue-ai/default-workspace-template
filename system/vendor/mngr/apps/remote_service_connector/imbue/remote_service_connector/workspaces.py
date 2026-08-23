"""Workspace lifecycle endpoints: list/get across all states, stop, start, abandon.

``GET /hosts`` (the deprecated leased-only listing) stays for released
clients; these endpoints are the full-lifecycle replacement. A workspace is
a ``pool_hosts`` row leased to the caller, in one of:

* ``running``  -- the VM is up (DB status ``leased``)
* ``stopping`` -- VM halted, upload in flight
* ``stopped``  -- artifact in object storage; the halted local VM (and the
  bare-metal slot) is kept for the retention window so a start within it
  restarts in place, then reaped
* ``starting`` -- a supervisor is restoring/booting it
* ``crashed``  -- operator-abandoned (recover from the workspace backup)

Stop/start are asynchronous: they CAS the row into the transition status
(minting the fencing ``transition_id`` the spawned supervisor owns), spawn a
supervisor, and return 202; clients poll ``GET /workspaces/{id}``. Transitions
only begin from the stable states (``leased`` for stop, ``stopped`` for
start); a request against a row mid-transition is answered 409 with the
current status so the caller re-reads state and retries when it settles.
"""

import logging
from typing import Any
from typing import Final
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector import stop_start
from imbue.remote_service_connector import storage
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.hosts import COUNT_RUNNING_WORKSPACES_SQL
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()

# DB status -> wire status. ``leased`` is a leasing-internal term; the
# workspace API speaks the user-facing lifecycle vocabulary.
_WIRE_STATUS_BY_DB_STATUS: Final[dict[str, str]] = {
    "leased": "running",
    "stopping": "stopping",
    "stopped": "stopped",
    "starting": "starting",
    "crashed": "crashed",
}

_WORKSPACE_STATUSES_SQL: Final[str] = "('leased', 'stopping', 'stopped', 'starting', 'crashed')"

_WORKSPACE_SELECT_COLUMNS: Final[str] = (
    "id, status, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, host_name, "
    "attributes, leased_at, stop_requested_at, stopped_at, transition_error, "
    "outer_host_public_key, container_host_public_key"
)

# The stop CAS, shared by the owner route, the operator route, and the account
# suspension fan-out so all three run the exact same transition. Parameters:
# (transition_id, host_db_id) -- the minted transition id fences out any
# supervisor from an earlier transition.
_STOP_LEASED_WORKSPACE_SQL: Final[str] = (
    "UPDATE pool_hosts SET status = 'stopping', stop_requested_at = NOW(), "
    "transition_error = NULL, transition_failure_count = 0, transition_id = %s, "
    "transition_heartbeat_at = NOW() "
    "WHERE id = %s AND status = 'leased'"
)


class WorkspaceInfo(BaseModel):
    """One workspace row in its full lifecycle form.

    Placement fields (``vps_address`` and the two ports) stay set on a
    just-stopped workspace through the retention window (its halted local VM
    is kept for a restart in place) and are None once the retention finalize
    frees the slot -- the VM then exists only as encrypted objects in the
    tier's storage bucket.
    """

    host_db_id: UUID = Field(description="Durable workspace identity (the pool_hosts row id)")
    status: str = Field(description="Lifecycle status: running/stopping/stopped/starting/crashed")
    vps_address: str | None = Field(description="Box address (None once fully stopped; see class docstring)")
    ssh_port: int | None = Field(description="VM-root forwarded port (None once fully stopped; see class docstring)")
    ssh_user: str = Field(description="SSH user on the VM")
    container_ssh_port: int | None = Field(
        description="Container forwarded port (None once fully stopped; see class docstring)"
    )
    agent_id: str = Field(description="Pre-provisioned mngr agent id")
    host_id: str = Field(description="mngr host id (host-<32hex>)")
    host_name: str = Field(description="User-chosen friendly name")
    attributes: dict[str, Any] = Field(description="Lease attributes")
    leased_at: str = Field(description="ISO 8601 lease timestamp")
    stop_requested_at: str | None = Field(default=None, description="When the current/last stop was requested")
    stopped_at: str | None = Field(default=None, description="When the workspace reached stopped")
    transition_error: str | None = Field(default=None, description="Last stop/start failure, if any")
    outer_host_public_key: str | None = Field(default=None, description="Pinned VM-root sshd host key")
    container_host_public_key: str | None = Field(default=None, description="Pinned container sshd host key")


class TransitionResponse(BaseModel):
    """Response to a stop/start request: the workspace's (possibly unchanged) status."""

    host_db_id: UUID = Field(description="The workspace the transition applies to")
    status: str = Field(description="Wire lifecycle status after the request")


class AbandonWorkspaceRequest(BaseModel):
    reason: str = Field(description="Operator-facing reason recorded on the row")


def _workspace_info_from_row(row: tuple[Any, ...]) -> WorkspaceInfo:
    return WorkspaceInfo(
        host_db_id=row[0],
        status=_WIRE_STATUS_BY_DB_STATUS.get(row[1], row[1]),
        vps_address=row[2],
        ssh_port=row[3],
        ssh_user=row[4] or "root",
        container_ssh_port=row[5],
        agent_id=row[6],
        host_id=row[7],
        host_name=row[8],
        attributes=row[9] if isinstance(row[9], dict) else {},
        leased_at=str(row[10]) if row[10] is not None else "",
        stop_requested_at=str(row[11]) if row[11] is not None else None,
        stopped_at=str(row[12]) if row[12] is not None else None,
        transition_error=row[13],
        outer_host_public_key=row[14],
        container_host_public_key=row[15],
    )


@router.get("/workspaces")
def list_workspaces(request: Request) -> list[dict[str, object]]:
    """List every workspace the caller owns, in all lifecycle states."""
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_WORKSPACE_SELECT_COLUMNS} FROM pool_hosts "
                    f"WHERE leased_to_user = %s AND status IN {_WORKSPACE_STATUSES_SQL} "
                    "ORDER BY leased_at",
                    (user.user_id_prefix,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_workspace_info_from_row(row).model_dump(mode="json") for row in rows]


def _read_owned_workspace(conn: Any, host_db_id: UUID, user_id_prefix: str) -> tuple[Any, ...]:
    """Read one workspace row, enforcing ownership (404 unknown, 403 not owner)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT leased_to_user, {_WORKSPACE_SELECT_COLUMNS} FROM pool_hosts WHERE id = %s",
            (str(host_db_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such workspace")
    if row[0] != user_id_prefix:
        raise HTTPException(status_code=403, detail="You do not own this workspace")
    return row[1:]


@router.get("/workspaces/{host_db_id}")
def get_workspace(request: Request, host_db_id: UUID) -> dict[str, object]:
    """One workspace's full lifecycle view (the poll target during stop/start)."""
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        conn = db.get_pool_db_connection()
        try:
            row = _read_owned_workspace(conn, host_db_id, user.user_id_prefix)
        finally:
            conn.close()
        return _workspace_info_from_row(row).model_dump(mode="json")


def _apply_stop_preconditions(host_db_id: UUID, current_db_status: str) -> dict[str, object] | None:
    """Apply the stop preconditions shared by the owner and operator stop routes.

    Returns the idempotent response payload for a workspace that is already
    ``stopping``/``stopped`` (nothing to do), None when the row is ``leased``
    and the caller should run the stop CAS, and raises 409 for every other
    lifecycle state (a ``starting`` or ``crashed`` row cannot be stopped).
    """
    if current_db_status in ("stopping", "stopped"):
        return TransitionResponse(
            host_db_id=host_db_id, status=_WIRE_STATUS_BY_DB_STATUS[current_db_status]
        ).model_dump(mode="json")
    if current_db_status != "leased":
        raise HTTPException(
            status_code=409,
            detail=f"Workspace is {_WIRE_STATUS_BY_DB_STATUS.get(current_db_status, current_db_status)}"
            " and cannot be stopped right now",
        )
    return None


@router.post("/workspaces/{host_db_id}/stop", status_code=202)
def stop_workspace(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Begin stopping a running workspace: halt its VM and upload it (slot freed after retention).

    Asynchronous: CAS ``leased -> stopping`` (minting the fencing
    ``transition_id`` the spawned supervisor owns), spawn the transition
    supervisor, and return immediately. Idempotent: a workspace already
    stopping/stopped reports its current status. Stop is always allowed for
    a running workspace (it frees a running-quota slot; the stopped
    workspace was already counted by max_total_workspaces at create time).
    """
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        storage.read_storage_config()
        transition_id = str(uuid4())
        conn = db.get_pool_db_connection()
        try:
            row = _read_owned_workspace(conn, host_db_id, user.user_id_prefix)
            already_stopped_response = _apply_stop_preconditions(host_db_id, str(row[1]))
            if already_stopped_response is not None:
                return already_stopped_response
            with conn.cursor() as cur:
                cur.execute(_STOP_LEASED_WORKSPACE_SQL, (transition_id, str(host_db_id)))
                updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if updated == 0:
            # Lost a race with another request; report whatever won.
            return get_workspace(request, host_db_id)
        stop_start.spawn_supervisor(str(host_db_id), transition_id)
        return TransitionResponse(host_db_id=host_db_id, status="stopping").model_dump(mode="json")


@router.post("/workspaces/{host_db_id}/start", status_code=202)
def start_workspace(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Begin starting a stopped workspace.

    Asynchronous: CAS ``stopped -> starting`` (minting the fencing
    ``transition_id`` the spawned supervisor owns), spawn the supervisor
    (which restarts in place when the VM is still on its origin box, or
    restores from the artifact otherwise), and return immediately. A start
    re-occupies a running-workspace slot, so it checks
    ``max_remote_workspaces`` under the same per-user lock the lease path
    uses.

    Only ``stopped`` rows are startable: a still-``stopping`` row is
    mid-upload with its own supervisor driving it, so a start is refused
    (409) -- the caller waits for ``stopped`` and retries, keeping stop and
    start supervisors from ever running concurrently.
    """
    with handle_endpoint_errors():
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        storage.read_storage_config()
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        transition_id = str(uuid4())
        conn = db.get_pool_db_connection()
        try:
            row = _read_owned_workspace(conn, host_db_id, user.user_id_prefix)
            current_db_status = row[1]
            if current_db_status in ("leased", "starting"):
                return TransitionResponse(
                    host_db_id=host_db_id, status=_WIRE_STATUS_BY_DB_STATUS[current_db_status]
                ).model_dump(mode="json")
            if current_db_status != "stopped":
                # Waiting only helps mid-stop: a crashed or removing row will
                # never reach stopped, so it gets the plain refusal.
                retry_advice = "; wait for it to reach stopped and retry" if current_db_status == "stopping" else ""
                raise HTTPException(
                    status_code=409,
                    detail=f"Workspace is {_WIRE_STATUS_BY_DB_STATUS.get(current_db_status, current_db_status)}"
                    f" and cannot be started right now{retry_advice}",
                )
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user.user_id_prefix,))
                    cur.execute(COUNT_RUNNING_WORKSPACES_SQL, (user.user_id_prefix,))
                    count_row = cur.fetchone()
                    running_count = int(count_row[0]) if count_row is not None else 0
                    if running_count >= entitlements.max_remote_workspaces:
                        raise_quota_exceeded(
                            "max_remote_workspaces",
                            entitlements.max_remote_workspaces,
                            running_count,
                            "running workspaces",
                        )
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'starting', transition_error = NULL, "
                        "transition_failure_count = 0, transition_id = %s, transition_heartbeat_at = NOW() "
                        "WHERE id = %s AND status = 'stopped'",
                        (transition_id, str(host_db_id)),
                    )
                    updated = cur.rowcount
        finally:
            conn.close()
        if updated == 0:
            return get_workspace(request, host_db_id)
        stop_start.spawn_supervisor(str(host_db_id), transition_id)
        return TransitionResponse(host_db_id=host_db_id, status="starting").model_dump(mode="json")


@router.post("/admin/workspaces/{host_db_id}/stop", status_code=202)
def admin_stop_workspace(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Operator force-stop of one workspace, regardless of owner.

    The same transition the owner's ``POST /workspaces/{id}/stop`` runs (halt
    the VM, upload the artifact, free the slot -- data-preserving and
    restartable), minus the ownership check: used by account suspension,
    migrations, and future idle shutdown. Idempotent like the owner route.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        storage.read_storage_config()
        transition_id = str(uuid4())
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM pool_hosts WHERE id = %s", (str(host_db_id),))
                status_row = cur.fetchone()
            if status_row is None:
                raise HTTPException(status_code=404, detail="No such workspace")
            already_stopped_response = _apply_stop_preconditions(host_db_id, str(status_row[0]))
            if already_stopped_response is not None:
                return already_stopped_response
            with conn.cursor() as cur:
                cur.execute(_STOP_LEASED_WORKSPACE_SQL, (transition_id, str(host_db_id)))
                updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if updated == 0:
            raise HTTPException(status_code=409, detail="Workspace changed state concurrently; retry")
        stop_start.spawn_supervisor(str(host_db_id), transition_id)
        logger.info("Workspace %s force-stopped by operator", host_db_id)
        return TransitionResponse(host_db_id=host_db_id, status="stopping").model_dump(mode="json")


def begin_stopping_all_leased_workspaces(user_id_prefix: str) -> dict[str, object]:
    """CAS every leased workspace of one user into ``stopping`` and spawn supervisors.

    The suspend fan-out's workspace step. Returns a summary of what happened
    per lifecycle state; rows in ``starting`` cannot be stopped mid-transition,
    so they make the step report ``status: error`` (driving the fan-out's
    ``partial`` verdict) until a re-run catches them once they reach
    ``leased``. Raises ``MissingStorageConfigError`` before touching anything
    when the deployment cannot run stop transitions at all.
    """
    storage.read_storage_config()
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_WORKSPACE_SELECT_COLUMNS} FROM pool_hosts "
                f"WHERE leased_to_user = %s AND status IN {_WORKSPACE_STATUSES_SQL} "
                "ORDER BY leased_at",
                (user_id_prefix,),
            )
            rows = cur.fetchall()
        stopping_transitions: list[tuple[str, str]] = []
        starting_ids: list[str] = []
        already_inactive_count = 0
        for row in rows:
            host_db_id = str(row[0])
            row_status = str(row[1])
            if row_status == "leased":
                transition_id = str(uuid4())
                with conn.cursor() as cur:
                    cur.execute(_STOP_LEASED_WORKSPACE_SQL, (transition_id, host_db_id))
                    if cur.rowcount:
                        stopping_transitions.append((host_db_id, transition_id))
                conn.commit()
            elif row_status == "starting":
                starting_ids.append(host_db_id)
            else:
                already_inactive_count += 1
    finally:
        conn.close()
    for host_db_id, transition_id in stopping_transitions:
        stop_start.spawn_supervisor(host_db_id, transition_id)
    stopping_ids = [host_db_id for host_db_id, _transition_id in stopping_transitions]
    result: dict[str, object] = {
        "stopping": stopping_ids,
        "still_starting": starting_ids,
        # Rows already in stopping/stopped/crashed: nothing to do for them.
        "already_inactive": already_inactive_count,
    }
    if starting_ids:
        # A starting row finishes into ``leased`` and then runs under the
        # suspension; only a re-run stops it, so the step must not read as
        # converged.
        result["status"] = "error"
        result["error"] = f"{len(starting_ids)} workspace(s) still starting; re-run suspend once they finish"
    return result


@router.post("/admin/workspaces/{host_db_id}/abandon")
def abandon_workspace(request: Request, host_db_id: UUID, body: AbandonWorkspaceRequest) -> dict[str, object]:
    """Operator escape hatch: mark a workspace crashed (e.g. its box is permanently dead).

    The user recovers by restoring the workspace's backup into a fresh
    workspace; artifacts and any surviving VM are left untouched for
    forensics and are reclaimed when the row is released.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                # A fresh transition_id fences out any supervisor still driving
                # the row, so it can neither overwrite the operator's reason
                # nor keep mutating an abandoned workspace.
                cur.execute(
                    "UPDATE pool_hosts SET status = 'crashed', transition_error = %s, transition_id = %s "
                    "WHERE id = %s AND status IN ('leased', 'stopping', 'stopped', 'starting')",
                    (body.reason[:2000], str(uuid4()), str(host_db_id)),
                )
                updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if updated == 0:
            raise HTTPException(status_code=404, detail="No abandonable workspace with that id")
        # A deliberate operator action, not a fault -- log for the record
        # without raising an error-tracker event.
        logger.info("Workspace %s abandoned by operator: %s", host_db_id, body.reason)
        return TransitionResponse(host_db_id=host_db_id, status="crashed").model_dump(mode="json")
