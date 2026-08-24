"""Workspace stop/start transition supervisor.

One supervisor run drives one workspace's in-flight transition to completion:

* ``stopping``: halt the VM, generate + wrap the per-stop age identity,
  launch the box-side upload, wait for it to verify, honor the local
  retention window, then free the slot (delete the VM, null the placement)
  and land the row on ``stopped``.
* ``starting``: restart in place when the VM still exists on its origin box
  (an in-window restart), otherwise reserve a slot on a random same-region
  box, restore the artifact there, boot it, and land the row on ``leased``
  with its new coordinates.

Supervisors are spawned by the stop/start endpoints (via the hook the Modal
entrypoint wires) and re-spawned by the watchdog cron whenever an in-flight
row's heartbeat goes stale -- every step is idempotent, so a re-driven
transition converges. The row's status is the single source of truth: every
finalization is a compare-and-swap on it, so a user restart during the
retention window cleanly supersedes the stop.
"""

import json
import logging
import os
import random
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final

import paramiko
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.hosts as hosts_module
import imbue.remote_service_connector.storage as storage_module
from imbue.remote_service_connector import box_scripts
from imbue.remote_service_connector import db
from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.errors import WorkspaceTransitionError
from imbue.remote_service_connector.storage import StorageConfig

logger = logging.getLogger(__name__)

# How often a supervisor polls the box status file (and heartbeats the row).
_POLL_SECONDS: Final[float] = 15.0
# A transition whose heartbeat is older than this is considered orphaned and
# gets a fresh supervisor from the watchdog cron.
STALE_HEARTBEAT_SECONDS: Final[int] = 120
# Bound for one supervisor's polling of a single transfer. The enclosing
# Modal function timeout is the hard stop; this keeps a wedged transfer from
# consuming the entire function timeout before the watchdog can see it.
_TRANSFER_WAIT_SECONDS: Final[float] = 6000.0
_SSH_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
_VM_STOP_TIMEOUT_SECONDS: Final[float] = 300.0
_VM_START_TIMEOUT_SECONDS: Final[float] = 600.0

# Lease-region labels -> the OVH datacenter codes recorded on
# ``bare_metal_servers.region``. Mirrors ``mngr_imbue_cloud.primitives``
# (the shipped connector must not import the monorepo).
_REGION_LABEL_TO_DATACENTER: Final[dict[str, str]] = {"US-EAST-VA": "vin", "US-WEST-OR": "hil"}

# Names of the artifact objects tracked in the manifest, in upload order.
_OBJECT_NAMES: Final[tuple[str, ...]] = ("DISK", "DATADISK", "META")

_UPLOAD_SCRIPT_FILENAME: Final[str] = "upload.sh"
_DOWNLOAD_SCRIPT_FILENAME: Final[str] = "download.sh"


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


class WorkspaceRow(BaseModel):
    """The pool_hosts columns a transition supervisor works from."""

    host_db_id: str = Field(description="pool_hosts row id (UUID as string)")
    status: str = Field(description="Lifecycle status")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix")
    host_id: str = Field(description="mngr host id (host-<32hex>)")
    vps_address: str | None = Field(description="Box public address (NULL when stopped)")
    ssh_port: int | None = Field(description="VM-root forwarded port (NULL when stopped)")
    ssh_user: str = Field(description="SSH user on the VM (root)")
    container_ssh_port: int | None = Field(description="Container forwarded port (NULL when stopped)")
    bare_metal_server_id: str | None = Field(description="Owning box row id (kept until the VM is deleted)")
    lima_instance_name: str | None = Field(description="Slice lima instance name")
    lima_disk_name: str | None = Field(description="Slice lima data-disk name")
    region: str | None = Field(description="Lease-region label (e.g. US-EAST-VA)")
    stop_requested_at: datetime | None = Field(description="When the stop was requested")
    artifact_manifest: ArtifactManifest | None = Field(description="Uploaded artifact coordinates")
    wrapped_dek: str | None = Field(description="KEK-wrapped age identity")
    artifact_generation: int = Field(description="Last fully-uploaded artifact generation")


class BoxRow(BaseModel):
    """The bare_metal_servers columns needed to reach a box."""

    server_id: str = Field(description="bare_metal_servers row id (UUID as string)")
    public_address: str = Field(description="SSH-reachable public address")
    lima_service_user: str = Field(description="Non-root lima user that owns the VMs")
    box_host_public_key: str = Field(description="Pinned sshd host public key")
    slot_count: int = Field(description="Slices the box holds when full")


class _SupervisorSpawner(BaseModel):
    """Holder for the spawn hook the Modal entrypoint wires at import time."""

    hook: Callable[[str], None] | None = None


spawner = _SupervisorSpawner()


def spawn_supervisor(host_db_id: str) -> None:
    """Spawn a detached transition supervisor for the row (no-op with a warning when unwired)."""
    if spawner.hook is None:
        logger.warning(
            "No supervisor spawn hook wired; transition for %s will be driven by the watchdog cron", host_db_id
        )
        return
    spawner.hook(host_db_id)


# ---------------------------------------------------------------------------
# Row / box access (thin SQL wrappers; the fake DB in tests emulates these)
# ---------------------------------------------------------------------------

_WORKSPACE_ROW_SELECT: Final[str] = (
    "SELECT id, status, leased_to_user, host_id, vps_address, ssh_port, ssh_user, container_ssh_port, "
    "bare_metal_server_id, lima_instance_name, lima_disk_name, region, stop_requested_at, "
    "artifact_manifest, wrapped_dek, artifact_generation "
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
        lima_instance_name=row[9],
        lima_disk_name=row[10],
        region=row[11],
        stop_requested_at=row[12],
        artifact_manifest=manifest,
        wrapped_dek=row[14],
        artifact_generation=int(row[15] or 0),
    )


def read_workspace_row(conn: Any, host_db_id: str) -> WorkspaceRow | None:
    with conn.cursor() as cur:
        cur.execute(_WORKSPACE_ROW_SELECT, (host_db_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _workspace_row_from_tuple(row)


def _read_box_row(conn: Any, server_id: str) -> BoxRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, public_address, lima_service_user, box_host_public_key, slot_count "
            "FROM bare_metal_servers WHERE id = %s",
            (server_id,),
        )
        row = cur.fetchone()
    if row is None or not row[1] or not row[3]:
        return None
    return BoxRow(
        server_id=str(row[0]),
        public_address=row[1],
        lima_service_user=row[2] or "root",
        box_host_public_key=row[3],
        slot_count=int(row[4] or 0),
    )


def _list_candidate_boxes(conn: Any, region_label: str | None) -> list[BoxRow]:
    """Every ready box eligible to host a restore, filtered to the row's region when known."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, public_address, lima_service_user, box_host_public_key, slot_count, region "
            "FROM bare_metal_servers WHERE status = 'ready'"
        )
        rows = cur.fetchall()
    datacenter = _REGION_LABEL_TO_DATACENTER.get(region_label) if region_label else None
    boxes: list[BoxRow] = []
    for row in rows:
        if not row[1] or not row[3]:
            continue
        if datacenter is not None and row[5] and row[5] != datacenter:
            continue
        boxes.append(
            BoxRow(
                server_id=str(row[0]),
                public_address=row[1],
                lima_service_user=row[2] or "root",
                box_host_public_key=row[3],
                slot_count=int(row[4] or 0),
            )
        )
    return boxes


def _heartbeat(host_db_id: str) -> None:
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pool_hosts SET transition_heartbeat_at = NOW() WHERE id = %s", (host_db_id,))
        conn.commit()
    finally:
        conn.close()


def _current_status(host_db_id: str) -> str | None:
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pool_hosts WHERE id = %s", (host_db_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _record_transition_error(host_db_id: str, message: str) -> None:
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET transition_error = %s WHERE id = %s",
                (message[:2000], host_db_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Box SSH seams (faked in tests)
# ---------------------------------------------------------------------------


def _management_key_pem() -> str:
    return os.environ["POOL_SSH_PRIVATE_KEY"]


def _run_box_command(
    box: BoxRow,
    command: str,
    input_text: str | None = None,
    timeout_seconds: float = _SSH_COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run one command on the box as the lima user; return (rc, stdout, stderr)."""
    with hosts_module.management_ssh_client(
        box.public_address,
        22,
        box.lima_service_user,
        _management_key_pem(),
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
    for command in commands:
        exit_status, _stdout, stderr = _run_box_command(box, command, timeout_seconds=timeout_seconds)
        if exit_status != 0:
            raise WorkspaceTransitionError(f"box command {command!r} failed (exit {exit_status}): {stderr.strip()}")


def _write_box_file(box: BoxRow, instance_name: str, filename: str, content: str) -> None:
    transfer_dir = f"$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{instance_name}"
    command = f'umask 077 && mkdir -p "{transfer_dir}" && cat > "{transfer_dir}/{filename}"'
    exit_status, _stdout, stderr = _run_box_command(box, command, input_text=content)
    if exit_status != 0:
        raise WorkspaceTransitionError(f"failed to write {filename} on {box.public_address}: {stderr.strip()}")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------


def run_transition_supervisor(host_db_id: str) -> str:
    """Drive one workspace's in-flight transition to completion; returns an outcome label."""
    config = storage_module.read_storage_config()
    conn = db.get_pool_db_connection()
    try:
        row = read_workspace_row(conn, host_db_id)
    finally:
        conn.close()
    if row is None:
        return "row-gone"
    if row.status == "stopping":
        return _drive_stop(config, row)
    if row.status == "starting":
        return _drive_start(config, row)
    if row.status == "stopped" and row.bare_metal_server_id is not None:
        # A previous supervisor landed the row on ``stopped`` but died before
        # deleting the local VM; finish freeing the slot.
        return _finalize_stopped_leftover(config, row)
    logger.info("Supervisor for %s: nothing to do (status=%s)", host_db_id, row.status)
    return "no-op"


def _require_box(row: WorkspaceRow) -> BoxRow:
    if row.bare_metal_server_id is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no bare_metal_server_id")
    conn = db.get_pool_db_connection()
    try:
        box = _read_box_row(conn, row.bare_metal_server_id)
    finally:
        conn.close()
    if box is None:
        raise WorkspaceTransitionError(
            f"workspace {row.host_db_id}: bare_metal_servers row {row.bare_metal_server_id} is missing "
            "its address or pinned host key"
        )
    return box


def _require_lima_names(row: WorkspaceRow) -> tuple[str, str]:
    if not row.lima_instance_name or not row.lima_disk_name:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no lima instance/disk names recorded")
    return row.lima_instance_name, row.lima_disk_name


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
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET wrapped_dek = %s, artifact_manifest = %s WHERE id = %s AND status = 'stopping'",
                (wrapped, json.dumps(manifest.model_dump()), row.host_db_id),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if updated == 0:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} left 'stopping' before material was recorded")
    return manifest


def _transfer_env_for(
    config: StorageConfig, manifest: ArtifactManifest, instance_name: str, identity: str = ""
) -> str:
    return box_scripts.render_transfer_env(
        box_scripts.TransferEnv(
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
    exit_status, stdout, _stderr = _run_box_command(box, box_scripts.build_read_status_command(instance_name))
    if exit_status != 0 or not stdout.strip():
        return None
    status = box_scripts.parse_status_text(stdout)
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
    before finishing, or the row leaves ``expected_status`` (superseded).
    """
    deadline = time.monotonic() + _TRANSFER_WAIT_SECONDS
    while time.monotonic() < deadline:
        _heartbeat(row.host_db_id)
        current = _current_status(row.host_db_id)
        if current != expected_status:
            raise _TransitionSuperseded(current or "gone")
        finished = _read_finished_transfer_status(box, instance_name)
        if finished is not None:
            return finished
        alive_status, _out, _err = _run_box_command(box, box_scripts.build_is_transfer_alive_command(instance_name))
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
    return ArtifactManifest(
        generation=manifest.generation,
        key_prefix=manifest.key_prefix,
        age_recipient=manifest.age_recipient,
        source_vm_ssh_port=manifest.source_vm_ssh_port,
        source_container_ssh_port=manifest.source_container_ssh_port,
        object_by_name=objects,
    )


def _drive_stop(config: StorageConfig, row: WorkspaceRow) -> str:
    try:
        return _drive_stop_inner(config, row)
    except _TransitionSuperseded as exc:
        logger.info("Stop of %s superseded (row now %s)", row.host_db_id, exc.new_status)
        return "superseded"
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.error("Stop of %s failed", row.host_db_id, exc_info=exc)
        _record_transition_error(row.host_db_id, str(exc))
        return "stop-failed"


def _drive_stop_inner(config: StorageConfig, row: WorkspaceRow) -> str:
    box = _require_box(row)
    instance_name, disk_name = _require_lima_names(row)

    # Halt the VM and mark it so the box's autostart unit leaves it alone.
    _run_box_commands_checked(box, box_scripts.build_stop_vm_commands(instance_name), _VM_STOP_TIMEOUT_SECONDS)

    # Commit the encryption material before any byte leaves the box.
    manifest = _ensure_stop_artifact_material(config, row, box)

    # Stage the env + script, and launch the upload if it is not already
    # running (idempotent: a re-driven supervisor re-reads the status file).
    _write_box_file(box, instance_name, "env", _transfer_env_for(config, manifest, instance_name))
    _write_box_file(
        box, instance_name, _UPLOAD_SCRIPT_FILENAME, box_scripts.render_upload_script(instance_name, disk_name)
    )
    status_now, stdout_now, _stderr_now = _run_box_command(box, box_scripts.build_read_status_command(instance_name))
    parsed_now = box_scripts.parse_status_text(stdout_now) if status_now == 0 else {}
    alive_now, _o, _e = _run_box_command(box, box_scripts.build_is_transfer_alive_command(instance_name))
    # Only a status this upload wrote to completion counts: a stale one (a
    # failed earlier attempt, or a leftover download status from a previous
    # restore onto this box) must trigger a relaunch, which clears it.
    is_upload_complete = parsed_now.get("FINISHED") == "1" and parsed_now.get("STAGE") == "uploaded"
    if not is_upload_complete and alive_now != 0:
        launch_status, _lo, launch_err = _run_box_command(
            box, box_scripts.build_launch_detached_command(instance_name, _UPLOAD_SCRIPT_FILENAME)
        )
        if launch_status != 0:
            raise WorkspaceTransitionError(f"failed to launch upload: {launch_err.strip()}")

    final_status = _poll_transfer(row, box, instance_name, expected_status="stopping")
    manifest_with_objects = _manifest_with_objects(manifest, final_status)

    # Honor the remainder of the retention window (the user may still restart
    # in place); the CAS below closes the window atomically.
    retention_end = (row.stop_requested_at or _now()) + timedelta(seconds=config.retention_seconds)
    while _now() < retention_end:
        _heartbeat(row.host_db_id)
        current = _current_status(row.host_db_id)
        if current != "stopping":
            raise _TransitionSuperseded(current or "gone")
        _sleep(min(30.0, max(1.0, (retention_end - _now()).total_seconds())))

    # Atomically end the cancel window: once this lands, a concurrent start
    # takes the download path and the local VM is ours to delete.
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'stopped', stopped_at = NOW(), vps_address = NULL, "
                "ssh_port = NULL, container_ssh_port = NULL, artifact_manifest = %s, "
                "artifact_generation = %s, transition_error = NULL "
                "WHERE id = %s AND status = 'stopping'",
                (json.dumps(manifest_with_objects.model_dump()), manifest.generation, row.host_db_id),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")

    _delete_local_vm_and_previous_generation(config, row, box, previous_generation=row.artifact_generation)
    logger.info("Workspace %s stopped (generation %d uploaded, slot freed)", row.host_db_id, manifest.generation)
    return "stopped"


def _delete_local_vm_and_previous_generation(
    config: StorageConfig, row: WorkspaceRow, box: BoxRow, previous_generation: int
) -> None:
    """Free the slot and drop the superseded artifact generation, then clear the box link."""
    instance_name, disk_name = _require_lima_names(row)
    _run_box_commands_checked(
        box, box_scripts.build_finalize_stop_commands(instance_name, disk_name), _VM_STOP_TIMEOUT_SECONDS
    )
    if previous_generation > 0:
        storage_module.delete_prefix(
            config, f"{storage_module.workspace_key_prefix(config, row.host_id)}/gen-{previous_generation}/"
        )
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET bare_metal_server_id = NULL, transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'stopped'",
                (row.host_db_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _finalize_stopped_leftover(config: StorageConfig, row: WorkspaceRow) -> str:
    """Finish freeing the slot for a row that landed on ``stopped`` but kept its box link."""
    try:
        box = _require_box(row)
        # The superseded generation is whatever precedes the recorded one.
        _delete_local_vm_and_previous_generation(config, row, box, previous_generation=row.artifact_generation - 1)
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.error("Finalize of stopped %s failed", row.host_db_id, exc_info=exc)
        _record_transition_error(row.host_db_id, str(exc))
        return "finalize-failed"
    return "finalized"


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
    """Land a failed start back on ``stopped`` (artifact untouched) with the error recorded."""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pool_hosts SET status = 'stopped', transition_error = %s, "
                "transition_heartbeat_at = NULL, vps_address = NULL, ssh_port = NULL, "
                "container_ssh_port = NULL, bare_metal_server_id = NULL "
                "WHERE id = %s AND status = 'starting' AND stopped_at IS NOT NULL",
                (message[:2000], row.host_db_id),
            )
            updated = cur.rowcount
            if updated == 0:
                # The row never fully stopped (an in-window restart whose fast
                # path failed): keep its placement so a retry can reach the VM.
                cur.execute(
                    "UPDATE pool_hosts SET status = 'stopping', transition_error = %s "
                    "WHERE id = %s AND status = 'starting'",
                    (message[:2000], row.host_db_id),
                )
        conn.commit()
    finally:
        conn.close()


def _drive_start_inner(config: StorageConfig, row: WorkspaceRow) -> str:
    instance_name, _disk_name = _require_lima_names(row)
    if row.vps_address is not None and row.bare_metal_server_id is not None:
        box = _require_box(row)
        exists_status, _out, _err = _run_box_command(box, f'[ -d "$HOME/.lima/{instance_name}" ]')
        if exists_status == 0:
            return _restart_in_place(config, row, box)
    return _restore_from_artifact(config, row)


def _restart_in_place(config: StorageConfig, row: WorkspaceRow, box: BoxRow) -> str:
    """Fast path: the VM never left its origin box; cancel the upload and boot it."""
    instance_name, _disk_name = _require_lima_names(row)
    _run_box_commands_checked(
        box, box_scripts.build_cancel_and_restart_commands(instance_name), _VM_START_TIMEOUT_SECONDS
    )
    if row.ssh_port is None or row.container_ssh_port is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} restart-in-place has no recorded ports")

    # Drop the canceled (partial or complete) upload generation and its
    # material -- the running VM immediately diverges from it.
    pending_generation = row.artifact_generation + 1
    storage_module.delete_prefix(
        config, f"{storage_module.workspace_key_prefix(config, row.host_id)}/gen-{pending_generation}/"
    )

    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                # stopped_at is cleared so it again means "this row fully
                # stopped in the current cycle" (the discriminator
                # _fail_start_back_to_stopped relies on).
                "UPDATE pool_hosts SET status = 'leased', stop_requested_at = NULL, stopped_at = NULL, "
                "artifact_manifest = NULL, wrapped_dek = NULL, transition_error = NULL, "
                "transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'starting'",
                (row.host_db_id,),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if updated == 0:
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    logger.info("Workspace %s restarted in place on %s", row.host_db_id, box.public_address)
    return "restarted-in-place"


def _restore_from_artifact(config: StorageConfig, row: WorkspaceRow) -> str:
    """Slow path: reserve a slot on a same-region box, download the artifact, boot it."""
    instance_name, disk_name = _require_lima_names(row)
    manifest = row.artifact_manifest
    if manifest is None or row.wrapped_dek is None:
        raise WorkspaceTransitionError(f"workspace {row.host_db_id} has no artifact to restore")
    for name in _OBJECT_NAMES:
        if name not in manifest.object_by_name:
            raise WorkspaceTransitionError(f"workspace {row.host_db_id} artifact manifest is missing {name}")
    identity = storage_module.unwrap_dek(config, row.wrapped_dek)

    conn = db.get_pool_db_connection()
    try:
        candidates = _list_candidate_boxes(conn, row.region)
    finally:
        conn.close()
    if not candidates:
        raise WorkspaceTransitionError("no capacity available right now, try again later")
    random.shuffle(candidates)

    reserved: tuple[BoxRow, int, int] | None = None
    for box in candidates:
        _write_box_file(box, instance_name, "env", _transfer_env_for(config, manifest, instance_name, identity))
        reserve_script = box_scripts.render_restore_reserve_script(
            instance_name=instance_name,
            disk_name=disk_name,
            slot_count=box.slot_count,
            old_vm_ssh_port=manifest.source_vm_ssh_port,
            old_container_ssh_port=manifest.source_container_ssh_port,
            expected_meta_sha=manifest.object_by_name["META"].sha256,
        )
        _write_box_file(box, instance_name, "reserve.sh", reserve_script)
        transfer_dir = f"$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{instance_name}"
        exit_status, stdout, stderr = _run_box_command(
            box, f'bash "{transfer_dir}/reserve.sh"', timeout_seconds=_VM_START_TIMEOUT_SECONDS
        )
        if exit_status == 0:
            ports = box_scripts.parse_reserved_ports_line(stdout)
            if ports is None:
                # The reserve claimed a slot we cannot use without its ports:
                # roll the claim (and the staged creds) back before raising.
                _cleanup_reserved_restore(box, instance_name, disk_name)
                raise WorkspaceTransitionError(f"reserve on {box.public_address} printed no ports: {stdout[-500:]!r}")
            reserved = (box, ports[0], ports[1])
            break
        if box_scripts.RESTORE_BOX_FULL_MARKER in stderr or box_scripts.RESTORE_NO_PORTS_MARKER in stderr:
            logger.info("Box %s has no capacity for %s; trying another", box.public_address, row.host_db_id)
            # Do not leave the staged env (S3 creds + age identity) on a box
            # that will not host the restore.
            _remove_transfer_dir(box, instance_name)
            continue
        # A hard reserve failure may have materialized partial instance/disk
        # state before its ERR trap fired; drop it (and the staged env) so the
        # box holds nothing for a restore that is not happening here.
        _cleanup_reserved_restore(box, instance_name, disk_name)
        raise WorkspaceTransitionError(
            f"reserve on {box.public_address} failed (exit {exit_status}): {stderr.strip()}"
        )
    if reserved is None:
        raise WorkspaceTransitionError("no capacity available right now, try again later")
    box, vm_ssh_port, container_ssh_port = reserved

    try:
        download_script = box_scripts.render_download_script(
            instance_name=instance_name,
            disk_name=disk_name,
            expected_sha_by_name={name: obj.sha256 for name, obj in manifest.object_by_name.items()},
            vm_ssh_port=vm_ssh_port,
            container_ssh_port=container_ssh_port,
        )
        _write_box_file(box, instance_name, _DOWNLOAD_SCRIPT_FILENAME, download_script)
        launch_status, _lo, launch_err = _run_box_command(
            box, box_scripts.build_launch_detached_command(instance_name, _DOWNLOAD_SCRIPT_FILENAME)
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

    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                # stopped_at is cleared so it again means "this row fully
                # stopped in the current cycle" (the discriminator
                # _fail_start_back_to_stopped relies on).
                "UPDATE pool_hosts SET status = 'leased', vps_address = %s, ssh_port = %s, "
                "container_ssh_port = %s, bare_metal_server_id = %s, stop_requested_at = NULL, "
                "stopped_at = NULL, transition_error = NULL, transition_heartbeat_at = NULL "
                "WHERE id = %s AND status = 'starting'",
                (box.public_address, vm_ssh_port, container_ssh_port, box.server_id, row.host_db_id),
            )
            updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if updated == 0:
        # The row moved on (released or abandoned) after the download booted
        # the VM. As in the mid-download case, nothing else can ever reclaim
        # this box's slot -- the row's box link never pointed at it -- but
        # here the VM is already running, so it needs a real delete, not just
        # a directory rollback.
        _teardown_superseded_restore(box, instance_name, disk_name)
        raise _TransitionSuperseded(_current_status(row.host_db_id) or "gone")
    logger.info(
        "Workspace %s restored on %s (ports vm=%d/container=%d)",
        row.host_db_id,
        box.public_address,
        vm_ssh_port,
        container_ssh_port,
    )
    return "restored"


def _remove_transfer_dir(box: BoxRow, instance_name: str) -> None:
    """Best-effort removal of the instance's transfer dir (creds + status + logs)."""
    transfer_dir = f"$HOME/{box_scripts.TRANSFER_DIR_ROOT}/{instance_name}"
    try:
        exit_status, _stdout, stderr = _run_box_command(box, f'rm -rf "{transfer_dir}"')
    except (paramiko.SSHException, OSError) as exc:
        logger.warning("Could not remove transfer dir for %s on %s", instance_name, box.public_address, exc_info=exc)
        return
    if exit_status != 0:
        logger.warning(
            "Could not remove transfer dir for %s on %s: %s", instance_name, box.public_address, stderr.strip()
        )


def _cleanup_reserved_restore(box: BoxRow, instance_name: str, disk_name: str) -> None:
    """Best-effort rollback of a claimed restore slot after a failed download/boot."""
    try:
        _run_box_commands_checked(
            box,
            box_scripts.build_cleanup_reserved_restore_commands(instance_name, disk_name),
            _SSH_COMMAND_TIMEOUT_SECONDS,
        )
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.warning(
            "Could not roll back reserved restore for %s on %s", instance_name, box.public_address, exc_info=exc
        )


def _teardown_superseded_restore(box: BoxRow, instance_name: str, disk_name: str) -> None:
    """Best-effort teardown of a booted restore VM whose row moved on before the final CAS."""
    try:
        _run_box_commands_checked(
            box, box_scripts.build_finalize_stop_commands(instance_name, disk_name), _VM_STOP_TIMEOUT_SECONDS
        )
    except (WorkspaceTransitionError, paramiko.SSHException, OSError) as exc:
        logger.warning(
            "Could not tear down superseded restore for %s on %s", instance_name, box.public_address, exc_info=exc
        )


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


def find_stale_transitions() -> list[str]:
    """Rows with an in-flight transition (or unfinalized stop) whose supervisor looks dead."""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM pool_hosts WHERE "
                "(status IN ('stopping', 'starting') OR (status = 'stopped' AND bare_metal_server_id IS NOT NULL)) "
                "AND (transition_heartbeat_at IS NULL OR transition_heartbeat_at < NOW() - INTERVAL '%s seconds')"
                % STALE_HEARTBEAT_SECONDS,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def run_transition_watchdog() -> int:
    """Re-spawn a supervisor for every orphaned transition; returns how many were re-driven."""
    if not storage_module.is_storage_configured():
        logger.info("Transition watchdog skipped: workspace storage is not configured for this env")
        return 0
    stale = find_stale_transitions()
    for host_db_id in stale:
        logger.info("Re-driving orphaned transition for %s", host_db_id)
        spawn_supervisor(host_db_id)
    return len(stale)
