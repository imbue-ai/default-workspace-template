"""Machine sizing endpoints (specs/slice-fleet): record a resize, applied at the next start.

A machine's size is measured in units (1 unit = 1GiB of guest RAM, which also
drives vCPUs and fair-share bandwidth proportionally); its data disk is a
second, grow-only factor. A resize is record-only: these endpoints stamp the
``target_memory_units`` / ``target_disk_gb`` columns after validation, and the
machine picks the targets up at its next start (in place when the origin box
has room, otherwise via a restore -- possibly onto a different box). Nothing
changes until then.
"""

import logging
from typing import Any
from typing import Final
from uuid import UUID

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MACHINE_UNITS_STEP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import MAX_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import is_allowed_machine_units
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.entitlements import AccountEntitlements
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.hosts import RUNNING_WORKSPACE_STATUSES
from imbue.remote_service_connector.hosts import TOTAL_WORKSPACE_STATUSES
from imbue.remote_service_connector.hosts import sum_active_machine_units_with_cursor
from imbue.remote_service_connector.hosts import sum_total_machine_disk_gb_with_cursor
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()

_MACHINE_SELECT_SQL: Final[str] = (
    "SELECT leased_to_user, status, box_generation, memory_units, target_memory_units, disk_gb, target_disk_gb "
    "FROM pool_hosts WHERE id = %s"
)


class ResizeMachineRequest(BaseModel):
    """The resize targets to stamp; omitted fields leave that dimension unchanged."""

    target_memory_units: int | None = Field(
        default=None,
        description=(
            "Desired size in units (1 unit = 1GiB guest RAM); any multiple of "
            f"{MACHINE_UNITS_STEP} in [{MACHINE_UNITS_STEP}, {MAX_MACHINE_UNITS}]. Up or down."
        ),
    )
    target_disk_gb: int | None = Field(
        default=None,
        description="Desired data-disk size in GB. Grow-only: a value below the current size is refused.",
    )


class ResizeMachineResponse(BaseModel):
    """The machine's recorded sizes after the request."""

    host_db_id: UUID = Field(description="The machine the resize applies to")
    status: str = Field(description="The machine's lifecycle status (DB vocabulary)")
    memory_units: int = Field(description="Current size in units")
    target_memory_units: int | None = Field(description="Pending unit target (None when nothing is pending)")
    disk_gb: int = Field(description="Current data-disk GB")
    target_disk_gb: int | None = Field(description="Pending disk target (None when nothing is pending)")


class _MachineRow(BaseModel):
    """The pool_hosts columns the resize validation reads."""

    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix")
    status: str = Field(description="Lifecycle status")
    box_generation: int = Field(description="Slice-fleet generation of the machine's placement")
    memory_units: int = Field(description="Current size in units")
    target_memory_units: int | None = Field(description="Pending unit target")
    disk_gb: int = Field(description="Current data-disk GB")
    target_disk_gb: int | None = Field(description="Pending disk target")


def _read_machine_row(cur: Any, host_db_id: UUID) -> _MachineRow:
    cur.execute(_MACHINE_SELECT_SQL, (str(host_db_id),))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such machine")
    return _MachineRow(
        leased_to_user=row[0],
        status=row[1],
        box_generation=int(row[2] or 1),
        memory_units=int(row[3]),
        target_memory_units=int(row[4]) if row[4] is not None else None,
        disk_gb=int(row[5]),
        target_disk_gb=int(row[6]) if row[6] is not None else None,
    )


def _validate_resize_request(machine: _MachineRow, body: ResizeMachineRequest) -> None:
    """The state / allowed-size / disk-shrink validation shared by the owner and admin routes."""
    if body.target_memory_units is None and body.target_disk_gb is None:
        raise HTTPException(status_code=400, detail="Nothing to resize: set target_memory_units and/or target_disk_gb")
    if machine.box_generation < FIRST_QEMU_BOX_GENERATION:
        raise HTTPException(
            status_code=409,
            detail=(
                "This machine's placement is not resizable (generation 1); it becomes resizable once the "
                "fleet cutover moves it to a generation-2 box"
            ),
        )
    # An in-flight start supervisor may be reading the targets mid-apply, so a
    # `starting` row refuses; every other state (crashed included -- it is just
    # database fields) accepts, and a `stopping` row's targets apply at its
    # eventual start.
    if machine.status == "starting":
        raise HTTPException(
            status_code=409,
            detail="Machine is starting; wait for the start to finish and retry the resize",
        )
    if body.target_memory_units is not None and not is_allowed_machine_units(body.target_memory_units):
        raise HTTPException(
            status_code=400,
            detail=(
                f"target_memory_units must be a multiple of {MACHINE_UNITS_STEP} between "
                f"{MACHINE_UNITS_STEP} and {MAX_MACHINE_UNITS}, got {body.target_memory_units}"
            ),
        )
    if body.target_disk_gb is not None and body.target_disk_gb < machine.disk_gb:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Disk never shrinks: target_disk_gb={body.target_disk_gb} is below the machine's "
                f"current {machine.disk_gb}GB"
            ),
        )


def _check_resize_quotas(
    cur: Any,
    user_id_prefix: str,
    machine: _MachineRow,
    body: ResizeMachineRequest,
    entitlements: AccountEntitlements,
) -> None:
    """The resize's quota checks: the resize request is the grant point.

    ``max_active_machine_units`` counts running machines with each at the
    larger of current/target size (the sum already includes this machine when
    it is running, so the delta to its new target is what is checked).
    ``max_total_machine_disk_gb`` counts running + stopped -- disk never
    changes at stop, so the target is granted here and never re-checked.
    """
    counted_units = max(machine.memory_units, machine.target_memory_units or 0)
    if body.target_memory_units is not None and machine.status in RUNNING_WORKSPACE_STATUSES:
        active_units = sum_active_machine_units_with_cursor(cur, user_id_prefix)
        new_active_units = active_units - counted_units + max(machine.memory_units, body.target_memory_units)
        if new_active_units > entitlements.max_active_machine_units:
            raise_quota_exceeded(
                "max_active_machine_units",
                entitlements.max_active_machine_units,
                active_units,
                "active machine units",
            )
    if body.target_disk_gb is not None:
        # The sum covers only running + stopped rows, so a machine outside
        # those statuses (e.g. crashed) has nothing in it to subtract; its
        # post-resize footprint is checked as if it re-entered the counted set.
        counted_disk_gb = (
            max(machine.disk_gb, machine.target_disk_gb or 0) if machine.status in TOTAL_WORKSPACE_STATUSES else 0
        )
        total_disk_gb = sum_total_machine_disk_gb_with_cursor(cur, user_id_prefix)
        new_total_disk_gb = total_disk_gb - counted_disk_gb + max(machine.disk_gb, body.target_disk_gb)
        if new_total_disk_gb > entitlements.max_total_machine_disk_gb:
            raise_quota_exceeded(
                "max_total_machine_disk_gb",
                entitlements.max_total_machine_disk_gb,
                total_disk_gb,
                "machine disk GB",
            )


def _stamp_resize_targets(cur: Any, host_db_id: UUID, machine: _MachineRow, body: ResizeMachineRequest) -> None:
    """Stamp the requested targets; a target equal to the current size clears that dimension (idempotent)."""
    new_target_units: int | None = machine.target_memory_units
    if body.target_memory_units is not None:
        new_target_units = None if body.target_memory_units == machine.memory_units else body.target_memory_units
    new_target_disk: int | None = machine.target_disk_gb
    if body.target_disk_gb is not None:
        new_target_disk = None if body.target_disk_gb == machine.disk_gb else body.target_disk_gb
    cur.execute(
        "UPDATE pool_hosts SET target_memory_units = %s, target_disk_gb = %s WHERE id = %s",
        (new_target_units, new_target_disk, str(host_db_id)),
    )


def _resize_response(cur: Any, host_db_id: UUID) -> ResizeMachineResponse:
    machine = _read_machine_row(cur, host_db_id)
    return ResizeMachineResponse(
        host_db_id=host_db_id,
        status=machine.status,
        memory_units=machine.memory_units,
        target_memory_units=machine.target_memory_units,
        disk_gb=machine.disk_gb,
        target_disk_gb=machine.target_disk_gb,
    )


@router.post("/machines/{host_db_id}/resize")
def resize_machine(request: Request, host_db_id: UUID, body: ResizeMachineRequest) -> dict[str, object]:
    """Record a machine resize (owner route): validated, quota-checked, applied at the next start.

    Units may go up or down within the allowed-size set; disk only grows.
    Targets equal to the current size clear that dimension's pending target
    (idempotent). The CLI tells the user nothing changes until a restart.
    """
    with handle_endpoint_errors():
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    # The per-user advisory lock serializes this against
                    # concurrent leases/starts/resizes, so the quota sums
                    # cannot be raced past their caps.
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user.user_id_prefix,))
                    machine = _read_machine_row(cur, host_db_id)
                    if machine.leased_to_user != user.user_id_prefix:
                        raise HTTPException(status_code=403, detail="You do not own this machine")
                    _validate_resize_request(machine, body)
                    _check_resize_quotas(cur, user.user_id_prefix, machine, body, entitlements)
                    _stamp_resize_targets(cur, host_db_id, machine, body)
                    response = _resize_response(cur, host_db_id)
        emit_metric("machine_resize_recorded", 1, {"status": response.status})
        logger.info(
            "Machine %s resize recorded (units target=%s, disk target=%s)",
            host_db_id,
            response.target_memory_units,
            response.target_disk_gb,
        )
        return response.model_dump(mode="json")


@router.post("/admin/machines/{host_db_id}/resize")
def admin_resize_machine(request: Request, host_db_id: UUID, body: ResizeMachineRequest) -> dict[str, object]:
    """Operator resize of any machine: the same validation, minus ownership and quotas."""
    with handle_endpoint_errors():
        require_admin_key(request)
        with db.pooled_db_connection() as conn:
            with conn:
                with conn.cursor() as cur:
                    machine = _read_machine_row(cur, host_db_id)
                    if machine.leased_to_user is not None:
                        # The owner's advisory lock is what a start flips the
                        # row to `starting` under, so only a read taken under
                        # it can enforce the starting-state refusal (an
                        # unlocked stamp could land mid-apply and be cleared
                        # unapplied by the start's completion). Unowned rows
                        # cannot start, so they need no lock.
                        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (machine.leased_to_user,))
                        machine = _read_machine_row(cur, host_db_id)
                    _validate_resize_request(machine, body)
                    _stamp_resize_targets(cur, host_db_id, machine, body)
                    response = _resize_response(cur, host_db_id)
        emit_metric("machine_resize_recorded", 1, {"status": response.status, "actor": "admin"})
        logger.info(
            "Machine %s resize recorded by operator (units target=%s, disk target=%s)",
            host_db_id,
            response.target_memory_units,
            response.target_disk_gb,
        )
        return response.model_dump(mode="json")
