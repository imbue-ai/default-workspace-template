"""Workspace stop/start transition supervisor.

One supervisor run drives one workspace's in-flight transition to completion:

* ``stopping``: halt the VM, generate + wrap the per-stop age identity,
  launch the box-side upload, wait for it to verify, and land the row on
  ``stopped`` the moment the upload verifies -- placement and the box link
  stay set, because the halted local VM is kept for the retention window.
  The supervisor then sits out the remainder of that window and finalizes:
  delete the local VM, drop the superseded artifact generation, and null
  the placement (freeing the slot).
* ``stopped`` with a box link: resume the retention wait / finalize for a
  row whose previous supervisor died after landing it on ``stopped``.
* ``starting``: restart in place when the VM still exists on its origin box
  (a start within the retention window), otherwise reserve a slot on a
  random same-region box, restore the artifact there, boot it, and land the
  row on ``leased`` with its new coordinates. A failed start always lands
  back on ``stopped`` with the error recorded, placement untouched.

Transitions only ever begin from stable states (``leased`` and ``stopped``;
the endpoints 409 anything else), so a stop supervisor and a start
supervisor can never legitimately coexist for one row. Ownership is
enforced with a fencing token: whoever begins (or takes over) a transition
mints a fresh ``transition_id`` under the same CAS that sets the status,
and every write a supervisor makes -- heartbeats, recorded material, the
final CAS, and ``transition_error`` -- is guarded on it, so a superseded
driver's writes hit zero rows and it exits quietly.

Supervisors are spawned by the stop/start endpoints (via the hook the Modal
entrypoint wires). The watchdog cron re-drives any in-flight row whose
heartbeat has gone stale (crash recovery) by *taking over*: it mints a
fresh ``transition_id`` -- fencing out an alive-but-wedged driver -- and
spawns a new supervisor with it, backing off exponentially in
``transition_failure_count`` and alerting ops once a transition has failed
many consecutive times.
"""

import base64
import json
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from uuid import uuid4

import paramiko
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.hosts as hosts_module
import imbue.remote_service_connector.storage as storage_module
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_DISK_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.regions import OVH_DATACENTER_CODE_BY_US_REGION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import HOST_RAM_RESERVE_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import PER_VM_RAM_OVERHEAD_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_box_total_units
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_gen2_disk_budget_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_vcpus
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import RESTORE_NO_SPACE_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import TransferEnv
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_is_transfer_alive_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_launch_detached_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import build_read_status_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_gen2_resize_applied_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_gen2_restore_reserved_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import parse_status_text
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_download_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_resize_in_place_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_gen2_restore_reserve_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import render_transfer_env
from imbue.mngr_imbue_cloud.slices.gen2_scripts.transfer import transfer_dir
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import box_scripts
from imbue.remote_service_connector import box_scripts_gen2
from imbue.remote_service_connector import db
from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.errors import WorkspaceTransitionError
from imbue.remote_service_connector.ssh_certs import SshCertificateBundleMissingError
from imbue.remote_service_connector.ssh_certs import management_credentials_for_generation
from imbue.remote_service_connector.storage import StorageConfig

logger = logging.getLogger(__name__)

# How often a supervisor polls the box status file (and heartbeats the row).
_POLL_SECONDS: Final[float] = 15.0
# A transition whose heartbeat is older than this is considered orphaned and
# gets taken over by a fresh supervisor from the watchdog cron.
STALE_HEARTBEAT_SECONDS: Final[int] = 120
# Watchdog re-drive backoff: a row is only re-driven once its heartbeat is
# staler than min(cap, STALE_HEARTBEAT_SECONDS * 2^failure_count), so a
# persistently-failing transition is retried ever less often instead of on
# every cron tick, up to this cap.
_WATCHDOG_BACKOFF_CAP_SECONDS: Final[float] = 6 * 3600.0
# Exponent clamp for the backoff: any value whose doubled delay already
# exceeds the cap works (120 * 2^8 > 6h).
_WATCHDOG_BACKOFF_MAX_EXPONENT: Final[int] = 16
# Once a transition has failed this many consecutive times it is clearly not
# converging on its own: the watchdog logs at error level (which reaches the
# tier's error tracker and alerts ops) while continuing the backed-off
# retries. With the hourly cron and the backoff above, this threshold is
# reached after roughly a day of persistent failure.
_ESCALATION_FAILURE_COUNT: Final[int] = 8
# Bound for one supervisor's polling of a single transfer. The enclosing
# Modal function timeout is the hard stop; this keeps a wedged transfer from
# consuming the entire function timeout before the watchdog can see it.
_TRANSFER_WAIT_SECONDS: Final[float] = 6000.0
_SSH_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
_VM_STOP_TIMEOUT_SECONDS: Final[float] = 300.0
_VM_START_TIMEOUT_SECONDS: Final[float] = 600.0

# Names of the artifact objects tracked in the manifest, in upload order.
_OBJECT_NAMES: Final[tuple[str, ...]] = ("DISK", "DATADISK", "META")

_UPLOAD_SCRIPT_FILENAME: Final[str] = "upload.sh"
_DOWNLOAD_SCRIPT_FILENAME: Final[str] = "download.sh"
_RESIZE_SCRIPT_FILENAME: Final[str] = "resize.sh"


class ArtifactObject(BaseModel):
    """One uploaded object's ciphertext digest and size."""

    sha256: str = Field(description="sha256 hex digest of the object (ciphertext)")
    size_bytes: int = Field(description="Object size in bytes (ciphertext)")


class ArtifactManifest(BaseModel):
    """The uploaded artifact's coordinates, recorded on the row as JSONB."""

    generation: int = Field(description="Artifact generation this manifest describes")
    key_prefix: str = Field(description="Object key prefix (<host_id>/gen-<n>)")
    age_recipient: str = Field(description="age recipient the objects are encrypted to")
    source_vm_ssh_port: int = Field(description="VM-root host port at stop time (rewritten at restore)")
    source_container_ssh_port: int = Field(description="Container host port at stop time (rewritten at restore)")
    object_by_name: dict[str, ArtifactObject] = Field(
        default_factory=dict, description="Uploaded objects keyed by DISK/DATADISK/META"
    )
    disk_virtual_bytes: int | None = Field(
        default=None,
        description="The boot disk's measured qcow2 virtual size (gen-2 uploads; sizes the restore's df guard)",
    )
    datadisk_virtual_bytes: int | None = Field(
        default=None,
        description="The data disk's measured qcow2 virtual size at upload (gen-2 uploads; recorded for operators)",
    )


class WorkspaceRow(BaseModel):
    """The pool_hosts columns a transition supervisor works from."""

    host_db_id: str = Field(description="pool_hosts row id (UUID as string)")
    status: str = Field(description="Lifecycle status")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix")
    host_id: str = Field(description="mngr host id (host-<32hex>)")
    vps_address: str | None = Field(description="Box public address (NULL once the retention finalize frees the slot)")
    ssh_port: int | None = Field(
        description="VM-root forwarded port (NULL once the retention finalize frees the slot)"
    )
    ssh_user: str = Field(description="SSH user on the VM (root)")
    container_ssh_port: int | None = Field(
        description="Container forwarded port (NULL once the retention finalize frees the slot)"
    )
    bare_metal_server_id: str | None = Field(description="Owning box row id (kept until the VM is deleted)")
    slice_instance_name: str | None = Field(description="Slice VM instance name on the box")
    slice_disk_name: str | None = Field(description="Slice data-disk name on the box")
    region: str | None = Field(description="Lease-region label (e.g. US-EAST-VA)")
    stop_requested_at: datetime | None = Field(description="When the stop was requested")
    artifact_manifest: ArtifactManifest | None = Field(description="Uploaded artifact coordinates")
    wrapped_dek: str | None = Field(description="KEK-wrapped age identity")
    artifact_generation: int = Field(description="Last fully-uploaded artifact generation")
    transition_id: str | None = Field(description="Fencing token of the transition's current owner")
    transition_failure_count: int = Field(description="Consecutive failed drives of the current transition")
    box_generation: int = Field(
        description="Slice-fleet generation of the VM's current placement (selects the box-side script set)"
    )
    memory_units: int = Field(description="The machine's current size in units (1 unit = 1GiB guest RAM)")
    target_memory_units: int | None = Field(description="A pending resize's unit target (applied at the next start)")
    disk_gb: int = Field(description="The machine's data-disk GB")
    target_disk_gb: int | None = Field(description="A pending disk grow's GB target (applied at the next start)")


class BoxRow(BaseModel):
    """The bare_metal_servers columns needed to reach a box."""

    server_id: str = Field(description="bare_metal_servers row id (UUID as string)")
    public_address: str = Field(description="SSH-reachable public address")
    slice_service_user: str = Field(description="Non-root service user that owns the slice VMs")
    box_host_public_key: str = Field(description="Pinned sshd host public key")
    slot_count: int = Field(description="Slices the box holds when full (gen-1 capacity accounting)")
    box_generation: int = Field(description="Slice-fleet generation the box runs (selects the script set)")
    status: str = Field(description="Box lifecycle status (a draining origin box forces the restore path)")
    uplink_mbps: int = Field(description="Declared uplink rate in Mbit/s, the input to gen-2 fair-share shaping")
    disk_gb: int | None = Field(
        description="Gen-2: the measured storage partition in GiB (the disk budget's input); gen-1: usable slice disk in GB"
    )
    ram_gb: int | None = Field(description="Total RAM in GB (the gen-2 unit budget's input)")
    cpu_threads: int | None = Field(description="CPU threads (sizes a gen-2 restore's proportional vCPUs)")
    cpu_overcommit_ratio: float | None = Field(description="CPU overcommit factor for gen-2 vCPU sizing")


class _SupervisorSpawner(BaseModel):
    """Holder for the spawn hook the Modal entrypoint wires at import time."""

    hook: Callable[[str, str], None] | None = None


spawner = _SupervisorSpawner()


def spawn_supervisor(host_db_id: str, transition_id: str) -> None:
    """Spawn a detached supervisor owning ``transition_id`` (no-op with a warning when unwired).

    The token is passed by the spawner rather than re-read from the row so a
    late-starting supervisor can never adopt a newer transition's token and
    duel with that transition's own supervisor.
    """
    if spawner.hook is None:
        logger.warning(
            "No supervisor spawn hook wired; transition for %s will be driven by the watchdog cron", host_db_id
        )
        return
    spawner.hook(host_db_id, transition_id)


# Row / box access (thin SQL wrappers; the fake DB in tests emulates these)

# CLEANUP: drop the COALESCE fallbacks to the legacy lima_instance_name /
# lima_disk_name / lima_service_user columns once every tier's pool DB has
# applied migration 041 and no pre-rename checkout writes them anymore.
_WORKSPACE_ROW_SELECT: Final[str] = (
    "SELECT id, status, leased_to_user, host_id, vps_address, ssh_port, ssh_user, container_ssh_port, "
    "bare_metal_server_id, COALESCE(slice_instance_name, lima_instance_name), "
    "COALESCE(slice_disk_name, lima_disk_name), region, stop_requested_at, "
    "artifact_manifest, wrapped_dek, artifact_generation, transition_id, transition_failure_count, "
    "box_generation, memory_units, target_memory_units, disk_gb, target_disk_gb "
    "FROM pool_hosts WHERE id = %s"
)


def _workspace_row_from_tuple(row: tuple[Any, ...]) -> WorkspaceRow:
    manifest_raw = row[13]
    manifest = None
    if manifest_raw:
        parsed = json.loads(manifest_raw) if isinstance(manifest_raw, str) else manifest_raw
        manifest = ArtifactManifest.model_validate(parsed)
    return WorkspaceRow(
        host_db_id=str(row[0]),
        status=row[1],
        leased_to_user=row[2],
        host_id=row[3],
        vps_address=row[4],
        ssh_port=row[5],
        ssh_user=row[6] or "root",
        container_ssh_port=row[7],
        bare_metal_server_id=str(row[8]) if row[8] is not None else None,
        slice_instance_name=row[9],
        slice_disk_name=row[10],
        region=row[11],
        stop_requested_at=row[12],
        artifact_manifest=manifest,
        wrapped_dek=row[14],
        artifact_generation=int(row[15] or 0),
        transition_id=str(row[16]) if row[16] is not None else None,
        transition_failure_count=int(row[17] or 0),
        box_generation=int(row[18] or 1),
        memory_units=int(row[19]),
        target_memory_units=int(row[20]) if row[20] is not None else None,
        disk_gb=int(row[21]),
        target_disk_gb=int(row[22]) if row[22] is not None else None,
    )


def read_workspace_row(conn: Any, host_db_id: str) -> WorkspaceRow | None:
    with conn.cursor() as cur:
        cur.execute(_WORKSPACE_ROW_SELECT, (host_db_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _workspace_row_from_tuple(row)


_BOX_ROW_COLUMNS: Final[str] = (
    "id, public_address, COALESCE(slice_service_user, lima_service_user), box_host_public_key, slot_count, "
    "box_generation, status, "
    "uplink_mbps, disk_gb, ram_gb, cpu_threads, cpu_overcommit_ratio"
)
# Index of the appended region column in ``_list_candidate_boxes``'s SELECT.
_BOX_ROW_REGION_INDEX: Final[int] = 12


def _box_row_from_tuple(row: tuple[Any, ...]) -> BoxRow:
    return BoxRow(
        server_id=str(row[0]),
        public_address=row[1],
        slice_service_user=row[2] or "root",
        box_host_public_key=row[3],
        slot_count=int(row[4] or 0),
        box_generation=int(row[5] or 1),
        status=row[6] or "",
        uplink_mbps=int(row[7]),
        disk_gb=int(row[8]) if row[8] is not None else None,
        ram_gb=int(row[9]) if row[9] is not None else None,
        cpu_threads=int(row[10]) if row[10] is not None else None,
        cpu_overcommit_ratio=float(row[11]) if row[11] is not None else None,
    )


def _read_box_row(conn: Any, server_id: str) -> BoxRow | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BOX_ROW_COLUMNS} FROM bare_metal_servers WHERE id = %s",
            (server_id,),
        )
        row = cur.fetchone()
    if row is None or not row[1] or not row[3]:
        return None
    return _box_row_from_tuple(row)


def _list_candidate_boxes(conn: Any, region_label: str | None, box_generation: int) -> list[BoxRow]:
    """Every ready box eligible to host a restore.

    Filtered to the row's region when known, and to boxes of exactly the
    artifact's generation: nothing converts an artifact between generations
    (the gen-1 -> gen-2 move is the operator cutover, specs/slice-fleet).
    ``draining`` boxes are excluded by the ``ready`` filter, so drained
    workspaces' restores land on the surviving fleet.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_BOX_ROW_COLUMNS}, region FROM bare_metal_servers WHERE status = 'ready'")
        rows = cur.fetchall()
    datacenter = OVH_DATACENTER_CODE_BY_US_REGION.get(region_label) if region_label else None
    boxes: list[BoxRow] = []
    for row in rows:
        if not row[1] or not row[3]:
            continue
        if datacenter is not None and row[_BOX_ROW_REGION_INDEX] and row[_BOX_ROW_REGION_INDEX] != datacenter:
            continue
        box = _box_row_from_tuple(row)
        if box.box_generation != box_generation:
            continue
        boxes.append(box)
    return boxes


def _heartbeat(row: WorkspaceRow, expected_status: str) -> bool:
    """Stamp the supervisor's liveness; False when the row is no longer ours to drive.

    The guarded UPDATE doubles as the ownership probe: it only lands while the
    row still carries our fencing token *and* the phase's expected status, so
    a zero rowcount means the transition was superseded or taken over.
    """
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET transition_heartbeat_at = NOW() "
                "WHERE id = %s AND transition_id = %s AND status = %s",
                (row.host_db_id, row.transition_id, expected_status),
            )
            updated = cur.rowcount
        conn.commit()
    return updated == 1


def _assert_owned(row: WorkspaceRow, expected_status: str) -> None:
    """Heartbeat + ownership check; raises ``_TransitionSuperseded`` when fenced out."""
    if not _heartbeat(row, expected_status):
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")


def _current_status(host_db_id: str) -> str | None:
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pool_hosts WHERE id = %s", (host_db_id,))
            row = cur.fetchone()
    return row[0] if row is not None else None


def _record_transition_error(row: WorkspaceRow, message: str) -> None:
    """Record a failed drive on the row (guarded: a fenced-out supervisor writes nothing)."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET transition_error = %s, "
                "transition_failure_count = transition_failure_count + 1 "
                "WHERE id = %s AND transition_id = %s",
                (message[:2000], row.host_db_id, row.transition_id),
            )
        conn.commit()


# Box SSH seams (faked in tests)


def _run_box_command(
    box: BoxRow,
    command: str,
    input_text: str | None = None,
    timeout_seconds: float = _SSH_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run one command on the box as the slice service user; return (rc, stdout, stderr)."""
    try:
        credentials = management_credentials_for_generation(box.box_generation)
    except SshCertificateBundleMissingError as exc:
        raise WorkspaceTransitionError(
            f"no management SSH credentials available for {box.public_address}: {exc}"
        ) from exc
    with hosts_module.management_ssh_client(
        box.public_address,
        22,
        box.slice_service_user,
        credentials,
        timeout_seconds=30,
        expected_host_public_key=box.box_host_public_key,
    ) as client:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout_seconds)
        if input_text is not None:
            stdin.write(input_text)
            stdin.channel.shutdown_write()
        exit_status = stdout.channel.recv_exit_status()
        return exit_status, stdout.read().decode(), stderr.read().decode()


def _run_box_commands_checked(box: BoxRow, commands: tuple[str, ...], timeout_seconds: float) -> None:
    """Run a fail-fast command sequence as one ``&&``-joined line over a single SSH exec."""
    command = " && ".join(commands)
    exit_status, _stdout, stderr = _run_box_command(box, command, timeout_seconds=timeout_seconds)
    if exit_status != 0:
        raise WorkspaceTransitionError(f"box command {command!r} failed (exit {exit_status}): {stderr.strip()}")


def _write_box_file(box: BoxRow, instance_name: str, filename: str, content: str) -> None:
    instance_transfer_dir = transfer_dir(instance_name)
    command = f'umask 077 && mkdir -p "{instance_transfer_dir}" && cat > "{instance_transfer_dir}/{filename}"'
    exit_status, _stdout, stderr = _run_box_command(box, command, input_text=content)
    if exit_status != 0:
        raise WorkspaceTransitionError(f"failed to write {filename} on {box.public_address}: {stderr.strip()}")


def _is_gen2(generation: int) -> bool:
    return generation >= FIRST_QEMU_BOX_GENERATION


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The supervisor


def run_transition_supervisor(host_db_id: str, transition_id: str) -> str:
    """Drive one workspace's in-flight transition to completion; returns an outcome label.

    ``transition_id`` is the fencing token minted by whoever spawned this
    supervisor; a row that has since moved on to a newer token is not ours
    to drive.
    """
    config = storage_module.read_storage_config()
    with db.pooled_db_connection() as conn:
        row = read_workspace_row(conn, host_db_id)
    if row is None:
        return "row-gone"
    if row.transition_id != transition_id:
        logger.info("Supervisor for %s: transition was taken over or superseded; exiting", host_db_id)
        return "superseded"
    if row.status == "stopping":
        return _drive_stop(config, row)
    if row.status == "starting":
        return _drive_start(config, row)
    if row.status == "stopped" and row.bare_metal_server_id is not None:
        # The stop landed but its local VM is still on the box: resume the
        # retention wait (a start within the window restarts it in place)
        # and free the slot once the window closes.
        return _drive_stopped_retention(config, row)
    logger.info("Supervisor for %s: nothing to do (status=%s)", host_db_id, row.status)
    return "no-op"


def _require_box(row: WorkspaceRow) -> BoxRow:
    if row.bare_metal_server_id is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no bare_metal_server_id")
    with db.pooled_db_connection() as conn:
        box = _read_box_row(conn, row.bare_metal_server_id)
    if box is None:
        raise WorkspaceTransitionError(
            f"workspace {row.host_db_id}: bare_metal_servers row {row.bare_metal_server_id} is missing "
            "its address or pinned host key"
        )
    return box


def _require_slice_names(row: WorkspaceRow) -> tuple[str, str]:
    if not row.slice_instance_name or not row.slice_disk_name:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no slice instance/disk names recorded")
    return row.slice_instance_name, row.slice_disk_name


def _generate_age_keypair_on_box(box: BoxRow) -> tuple[str, str]:
    """Run age-keygen on the box; return (recipient, identity)."""
    exit_status, stdout, stderr = _run_box_command(
        box, "PATH=/usr/local/bin:$HOME/.local/bin:$PATH age-keygen 2>/dev/null"
    )
    if exit_status != 0:
        raise WorkspaceTransitionError(f"age-keygen failed on {box.public_address}: {stderr.strip()}")
    recipient = ""
    identity = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("# public key:"):
            recipient = stripped.split()[-1]
        elif stripped.startswith("AGE-SECRET-KEY-"):
            identity = stripped
        else:
            # Comment/banner lines from age-keygen are expected; skip them.
            pass
    if not recipient or not identity:
        raise WorkspaceTransitionError(f"could not parse age-keygen output on {box.public_address}")
    return recipient, identity


def _ensure_stop_artifact_material(config: StorageConfig, row: WorkspaceRow, box: BoxRow) -> ArtifactManifest:
    """Generate + persist the stop's age identity and manifest skeleton (idempotent).

    The wrapped identity is committed to the row *before* the upload launches,
    so a supervisor crash mid-upload never strands undecryptable objects.
    Returns the manifest the upload targets.

    Recorded material is only reused when it was minted for THIS stop
    (manifest generation == recorded generation + 1, the re-driven-supervisor
    case). A restore leaves the completed generation's manifest + wrapped dek
    on the leased row, and reusing those here would re-target the completed
    generation's key prefix: the upload would overwrite the workspace's only
    artifact in place and the post-CAS previous-generation cleanup would then
    delete it.
    """
    if (
        row.wrapped_dek is not None
        and row.artifact_manifest is not None
        and row.artifact_manifest.generation == row.artifact_generation + 1
    ):
        return row.artifact_manifest
    if row.ssh_port is None or row.container_ssh_port is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} is stopping but has no recorded ports")
    recipient, identity = _generate_age_keypair_on_box(box)
    wrapped = storage_module.wrap_dek(config, identity)
    manifest = ArtifactManifest(
        generation=row.artifact_generation + 1,
        key_prefix=f"{storage_module.workspace_key_prefix(config, row.host_id)}/gen-{row.artifact_generation + 1}",
        age_recipient=recipient,
        source_vm_ssh_port=row.ssh_port,
        source_container_ssh_port=row.container_ssh_port,
    )
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET wrapped_dek = %s, artifact_manifest = %s "
                "WHERE id = %s AND status = 'stopping' AND transition_id = %s",
                (wrapped, json.dumps(manifest.model_dump()), row.host_db_id, row.transition_id),
            )
            updated = cur.rowcount
        conn.commit()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    return manifest


def _transfer_env_for(
    config: StorageConfig, manifest: ArtifactManifest, instance_name: str, identity: str = ""
) -> str:
    return render_transfer_env(
        TransferEnv(
            s3_endpoint=config.s3_endpoint,
            s3_region=config.s3_region,
            access_key_id=config.access_key_id,
            secret_access_key=config.secret_access_key,
            bucket=config.bucket,
            key_prefix=manifest.key_prefix,
            instance_name=instance_name,
            age_recipient=manifest.age_recipient,
            age_identity=identity,
        )
    )


def _read_finished_transfer_status(box: BoxRow, instance_name: str) -> dict[str, str] | None:
    """One status-file read: the parsed status when it reports FINISHED=1, else None.

    Raises when the finished status reports failure.
    """
    exit_status, stdout, _stderr = _run_box_command(box, build_read_status_command(instance_name))
    if exit_status != 0 or not stdout.strip():
        return None
    status = parse_status_text(stdout)
    if status.get("FINISHED") != "1":
        return None
    if status.get("STAGE") == "failed":
        raise WorkspaceTransitionError(
            f"transfer failed on {box.public_address}: {status.get('ERROR', 'unknown error')}"
        )
    return status


def _poll_transfer(row: WorkspaceRow, box: BoxRow, instance_name: str, expected_status: str) -> dict[str, str]:
    """Poll the box status file until the transfer finishes; heartbeat as we go.

    Returns the final parsed status. Raises when the transfer fails, dies
    before finishing, or the transition is superseded / taken over (the
    guarded heartbeat stops landing).
    """
    deadline = time.monotonic() + _TRANSFER_WAIT_SECONDS
    while time.monotonic() < deadline:
        _assert_owned(row, expected_status)
        finished = _read_finished_transfer_status(box, instance_name)
        if finished is not None:
            return finished
        alive_status, _out, _err = _run_box_command(box, build_is_transfer_alive_command(instance_name))
        if alive_status != 0:
            # The transfer may have finished between the two checks (the final
            # status lands atomically just before the script exits): re-read
            # once, and only then declare it dead.
            finished = _read_finished_transfer_status(box, instance_name)
            if finished is not None:
                return finished
            raise WorkspaceTransitionError(f"transfer process died on {box.public_address} before finishing")
        _sleep(_POLL_SECONDS)
    raise WorkspaceTransitionError(f"transfer did not finish within {_TRANSFER_WAIT_SECONDS:.0f}s")


class _TransitionSuperseded(ConnectorError):
    """The row left the expected status mid-transition (e.g. an in-window restart)."""

    def __init__(self, new_status: str) -> None:
        self.new_status = new_status
        super().__init__(f"transition superseded; row is now {new_status}")


def _manifest_with_objects(manifest: ArtifactManifest, status: dict[str, str]) -> ArtifactManifest:
    objects: dict[str, ArtifactObject] = {}
    for name in _OBJECT_NAMES:
        sha = status.get(f"SHA_{name}", "")
        size_raw = status.get(f"BYTES_{name}", "")
        if not sha or not size_raw.isdigit():
            raise WorkspaceTransitionError(f"upload status is missing sha/bytes for {name}")
        objects[name] = ArtifactObject(sha256=sha, size_bytes=int(size_raw))
    # Gen-2 uploads also report the disks' measured qcow2 virtual sizes (the
    # boot disk's feeds the restore's df guard; the data disk's is recorded for
    # operators); gen-1 uploads (raw disk files) do not.
    disk_virtual_raw = status.get("VIRTUAL_BYTES_DISK", "")
    datadisk_virtual_raw = status.get("VIRTUAL_BYTES_DATADISK", "")
    return ArtifactManifest(
        generation=manifest.generation,
        key_prefix=manifest.key_prefix,
        age_recipient=manifest.age_recipient,
        source_vm_ssh_port=manifest.source_vm_ssh_port,
        source_container_ssh_port=manifest.source_container_ssh_port,
        object_by_name=objects,
        disk_virtual_bytes=int(disk_virtual_raw) if disk_virtual_raw.isdigit() else None,
        datadisk_virtual_bytes=int(datadisk_virtual_raw) if datadisk_virtual_raw.isdigit() else None,
    )


def _missing_manifest_object_names(manifest: ArtifactManifest) -> tuple[str, ...]:
    """The artifact objects the manifest does not record; empty means it is restorable."""
    return tuple(name for name in _OBJECT_NAMES if name not in manifest.object_by_name)


def _drive_stop(config: StorageConfig, row: WorkspaceRow) -> str:
    try:
        _drive_stop_inner(config, row)
    except _TransitionSuperseded as exc:
        logger.info("Stop of %s superseded (row now %s)", row.host_db_id, exc.new_status)
        return "superseded"
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.error("Stop of %s failed", row.host_db_id, exc_info=exc)
        _record_transition_error(row, str(exc))
        return "stop-failed"
    # The workspace is durably stopped; what remains (the retention wait and
    # the slot-freeing finalize) is plumbing with its own outcome labels.
    return _drive_stopped_retention(config, row)


def _drive_stop_inner(config: StorageConfig, row: WorkspaceRow) -> None:
    box = _require_box(row)
    instance_name, disk_name = _require_slice_names(row)

    # Halt the VM and take it out of boot autostart: the gen-1 stop marker
    # keeps the box's autostart script away, the gen-2 unit disable keeps
    # systemd's WantedBy away.
    _assert_owned(row, "stopping")
    if _is_gen2(row.box_generation):
        stop_commands = box_scripts_gen2.build_gen2_stop_vm_commands(instance_name)
    else:
        stop_commands = box_scripts.build_stop_vm_commands(instance_name)
    _run_box_commands_checked(box, stop_commands, _VM_STOP_TIMEOUT_SECONDS)

    # Commit the encryption material before any byte leaves the box.
    _assert_owned(row, "stopping")
    manifest = _ensure_stop_artifact_material(config, row, box)

    # Stage the env + script, and launch the upload if it is not already
    # running (idempotent: a re-driven supervisor re-reads the status file).
    if _is_gen2(row.box_generation):
        upload_script = box_scripts_gen2.render_gen2_upload_script(instance_name)
    else:
        upload_script = box_scripts.render_upload_script(instance_name, disk_name)
    _write_box_file(box, instance_name, "env", _transfer_env_for(config, manifest, instance_name))
    _write_box_file(box, instance_name, _UPLOAD_SCRIPT_FILENAME, upload_script)
    status_now, stdout_now, _stderr_now = _run_box_command(box, build_read_status_command(instance_name))
    parsed_now = parse_status_text(stdout_now) if status_now == 0 else {}
    alive_now, _o, _e = _run_box_command(box, build_is_transfer_alive_command(instance_name))
    # Only a status this upload wrote to completion counts: a stale one (a
    # failed earlier attempt, or a leftover download status from a previous
    # restore onto this box) must trigger a relaunch, which clears it.
    is_upload_complete = parsed_now.get("FINISHED") == "1" and parsed_now.get("STAGE") == "uploaded"
    if not is_upload_complete and alive_now != 0:
        launch_status, _lo, launch_err = _run_box_command(
            box, build_launch_detached_command(instance_name, _UPLOAD_SCRIPT_FILENAME)
        )
        if launch_status != 0:
            raise WorkspaceTransitionError(f"failed to launch upload: {launch_err.strip()}")

    final_status = _poll_transfer(row, box, instance_name, expected_status="stopping")
    manifest_with_objects = _manifest_with_objects(manifest, final_status)

    # The artifact is durable: land the row on ``stopped`` immediately.
    # Placement and the box link stay set -- the halted local VM is kept for
    # the retention window so a start within it restarts in place.
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'stopped', stopped_at = NOW(), artifact_manifest = %s, "
                "artifact_generation = %s, transition_error = NULL, transition_failure_count = 0 "
                "WHERE id = %s AND status = 'stopping' AND transition_id = %s",
                (
                    json.dumps(manifest_with_objects.model_dump()),
                    manifest.generation,
                    row.host_db_id,
                    row.transition_id,
                ),
            )
            updated = cur.rowcount
        conn.commit()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    logger.info("Workspace %s stopped (generation %d uploaded)", row.host_db_id, manifest.generation)


def _drive_stopped_retention(config: StorageConfig, row: WorkspaceRow) -> str:
    """Sit out the retention window on a ``stopped`` row, then free its slot.

    A start within the window mints a new transition token, which fences this
    supervisor out at its next heartbeat -- the local VM is then the start's
    to boot, not ours to delete.
    """
    try:
        # Re-read the row: when arriving from a just-landed stop, the caller's
        # snapshot still carries the pre-stop artifact generation.
        with db.pooled_db_connection() as conn:
            fresh_row = read_workspace_row(conn, row.host_db_id)
        if fresh_row is None or fresh_row.transition_id != row.transition_id or fresh_row.status != "stopped":
            raise _TransitionSuperseded(fresh_row.status if fresh_row is not None else "gone")
        # Deleting the local VM destroys the only bootable copy unless the
        # durable artifact is proven whole, so require the same manifest
        # completeness the restore path does. This can only fail for a row
        # that reached ``stopped`` without a verified upload (e.g. a legacy
        # start claimed from ``stopping`` that failed back to ``stopped``);
        # refusing keeps the VM -- and the restart-in-place recovery -- alive
        # while the recorded error escalates through the watchdog.
        manifest = fresh_row.artifact_manifest
        if manifest is None or _missing_manifest_object_names(manifest):
            raise WorkspaceTransitionError(
                f"workspace {row.host_db_id} is stopped but its artifact manifest is incomplete; "
                "keeping the local VM instead of finalizing"
            )
        box = _require_box(fresh_row)
        retention_end = (fresh_row.stop_requested_at or _now()) + timedelta(seconds=config.retention_seconds)
        while _now() < retention_end:
            _assert_owned(fresh_row, "stopped")
            _sleep(min(30.0, max(1.0, (retention_end - _now()).total_seconds())))
        _assert_owned(fresh_row, "stopped")
        _delete_local_vm_and_previous_generation(
            config, fresh_row, box, previous_generation=fresh_row.artifact_generation - 1
        )
    except _TransitionSuperseded as exc:
        logger.info("Retention finalize of %s superseded (row now %s)", row.host_db_id, exc.new_status)
        return "superseded"
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.error("Finalize of stopped %s failed", row.host_db_id, exc_info=exc)
        _record_transition_error(row, str(exc))
        return "finalize-failed"
    logger.info("Workspace %s finalized (local VM deleted, slot freed)", row.host_db_id)
    return "stopped"


def _claim_local_vm_for_deletion(row: WorkspaceRow) -> None:
    """Atomically clear the placement so the local VM becomes exclusively ours to delete.

    The fencing token guards every DB write but cannot guard box commands, so
    the DB must decide who owns the VM *before* any deletion starts: once this
    guarded CAS lands, a start can only observe a placement-less row and takes
    the restore path -- it can never restart-in-place a VM whose deletion is
    underway. A zero rowcount means a start already took the row over (the
    VM is its to boot), so nothing may be deleted.
    """
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET vps_address = NULL, ssh_port = NULL, container_ssh_port = NULL "
                "WHERE id = %s AND status = 'stopped' AND transition_id = %s",
                (row.host_db_id, row.transition_id),
            )
            updated = cur.rowcount
        conn.commit()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")


def _finalize_stop_commands_for(row: WorkspaceRow) -> tuple[str, ...]:
    instance_name, disk_name = _require_slice_names(row)
    if _is_gen2(row.box_generation):
        return box_scripts_gen2.build_gen2_finalize_stop_commands(instance_name)
    return box_scripts.build_finalize_stop_commands(instance_name, disk_name)


def _delete_local_vm_and_previous_generation(
    config: StorageConfig, row: WorkspaceRow, box: BoxRow, previous_generation: int
) -> None:
    """Free the slot and drop the superseded artifact generation, then clear the box link."""
    _claim_local_vm_for_deletion(row)
    _run_box_commands_checked(box, _finalize_stop_commands_for(row), _VM_STOP_TIMEOUT_SECONDS)
    if previous_generation > 0:
        storage_module.delete_prefix(
            config, f"{storage_module.workspace_key_prefix(config, row.host_id)}/gen-{previous_generation}/"
        )
    # The box link falls last: a crash anywhere above leaves the row matching
    # the watchdog's stopped-with-box-link predicate, so the finalize is
    # resumed (the claim CAS re-matches a row whose placement is already NULL).
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET bare_metal_server_id = NULL, transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'stopped' AND transition_id = %s",
                (row.host_db_id, row.transition_id),
            )
        conn.commit()


def _drive_start(config: StorageConfig, row: WorkspaceRow) -> str:
    try:
        return _drive_start_inner(config, row)
    except _TransitionSuperseded as exc:
        logger.info("Start of %s superseded (row now %s)", row.host_db_id, exc.new_status)
        return "superseded"
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.error("Start of %s failed", row.host_db_id, exc_info=exc)
        _fail_start_back_to_stopped(row, str(exc))
        return "start-failed"


def _fail_start_back_to_stopped(row: WorkspaceRow, message: str) -> None:
    """Land a failed start back on ``stopped`` with the error recorded.

    Everything else is left as it was: the artifact is untouched, and any
    placement/box link stays -- a start within the retention window fails
    back to a row whose local VM is still there for the next try.
    """
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'stopped', transition_error = %s, "
                "transition_failure_count = transition_failure_count + 1, transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'starting' AND transition_id = %s",
                (message[:2000], row.host_db_id, row.transition_id),
            )
        conn.commit()


def _drive_start_inner(config: StorageConfig, row: WorkspaceRow) -> str:
    instance_name, _disk_name = _require_slice_names(row)
    _assert_owned(row, "starting")
    origin_box_holding_vm: BoxRow | None = None
    if row.vps_address is not None and row.bare_metal_server_id is not None:
        box = _require_box(row)
        if _is_gen2(row.box_generation):
            exists_command = box_scripts_gen2.build_gen2_instance_exists_command(instance_name)
        else:
            exists_command = f'[ -d "$HOME/.lima/{instance_name}" ]'
        exists_status, _out, _err = _run_box_command(box, exists_command)
        if exists_status == 0:
            # A draining origin box never restarts in place: the whole point
            # of draining is that the client's own restart lands the
            # workspace on a surviving box, so the halted local VM is
            # abandoned to the restore path (and deleted once it succeeds).
            if box.status == "ready":
                return _restart_in_place(config, row, box)
            logger.info(
                "Workspace %s origin box %s is %s; restoring elsewhere instead of restarting in place",
                row.host_db_id,
                box.public_address,
                box.status,
            )
            origin_box_holding_vm = box
    return _restore_from_artifact(config, row, origin_box_holding_vm)


def _restart_in_place(config: StorageConfig, row: WorkspaceRow, box: BoxRow) -> str:
    """Fast path: the VM never left its origin box; cancel any upload and boot it.

    A gen-2 row with a pending resize target first applies it in place (the
    budget re-check + env rewrite + disk grow, under the box's allocation
    lock); when the origin box cannot fit the target, the start falls back to
    the restore path, whose candidate reserve applies the target elsewhere.
    """
    instance_name, _disk_name = _require_slice_names(row)
    _assert_owned(row, "starting")
    if row.ssh_port is None or row.container_ssh_port is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} restart-in-place has no recorded ports")
    is_resize_pending = row.target_memory_units is not None or row.target_disk_gb is not None
    if _is_gen2(row.box_generation) and is_resize_pending:
        is_resize_applied = _apply_gen2_resize_in_place(row, box, instance_name)
        if not is_resize_applied:
            # The restore path applies the target on whichever box fits (the
            # origin included -- its reserve reclaims the leftover dir); when
            # it lands elsewhere, the abandoned origin VM is reaped like the
            # drain-forced case's.
            logger.info(
                "Workspace %s origin box %s cannot fit the resize target; restoring elsewhere",
                row.host_db_id,
                box.public_address,
            )
            return _restore_from_artifact(config, row, box)
    if _is_gen2(row.box_generation):
        restart_commands = box_scripts_gen2.build_gen2_cancel_and_restart_commands(
            instance_name, row.ssh_port, row.container_ssh_port
        )
    else:
        restart_commands = box_scripts.build_cancel_and_restart_commands(instance_name)
    _run_box_commands_checked(box, restart_commands, _VM_START_TIMEOUT_SECONDS)

    # Drop this stop cycle's artifact generation and its material -- the
    # booted VM immediately diverges from it. The manifest names it when
    # recorded (a start from stopped-within-retention, where the completed
    # upload bumped the counter); without one only a partial upload at the
    # not-yet-bumped next generation can exist. The counter falls back to
    # the previous generation, whose objects (when any) remain the last
    # durable artifact bookkeeping-wise until the next stop supersedes them.
    # CLEANUP: drop the manifest-absent fallback once no 'starting' row
    # claimed from 'stopping' by the pre-#547 start endpoint can remain
    # in flight (the new endpoint only starts 'stopped' rows, which always
    # carry a manifest) -- any deploy after this one's transitions settle.
    pending_generation = (
        row.artifact_manifest.generation if row.artifact_manifest is not None else row.artifact_generation + 1
    )
    storage_module.delete_prefix(
        config, f"{storage_module.workspace_key_prefix(config, row.host_id)}/gen-{pending_generation}/"
    )

    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'leased', stop_requested_at = NULL, stopped_at = NULL, "
                "artifact_manifest = NULL, wrapped_dek = NULL, artifact_generation = %s, "
                # A successful start applies any pending resize: current = the
                # applied target, targets cleared. Disk never shrinks: a stale
                # target below the recorded size is clamped, matching the size
                # the resize script actually applied (GREATEST ignores NULLs, so
                # an unset target leaves disk_gb).
                "memory_units = COALESCE(target_memory_units, memory_units), "
                "disk_gb = GREATEST(disk_gb, target_disk_gb), "
                "target_memory_units = NULL, target_disk_gb = NULL, "
                "transition_error = NULL, transition_failure_count = 0, transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'starting' AND transition_id = %s",
                (pending_generation - 1, row.host_db_id, row.transition_id),
            )
            updated = cur.rowcount
        conn.commit()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    logger.info("Workspace %s restarted in place on %s", row.host_db_id, box.public_address)
    return "restarted-in-place"


def _apply_gen2_resize_in_place(row: WorkspaceRow, box: BoxRow, instance_name: str) -> bool:
    """Run the on-box in-place resize for a pending target; False when the box refuses for capacity.

    A capacity refusal (the shared NO_UNITS / NO_DISK markers) sends the start
    down the restore path; any other failure raises.
    """
    sizing = _gen2_machine_sizing(row)
    total_units, unit_budget_mib, disk_budget_gib = _gen2_box_budgets(box)
    vcpus = _gen2_machine_vcpus_for_box(box, sizing.units, total_units)
    resize_script = render_gen2_resize_in_place_script(
        instance_name=instance_name,
        units=sizing.units,
        vcpus=vcpus,
        data_disk_gib=sizing.data_disk_gib,
        total_units=total_units,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        uplink_mbps=box.uplink_mbps,
    )
    _write_box_file(box, instance_name, _RESIZE_SCRIPT_FILENAME, resize_script)
    instance_transfer_dir = transfer_dir(instance_name)
    exit_status, stdout, stderr = _run_box_command(
        box, f'bash "{instance_transfer_dir}/{_RESIZE_SCRIPT_FILENAME}"', timeout_seconds=_VM_START_TIMEOUT_SECONDS
    )
    if exit_status != 0:
        if GEN2_NO_UNITS_MARKER in stderr or GEN2_NO_DISK_MARKER in stderr:
            return False
        raise WorkspaceTransitionError(
            f"in-place resize of {row.host_db_id} on {box.public_address} failed "
            f"(exit {exit_status}): {stderr.strip()}"
        )
    if parse_gen2_resize_applied_line(stdout) is None:
        raise WorkspaceTransitionError(
            f"in-place resize of {row.host_db_id} printed no applied marker: {stdout[-500:]!r}"
        )
    return True


class _Gen2MachineSizing(BaseModel):
    """The size a gen-2 restore (or in-place resize) applies: the pending target when one is stamped."""

    units: int = Field(description="The machine's applied size in units (1 unit = 1GiB guest RAM)")
    data_disk_gib: int = Field(description="The machine's applied data-disk size in GiB")
    # The GiB target the downloaded data disk is grown to before boot; None
    # when the artifact's disk is already the applied size.
    grow_data_disk_gib: int | None = Field(description="Data-disk grow target, when the restore applies a grow")


def _gen2_machine_sizing(row: WorkspaceRow) -> _Gen2MachineSizing:
    """The size a gen-2 start applies, from the row's sizing columns.

    The pending target (when stamped) wins -- the start is what applies it.
    Disk never shrinks: a stale target below the recorded size is clamped to
    the recorded size.
    """
    units = row.target_memory_units if row.target_memory_units is not None else row.memory_units
    applied_disk_gib = max(row.disk_gb, row.target_disk_gb) if row.target_disk_gb is not None else row.disk_gb
    grow_data_disk_gib = applied_disk_gib if applied_disk_gib > row.disk_gb else None
    return _Gen2MachineSizing(units=units, data_disk_gib=applied_disk_gib, grow_data_disk_gib=grow_data_disk_gib)


def _gen2_box_budgets(box: BoxRow) -> tuple[int, int, int]:
    """The candidate box's (total_units, unit_budget_mib, disk_budget_gib); refuses an unsized box row."""
    if box.ram_gb is None or box.ram_gb <= HOST_RAM_RESERVE_GIB or box.disk_gb is None:
        raise WorkspaceTransitionError(
            f"bare_metal_servers row {box.server_id} has no usable ram_gb/disk_gb; re-register the box"
        )
    total_units = compute_box_total_units(box.ram_gb)
    return total_units, total_units * 1024, compute_gen2_disk_budget_gib(box.disk_gb)


def _gen2_machine_vcpus_for_box(box: BoxRow, units: int, total_units: int) -> int:
    """The proportional vCPU count the machine gets on ``box``; refuses an unsized box row."""
    if box.cpu_threads is None or box.cpu_threads <= 0 or box.cpu_overcommit_ratio is None:
        raise WorkspaceTransitionError(
            f"bare_metal_servers row {box.server_id} has no usable cpu_threads/cpu_overcommit_ratio; "
            "re-register the box"
        )
    return compute_machine_vcpus(box.cpu_threads, box.cpu_overcommit_ratio, units, total_units)


# Safety margin (GiB) the gen-2 restore's df guard requires beyond the
# machine's own virtual size, mirroring the carve-time guard's margin.
_GEN2_RESTORE_DF_MARGIN_GIB: Final[int] = 2


def _gen2_restore_required_free_bytes(manifest: ArtifactManifest, sizing: _Gen2MachineSizing) -> int:
    """The df-guard requirement for restoring this machine: its measured (or applied) virtual sizes + margin."""
    boot_bytes = (
        manifest.disk_virtual_bytes if manifest.disk_virtual_bytes is not None else GEN2_BOOT_DISK_GIB * 1024**3
    )
    data_bytes = sizing.data_disk_gib * 1024**3
    return boot_bytes + data_bytes + _GEN2_RESTORE_DF_MARGIN_GIB * 1024**3


def _render_gen2_reserve_script(row: WorkspaceRow, box: BoxRow, manifest: ArtifactManifest, instance_name: str) -> str:
    """Render the gen-2 restore-reserve for one candidate box.

    The env file ships as a single template whose ordinal-derived values the
    box substitutes under the allocation lock; the cidata is the artifact's
    own (user-data with the VM's pinned host key, the stable-id meta-data and
    the DHCP network-config), copied verbatim so cloud-init never reruns on
    the new placement. The machine is reserved at its APPLIED size (the
    pending resize target when one is stamped) -- a restore at the target
    size IS the resize application.
    """
    sizing = _gen2_machine_sizing(row)
    total_units, unit_budget_mib, disk_budget_gib = _gen2_box_budgets(box)
    vcpus = _gen2_machine_vcpus_for_box(box, sizing.units, total_units)
    env_template_b64 = base64.b64encode(
        build_qemu_slice_env_file(
            instance_name=instance_name,
            ordinal=None,
            vcpus=vcpus,
            units=sizing.units,
            total_units=total_units,
            data_disk_gib=sizing.data_disk_gib,
            vm_ssh_host_port=GEN2_VM_SSH_PORT_PLACEHOLDER,
            container_ssh_host_port=GEN2_CONTAINER_SSH_PORT_PLACEHOLDER,
            uplink_mbps=box.uplink_mbps,
        ).encode()
    ).decode()
    return render_gen2_restore_reserve_script(
        instance_name=instance_name,
        units=sizing.units,
        data_disk_gib=sizing.data_disk_gib,
        unit_budget_mib=unit_budget_mib,
        disk_budget_gib=disk_budget_gib,
        required_free_bytes=_gen2_restore_required_free_bytes(manifest, sizing),
        expected_meta_sha=manifest.object_by_name["META"].sha256,
        env_template_b64=env_template_b64,
    )


def _attempt_reserve_on_box(
    config: StorageConfig,
    row: WorkspaceRow,
    box: BoxRow,
    manifest: ArtifactManifest,
    instance_name: str,
    identity: str,
    # ((vm_port, container_port, gen2_ordinal|None) | None, hard-failure text).
    # (None, None) means a clean capacity refusal (eviction may help there).
) -> tuple[tuple[int, int, int | None] | None, str | None]:
    """One restore-reserve attempt on one candidate box, with its rollback on failure."""
    is_candidate_gen2 = _is_gen2(box.box_generation)
    disk_name = row.slice_disk_name or ""
    _write_box_file(box, instance_name, "env", _transfer_env_for(config, manifest, instance_name, identity))
    if is_candidate_gen2:
        reserve_script = _render_gen2_reserve_script(row, box, manifest, instance_name)
    else:
        reserve_script = box_scripts.render_restore_reserve_script(
            instance_name=instance_name,
            disk_name=disk_name,
            slot_count=box.slot_count,
            old_vm_ssh_port=manifest.source_vm_ssh_port,
            old_container_ssh_port=manifest.source_container_ssh_port,
            expected_meta_sha=manifest.object_by_name["META"].sha256,
        )
    _write_box_file(box, instance_name, "reserve.sh", reserve_script)
    instance_transfer_dir = transfer_dir(instance_name)
    exit_status, stdout, stderr = _run_box_command(
        box, f'bash "{instance_transfer_dir}/reserve.sh"', timeout_seconds=_VM_START_TIMEOUT_SECONDS
    )
    if exit_status == 0:
        if is_candidate_gen2:
            gen2_ports = parse_gen2_restore_reserved_line(stdout)
            reservation = (gen2_ports[0], gen2_ports[1], gen2_ports[2]) if gen2_ports is not None else None
        else:
            gen1_ports = box_scripts.parse_reserved_ports_line(stdout)
            reservation = (gen1_ports[0], gen1_ports[1], None) if gen1_ports is not None else None
        if reservation is not None:
            return reservation, None
        # The reserve claimed a slot we cannot use without its ports: roll
        # the claim (and the staged creds) back before moving on.
        logger.warning("Reserve for %s on %s printed no ports; trying another box", row.host_db_id, box.public_address)
        _cleanup_reserved_restore(box, instance_name, disk_name)
        return None, f"reserve on {box.public_address} printed no ports: {stdout[-500:]!r}"
    if (
        box_scripts.RESTORE_BOX_FULL_MARKER in stderr
        or RESTORE_NO_PORTS_MARKER in stderr
        or RESTORE_NO_SPACE_MARKER in stderr
        or GEN2_NO_UNITS_MARKER in stderr
        or GEN2_NO_DISK_MARKER in stderr
    ):
        logger.info("Box %s has no capacity for %s; trying another", box.public_address, row.host_db_id)
        # Do not leave the staged env (S3 creds + age identity) on a box
        # that will not host the restore.
        _remove_transfer_dir(box, instance_name)
        return None, None
    # A hard reserve failure (a broken or drifted box, e.g. missing
    # transfer tooling) may have materialized partial instance/disk state
    # before its ERR trap fired; drop it (and the staged env) so the box
    # holds nothing for a restore that is not happening here -- then let the
    # caller try the remaining candidates rather than failing the whole start
    # over one bad box.
    logger.warning(
        "Reserve for %s failed on %s; trying another box: %s", row.host_db_id, box.public_address, stderr.strip()
    )
    _cleanup_reserved_restore(box, instance_name, disk_name)
    return None, f"reserve on {box.public_address} failed (exit {exit_status}): {stderr.strip()}"


class _EvictionPlan(BaseModel):
    """The unleased rows to destroy on one box to make room for a machine."""

    box: BoxRow = Field(description="The candidate box the evictions happen on")
    pool_host_ids: list[str] = Field(description="The unleased available rows to destroy, in eviction order")


def _plan_eviction_for_box(
    conn: Any,
    box: BoxRow,
    needed_units: int,
    needed_disk_gib: int,
    # The restoring machine's own row id: its on-box footprint is excluded
    # from the sum (the reserve reclaims the same-instance dir before the
    # capacity guard), and its new footprint is exactly the needed size.
    restoring_pool_host_id: str,
) -> _EvictionPlan | None:
    """The fewest unleased rows on ``box`` whose destruction fits the machine, or None.

    The estimate uses the DB's view of the box (this env's rows and their
    recorded sizes) -- a heuristic, since other envs' machines are invisible
    here. The authoritative guard remains the on-box reserve, which is re-run
    after the evictions.
    """
    if box.ram_gb is None or box.ram_gb <= HOST_RAM_RESERVE_GIB or box.disk_gb is None:
        return None
    unit_budget_mib = compute_box_total_units(box.ram_gb) * 1024
    disk_budget_gib = compute_gen2_disk_budget_gib(box.disk_gb)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, memory_units, disk_gb FROM pool_hosts "
            "WHERE bare_metal_server_id = %s ORDER BY created_at ASC",
            (box.server_id,),
        )
        placed_rows = cur.fetchall()
    used_mib = 0
    used_disk_gib = 0
    evictable: list[tuple[str, int, int]] = []
    for pool_host_id, status, memory_units, machine_disk_gb in placed_rows:
        if str(pool_host_id) == restoring_pool_host_id:
            continue
        footprint_mib = int(memory_units) * 1024 + PER_VM_RAM_OVERHEAD_MIB
        footprint_disk = GEN2_BOOT_DISK_GIB + int(machine_disk_gb)
        used_mib += footprint_mib
        used_disk_gib += footprint_disk
        if status == "available":
            evictable.append((str(pool_host_id), footprint_mib, footprint_disk))
    needed_mib = needed_units * 1024 + PER_VM_RAM_OVERHEAD_MIB
    needed_disk = GEN2_BOOT_DISK_GIB + needed_disk_gib
    chosen: list[str] = []
    for pool_host_id, footprint_mib, footprint_disk in evictable:
        if used_mib + needed_mib <= unit_budget_mib and used_disk_gib + needed_disk <= disk_budget_gib:
            break
        chosen.append(pool_host_id)
        used_mib -= footprint_mib
        used_disk_gib -= footprint_disk
    if used_mib + needed_mib > unit_budget_mib or used_disk_gib + needed_disk > disk_budget_gib:
        return None
    return _EvictionPlan(box=box, pool_host_ids=chosen)


def _evict_pool_rows(conn: Any, box: BoxRow, pool_host_ids: list[str]) -> int:
    """Destroy the planned unleased rows (CAS-claimed so a concurrent lease cannot grab one mid-destroy).

    Returns how many were actually destroyed; a row that got leased between
    the plan and the claim is simply skipped (its capacity stays used, and the
    re-run reserve is the authoritative check either way).
    """
    evicted_count = 0
    for pool_host_id in pool_host_ids:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'removing', released_at = NOW() "
                "WHERE id = %s AND status = 'available' "
                "RETURNING COALESCE(slice_instance_name, lima_instance_name), "
                "COALESCE(slice_disk_name, lima_disk_name), box_generation",
                (pool_host_id,),
            )
            claimed = cur.fetchone()
        conn.commit()
        if claimed is None:
            logger.info("Eviction candidate %s was leased concurrently; skipping it", pool_host_id)
            continue
        evicted_instance_name, evicted_disk_name, evicted_generation = claimed
        if not evicted_instance_name:
            logger.warning("Eviction candidate %s has no instance name; dropping its row only", pool_host_id)
        else:
            _teardown_superseded_restore(box, evicted_instance_name, evicted_disk_name or "")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pool_hosts WHERE id = %s", (pool_host_id,))
        conn.commit()
        evicted_count += 1
    if evicted_count:
        emit_metric("pool_rows_evicted", evicted_count, {"box": box.public_address})
    return evicted_count


def _reserve_with_eviction(
    config: StorageConfig,
    row: WorkspaceRow,
    manifest: ArtifactManifest,
    instance_name: str,
    identity: str,
    capacity_refused_boxes: list[BoxRow],
    origin_box_holding_vm: BoxRow | None,
) -> tuple[BoxRow, int, int, int | None] | None:
    """The eviction pass: destroy just enough unleased rows on the best box and retry the reserve."""
    sizing = _gen2_machine_sizing(row)
    with db.pooled_db_connection() as conn:
        plans = [
            plan
            for box in capacity_refused_boxes
            if (
                plan := _plan_eviction_for_box(
                    conn, box, sizing.units, sizing.data_disk_gib, restoring_pool_host_id=row.host_db_id
                )
            )
            is not None
            and plan.pool_host_ids
        ]
        # Fewest evictions first; the origin box wins ties (an in-place
        # fallback stays home when possible, keeping its warm disks).
        origin_server_id = origin_box_holding_vm.server_id if origin_box_holding_vm is not None else None
        plans.sort(key=lambda plan: (len(plan.pool_host_ids), 0 if plan.box.server_id == origin_server_id else 1))
        for plan in plans:
            _assert_owned(row, "starting")
            logger.info(
                "Evicting %d unleased pool row(s) on %s to fit workspace %s",
                len(plan.pool_host_ids),
                plan.box.public_address,
                row.host_db_id,
            )
            _evict_pool_rows(conn, plan.box, plan.pool_host_ids)
            reservation, _failure_text = _attempt_reserve_on_box(
                config, row, plan.box, manifest, instance_name, identity
            )
            if reservation is not None:
                return (plan.box, reservation[0], reservation[1], reservation[2])
    return None


def _restore_from_artifact(
    config: StorageConfig,
    row: WorkspaceRow,
    # The origin box still holding the row's halted local VM when the restore
    # was forced past it (a draining origin, or an in-place resize the origin
    # could not fit); best-effort deleted once the restore lands elsewhere,
    # since nothing else ever will.
    origin_box_holding_vm: BoxRow | None,
) -> str:
    """Slow path: reserve a slot on a same-region, same-generation box, download the artifact, boot it."""
    instance_name, disk_name = _require_slice_names(row)
    manifest = row.artifact_manifest
    if manifest is None or row.wrapped_dek is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no artifact to restore")
    missing_names = _missing_manifest_object_names(manifest)
    if missing_names:
        raise WorkspaceTransitionError(
            f"workspace {row.host_db_id} artifact manifest is missing {', '.join(missing_names)}"
        )
    identity = storage_module.unwrap_dek(config, row.wrapped_dek)

    with db.pooled_db_connection() as conn:
        candidates = _list_candidate_boxes(conn, row.region, box_generation=row.box_generation)
    if not candidates:
        raise WorkspaceTransitionError("no capacity available right now, try again later")
    random.shuffle(candidates)

    # Pass 1: try every candidate as-is. Gen-2 boxes that refused for capacity
    # are remembered for the eviction pass.
    reserved: tuple[BoxRow, int, int, int | None] | None = None
    last_hard_failure: str | None = None
    capacity_refused_gen2_boxes: list[BoxRow] = []
    for box in candidates:
        # Each reserve attempt can run for minutes; keep the heartbeat fresh
        # (and notice a takeover) between candidates.
        _assert_owned(row, "starting")
        reservation, failure_text = _attempt_reserve_on_box(config, row, box, manifest, instance_name, identity)
        if reservation is not None:
            reserved = (box, reservation[0], reservation[1], reservation[2])
            break
        if failure_text is None:
            # A clean capacity refusal: an eviction there may make room.
            if _is_gen2(box.box_generation):
                capacity_refused_gen2_boxes.append(box)
            continue
        last_hard_failure = failure_text
    # Pass 2 (specs/slice-fleet): unleased ``available`` pool rows are just
    # pre-baked caches, so when every candidate refused for capacity, destroy
    # just enough of them (fewest-evictions box first, the origin box
    # preferred among ties) and retry the reserve there.
    if reserved is None and capacity_refused_gen2_boxes:
        reserved = _reserve_with_eviction(
            config,
            row,
            manifest,
            instance_name,
            identity,
            capacity_refused_gen2_boxes,
            origin_box_holding_vm,
        )
    if reserved is None:
        if last_hard_failure is not None:
            raise WorkspaceTransitionError(
                f"no box could host the restore ({len(candidates)} candidate(s) tried); last failure: "
                f"{last_hard_failure}"
            )
        if row.target_memory_units is not None or row.target_disk_gb is not None:
            emit_metric("machine_resize_placement_impossible", 1, {})
            raise WorkspaceTransitionError(
                "this machine size is not possible right now (no box can fit it, even after eviction); "
                "try a smaller size or try again later"
            )
        raise WorkspaceTransitionError("no capacity available right now, try again later")
    box, vm_ssh_port, container_ssh_port, gen2_ordinal = reserved

    try:
        if gen2_ordinal is not None:
            restore_sizing = _gen2_machine_sizing(row)
            download_script = render_gen2_download_script(
                instance_name=instance_name,
                ordinal=gen2_ordinal,
                expected_sha_by_name={name: obj.sha256 for name, obj in manifest.object_by_name.items()},
                vm_ssh_port=vm_ssh_port,
                container_ssh_port=container_ssh_port,
                grow_data_disk_gib=restore_sizing.grow_data_disk_gib,
            )
        else:
            download_script = box_scripts.render_download_script(
                instance_name=instance_name,
                disk_name=disk_name,
                expected_sha_by_name={name: obj.sha256 for name, obj in manifest.object_by_name.items()},
                vm_ssh_port=vm_ssh_port,
                container_ssh_port=container_ssh_port,
            )
        _write_box_file(box, instance_name, _DOWNLOAD_SCRIPT_FILENAME, download_script)
        launch_status, _lo, launch_err = _run_box_command(
            box, build_launch_detached_command(instance_name, _DOWNLOAD_SCRIPT_FILENAME)
        )
        if launch_status != 0:
            raise WorkspaceTransitionError(f"failed to launch download: {launch_err.strip()}")
        _poll_transfer(row, box, instance_name, expected_status="starting")
    except (WorkspaceTransitionError, _TransitionSuperseded, paramiko.SSHException, OSError):
        # Superseded counts too (the row was released or abandoned mid
        # -restore): nothing else can ever reclaim the claimed slot on the
        # candidate box, because the row's box link never pointed at it.
        _cleanup_reserved_restore(box, instance_name, disk_name)
        raise

    # The restore is done with the transfer dir: drop it so the env file
    # (S3 creds + age identity) does not linger on the box, and so a later
    # stop on this box never mistakes the download's status for its upload's.
    _remove_transfer_dir(box, instance_name)

    # The applied sizing the final CAS restamps: a gen-2 landing restores at
    # the machine's applied size (the pending target when stamped); a gen-1
    # landing applies no resize (gen-1 rows refuse them).
    if gen2_ordinal is not None:
        applied_sizing = _gen2_machine_sizing(row)
        applied_units: int | None = applied_sizing.units
        applied_disk_gb: int | None = applied_sizing.data_disk_gib
    else:
        applied_units = None
        applied_disk_gb = None

    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # A successful restore IS the resize application: the row's
                # current size becomes the applied size and the targets clear.
                "UPDATE pool_hosts SET status = 'leased', vps_address = %s, ssh_port = %s, "
                "container_ssh_port = %s, bare_metal_server_id = %s, box_generation = %s, "
                "memory_units = COALESCE(%s, memory_units), disk_gb = COALESCE(%s, disk_gb), "
                "target_memory_units = NULL, target_disk_gb = NULL, "
                "stop_requested_at = NULL, "
                "stopped_at = NULL, transition_error = NULL, transition_failure_count = 0, "
                "transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'starting' AND transition_id = %s",
                (
                    box.public_address,
                    vm_ssh_port,
                    container_ssh_port,
                    box.server_id,
                    box.box_generation,
                    applied_units,
                    applied_disk_gb,
                    row.host_db_id,
                    row.transition_id,
                ),
            )
            updated = cur.rowcount
        conn.commit()
    if updated == 0:
        # The row moved on (released or abandoned) after the download booted
        # the VM. As in the mid-download case, nothing else can ever reclaim
        # this box's slot -- the row's box link never pointed at it -- but
        # here the VM is already running, so it needs a real delete, not just
        # a directory rollback.
        _teardown_superseded_restore(box, instance_name, disk_name)
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    if origin_box_holding_vm is not None and origin_box_holding_vm.server_id != box.server_id:
        # A drain- or resize-forced restore left the halted local VM behind on
        # the origin box; nothing else will ever delete it (the row now points
        # elsewhere), so reap it best-effort -- a failure surfaces in the
        # reconcile sweep. Skipped when the restore landed back ON the origin
        # box: its reserve already reclaimed the leftover dir, and the "old"
        # VM is now the restored one.
        _delete_abandoned_origin_vm(origin_box_holding_vm, row)
    logger.info(
        "Workspace %s restored on %s (generation %d, ports vm=%d/container=%d)",
        row.host_db_id,
        box.public_address,
        box.box_generation,
        vm_ssh_port,
        container_ssh_port,
    )
    return "restored"


def _delete_abandoned_origin_vm(origin_box: BoxRow, row: WorkspaceRow) -> None:
    """Best-effort delete of the halted local VM left on a draining origin box."""
    try:
        _run_box_commands_checked(origin_box, _finalize_stop_commands_for(row), _VM_STOP_TIMEOUT_SECONDS)
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.warning(
            "Could not delete the abandoned local VM of %s on draining box %s (the reconcile sweep will flag it)",
            row.host_db_id,
            origin_box.public_address,
            exc_info=exc,
        )


def _remove_transfer_dir(box: BoxRow, instance_name: str) -> None:
    """Best-effort removal of the instance's transfer dir (creds + status + logs)."""
    instance_transfer_dir = transfer_dir(instance_name)
    try:
        exit_status, _stdout, stderr = _run_box_command(box, f'rm -rf "{instance_transfer_dir}"')
    except (paramiko.SSHException, OSError) as exc:
        logger.warning("Could not remove transfer dir for %s on %s", instance_name, box.public_address, exc_info=exc)
        return
    if exit_status != 0:
        logger.warning(
            "Could not remove transfer dir for %s on %s: %s", instance_name, box.public_address, stderr.strip()
        )


def _cleanup_reserved_restore(box: BoxRow, instance_name: str, disk_name: str) -> None:
    """Best-effort rollback of a claimed restore slot after a failed download/boot.

    Dispatches on the CANDIDATE box's generation (the claimed state lives in
    that box's layout, whatever generation the row itself records). The
    delete-failed marker is only ever emitted by the gen-1 script (the gen-2
    destroy tolerates every partial state), so the check is a no-op there.
    """
    if _is_gen2(box.box_generation):
        cleanup_commands = box_scripts_gen2.build_gen2_cleanup_reserved_restore_commands(instance_name)
    else:
        cleanup_commands = box_scripts.build_cleanup_reserved_restore_commands(instance_name, disk_name)
    command = " && ".join(cleanup_commands)
    try:
        exit_status, _stdout, stderr = _run_box_command(box, command, timeout_seconds=_VM_STOP_TIMEOUT_SECONDS)
    except (paramiko.SSHException, OSError) as exc:
        logger.warning(
            "Could not roll back reserved restore for %s on %s", instance_name, box.public_address, exc_info=exc
        )
        return
    if exit_status != 0:
        logger.warning(
            "Could not roll back reserved restore for %s on %s (exit %d): %s",
            instance_name,
            box.public_address,
            exit_status,
            stderr.strip(),
        )
        return
    if box_scripts.CLEANUP_DELETE_FAILED_MARKER in stderr:
        logger.warning(
            "Restore VM %s survived the rollback on %s; its instance and disk dirs were kept for a later sweep: %s",
            instance_name,
            box.public_address,
            stderr.strip(),
        )


def _teardown_superseded_restore(box: BoxRow, instance_name: str, disk_name: str) -> None:
    """Best-effort teardown of a booted restore VM whose row moved on before the final CAS."""
    if _is_gen2(box.box_generation):
        finalize_commands = box_scripts_gen2.build_gen2_finalize_stop_commands(instance_name)
    else:
        finalize_commands = box_scripts.build_finalize_stop_commands(instance_name, disk_name)
    try:
        _run_box_commands_checked(box, finalize_commands, _VM_STOP_TIMEOUT_SECONDS)
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.warning(
            "Could not tear down superseded restore for %s on %s", instance_name, box.public_address, exc_info=exc
        )


# Watchdog


# A row the watchdog is responsible for: mid-transition, or stopped with its
# local VM (box link) not yet reaped by the retention finalize. The takeover
# claim re-checks this same predicate, so the two must never drift.
_IN_FLIGHT_ROW_PREDICATE_SQL: Final[str] = (
    "(status IN ('stopping', 'starting') OR (status = 'stopped' AND bare_metal_server_id IS NOT NULL))"
)


class _WatchdogCandidate(BaseModel):
    """One in-flight (or unfinalized-stop) row the watchdog may need to re-drive."""

    host_db_id: str = Field(description="pool_hosts row id")
    status: str = Field(description="Current lifecycle status")
    failure_count: int = Field(description="Consecutive failed drives of this transition")
    heartbeat_age_seconds: float | None = Field(description="Age of the last heartbeat; None when never stamped")


def _redrive_delay_seconds(failure_count: int) -> float:
    """How stale a heartbeat must be before a re-drive, backing off in the failure count."""
    # The exponent is clamped: the delay already saturates at the cap well
    # below the clamp, and an unbounded failure count would eventually make
    # the float pow overflow (crashing the whole watchdog run).
    exponent = min(failure_count, _WATCHDOG_BACKOFF_MAX_EXPONENT)
    return min(_WATCHDOG_BACKOFF_CAP_SECONDS, float(STALE_HEARTBEAT_SECONDS) * (2.0**exponent))


def _find_watchdog_candidates() -> list[_WatchdogCandidate]:
    """Rows with an in-flight transition (or unfinalized stop), with liveness + failure data."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, transition_failure_count, "
                "EXTRACT(EPOCH FROM (NOW() - transition_heartbeat_at)) FROM pool_hosts "
                f"WHERE {_IN_FLIGHT_ROW_PREDICATE_SQL}"
            )
            rows = cur.fetchall()
    return [
        _WatchdogCandidate(
            host_db_id=str(row[0]),
            status=row[1],
            failure_count=int(row[2] or 0),
            heartbeat_age_seconds=float(row[3]) if row[3] is not None else None,
        )
        for row in rows
    ]


def _take_over_transition(host_db_id: str) -> str | None:
    """Claim an orphaned transition with a fresh fencing token; None when it is live after all.

    The claim re-checks staleness so a supervisor that heartbeated between the
    candidate read and this write is left alone; setting the heartbeat in the
    same statement keeps an overlapping watchdog run from double-claiming. It
    also re-checks the in-flight statuses, because a transition that completed
    in that window nulls its heartbeat -- which would otherwise read as stale
    -- and its settled row must not be stamped with a fresh token.
    """
    new_transition_id = str(uuid4())
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET transition_id = %s, transition_heartbeat_at = NOW() "
                f"WHERE id = %s AND {_IN_FLIGHT_ROW_PREDICATE_SQL} "
                "AND (transition_heartbeat_at IS NULL OR "
                f"transition_heartbeat_at < NOW() - INTERVAL '{STALE_HEARTBEAT_SECONDS} seconds')",
                (new_transition_id, host_db_id),
            )
            updated = cur.rowcount
        conn.commit()
    return new_transition_id if updated == 1 else None


def run_transition_watchdog() -> int:
    """Take over and re-drive every orphaned transition; returns how many were re-driven.

    Crash recovery with bounded persistence-handling: a row whose supervisor
    died gets a fresh one (under a fresh fencing token, so an alive-but-wedged
    driver is fenced out rather than dueled), a row that keeps failing is
    re-driven ever less often, and one that has failed many consecutive times
    is escalated to ops at error level while the backed-off retries continue.
    """
    if not storage_module.is_storage_configured():
        logger.info("Transition watchdog skipped: workspace storage is not configured for this env")
        return 0
    redriven_count = 0
    for candidate in _find_watchdog_candidates():
        delay_seconds = _redrive_delay_seconds(candidate.failure_count)
        if candidate.heartbeat_age_seconds is not None and candidate.heartbeat_age_seconds < delay_seconds:
            continue
        new_transition_id = _take_over_transition(candidate.host_db_id)
        if new_transition_id is None:
            continue
        # Escalate only after the claim lands: a lost claim means a live
        # supervisor heartbeated in the window, so the transition is being
        # driven and ops must not be paged for it.
        if candidate.failure_count >= _ESCALATION_FAILURE_COUNT:
            logger.error(
                "Workspace transition for %s (status=%s) has failed %d consecutive times; "
                "it needs operator attention (last error is on pool_hosts.transition_error)",
                candidate.host_db_id,
                candidate.status,
                candidate.failure_count,
            )
        logger.info("Re-driving orphaned transition for %s (status=%s)", candidate.host_db_id, candidate.status)
        spawn_supervisor(candidate.host_db_id, new_transition_id)
        redriven_count += 1
    return redriven_count
