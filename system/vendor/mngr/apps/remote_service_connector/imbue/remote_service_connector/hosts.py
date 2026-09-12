"""Pool-host leasing: lease/release/rename/list endpoints and slice cleanup.

A pool host is a "slice": a lima VM on one of our bare-metal boxes. Releasing
it (the inline release path) destroys the VM by SSHing the box and running
limactl. The connector makes no provider-API calls of its own.
"""

import base64
import contextlib
import json
import logging
import os
import re
import shlex
import socket
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from typing import Any
from typing import Final
from uuid import UUID

import paramiko
import psycopg2
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from paramiko.hostkeys import HostKeyEntry
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.auth_proxy as auth_proxy_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.relays as relays_module
import imbue.remote_service_connector.shares as shares_module
import imbue.remote_service_connector.storage as storage_module
import imbue.remote_service_connector.sync as sync_module
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_destroy_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_list_instances_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.entitlements import AccountEntitlements
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.errors import InvalidHostNameError
from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import PoolHostCleanupError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.ssh_certs import ManagementSshCredentials
from imbue.remote_service_connector.ssh_certs import SshCertificateBundleMissingError
from imbue.remote_service_connector.ssh_certs import management_credentials_for_generation

logger = logging.getLogger(__name__)

router = APIRouter()


# Mirror of mngr's SafeName regex (libs/mngr/imbue/mngr/primitives.py:_SAFE_NAME_RE).
# Duplicated here -- not imported -- because ``imbue.mngr`` does not ship into
# the connector container. Keep this in sync if the mngr-side rule
# changes (alphanumeric, dashes/underscores allowed in the middle only).
_HOST_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


def _validate_host_name(value: str) -> str:
    """Field validator: enforce the SafeName regex.

    Rejects empty strings and anything outside the alphanumeric+``-``/``_``
    middle-allowed shape so the connector cannot persist a host_name that
    mngr's ``HostName`` would refuse on the client side.
    """
    stripped = value.strip() if isinstance(value, str) else value
    if not isinstance(stripped, str) or not _HOST_NAME_RE.match(stripped):
        raise InvalidHostNameError(value)
    return stripped


class LeaseHostRequest(BaseModel):
    ssh_public_key: str = Field(description="SSH public key to authorize on the leased host")
    host_name: str = Field(
        description=(
            "User-chosen friendly name for the leased host. Must satisfy mngr's SafeName "
            "regex (alphanumeric, dashes/underscores allowed in the middle). Required."
        )
    )
    attributes: dict[str, Any] = Field(
        description=(
            "Lease-attribute filter. Matches with PostgreSQL '@>' so only fields the request "
            "explicitly sets are constrained; missing fields are unconstrained. Required."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "Hard region requirement (lease-region label, e.g. 'US-EAST-VA'). When set, only "
            "hosts whose region column equals this value are eligible; if none is available the "
            "lease fails. Leave unset to be region-agnostic."
        ),
    )
    # CLEANUP: default this to unrestricted (and drop the gen-1 confinement)
    # once the pre-minds-v0.6 client population is dead -- this outlives phase 6
    # of blueprint/slice-fleet-cutover (it gates on client age, not on gen-1
    # code existing).
    max_box_generation: int = Field(
        default=1,
        ge=1,
        description=(
            "The highest slice-fleet box generation this client can operate; rows on newer boxes "
            "are excluded from the lease. Absent means 1: clients that predate the field cannot "
            "run gen-2 workspaces (their slow-path rebuild would produce an unsandboxed runc "
            "container), so they are confined to gen-1 rows."
        ),
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class LeaseHostResponse(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description="SSH-reachable address of the leased host's bare-metal box (reaches the slice VM)."
    )
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned mngr agent ID")
    host_id: str = Field(description="Host ID in the mngr provider")
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(description="Attributes the row was matched against")
    outer_host_public_key: str = Field(
        description="The VPS/VM-root sshd host public key (port ssh_port), for strict host-key pinning"
    )
    container_host_public_key: str = Field(
        description="The docker container sshd host public key (port container_ssh_port), for strict host-key pinning"
    )
    box_generation: int = Field(
        description="Slice-fleet generation of the leased host's box (selects per-generation client behavior)"
    )
    memory_units: int = Field(description="The machine's current size in units (1 unit = 1GiB guest RAM)")
    disk_gb: int = Field(description="The machine's data-disk size in GB (grow-only)")


class ReleaseHostResponse(BaseModel):
    status: str = Field(
        description="Release status: 'released' on first call, 'already_released' on idempotent retries"
    )


class RenameHostRequest(BaseModel):
    host_name: str = Field(
        description=(
            "New user-chosen friendly name for the leased host. Must satisfy mngr's SafeName "
            "regex (alphanumeric, dashes/underscores allowed in the middle). Required."
        )
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class RenameHostResponse(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the renamed host")
    host_name: str = Field(description="The new user-chosen friendly name")


class LeasedHostInfo(BaseModel):
    host_db_id: UUID = Field(description="Database ID of the leased host")
    vps_address: str = Field(
        description="SSH-reachable address of the leased host's bare-metal box (reaches the slice VM)."
    )
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned mngr agent ID")
    host_id: str = Field(description="Host ID in the mngr provider")
    host_name: str = Field(description="User-chosen friendly name for the leased host")
    attributes: dict[str, Any] = Field(description="Attributes attached to the lease row")
    leased_at: str = Field(description="ISO 8601 timestamp when the host was leased")
    outer_host_public_key: str | None = Field(
        default=None, description="The VPS/VM-root sshd host public key, for strict host-key pinning"
    )
    container_host_public_key: str | None = Field(
        default=None, description="The docker container sshd host public key, for strict host-key pinning"
    )


# The workspace lifecycle statuses a user's row can hold. "Running" rows hold
# (or are about to hold) a bare-metal slot: leased, plus the in-flight stopping
# (VM halted, upload in flight) and starting transitions. "Total" adds stopped
# rows, whose durable VM exists only as encrypted objects in the tier bucket
# (a just-stopped row also keeps its halted local VM -- and thus a slot --
# until the retention window closes, but counts as stopped for quotas; pool
# capacity itself is enforced by real slot occupancy on the boxes). Shared by
# the quota checks and the /account usage display so the two can never drift.
RUNNING_WORKSPACE_STATUSES: Final[tuple[str, ...]] = ("leased", "stopping", "starting")
TOTAL_WORKSPACE_STATUSES: Final[tuple[str, ...]] = RUNNING_WORKSPACE_STATUSES + ("stopped",)


def _count_by_statuses_sql(statuses: tuple[str, ...]) -> str:
    literals = ", ".join(f"'{status}'" for status in statuses)
    return f"SELECT COUNT(*) FROM pool_hosts WHERE leased_to_user = %s AND status IN ({literals})"


COUNT_RUNNING_WORKSPACES_SQL: Final = _count_by_statuses_sql(RUNNING_WORKSPACE_STATUSES)
_COUNT_TOTAL_WORKSPACES_SQL: Final = _count_by_statuses_sql(TOTAL_WORKSPACE_STATUSES)

# Machine-sizing quota sums (specs/slice-fleet). A machine counts at the
# LARGER of its current and pending-target size: the resize request is the
# grant point, and a not-yet-applied downsize must not free capacity its VM
# still physically holds.
_ACTIVE_STATUS_LITERALS: Final = ", ".join(f"'{status}'" for status in RUNNING_WORKSPACE_STATUSES)
_TOTAL_STATUS_LITERALS: Final = ", ".join(f"'{status}'" for status in TOTAL_WORKSPACE_STATUSES)
SUM_ACTIVE_MACHINE_UNITS_SQL: Final = (
    "SELECT COALESCE(SUM(GREATEST(memory_units, COALESCE(target_memory_units, 0))), 0) "
    f"FROM pool_hosts WHERE leased_to_user = %s AND status IN ({_ACTIVE_STATUS_LITERALS})"
)
SUM_TOTAL_MACHINE_DISK_GB_SQL: Final = (
    "SELECT COALESCE(SUM(GREATEST(disk_gb, COALESCE(target_disk_gb, 0))), 0) "
    f"FROM pool_hosts WHERE leased_to_user = %s AND status IN ({_TOTAL_STATUS_LITERALS})"
)


def sum_active_machine_units_with_cursor(cur: Any, user_id_prefix: str) -> int:
    """The units counted against ``max_active_machine_units``, on the caller's open cursor."""
    cur.execute(SUM_ACTIVE_MACHINE_UNITS_SQL, (user_id_prefix,))
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def sum_total_machine_disk_gb_with_cursor(cur: Any, user_id_prefix: str) -> int:
    """The data-disk GB counted against ``max_total_machine_disk_gb``, on the caller's open cursor."""
    cur.execute(SUM_TOTAL_MACHINE_DISK_GB_SQL, (user_id_prefix,))
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def sum_active_machine_units(user_id_prefix: str) -> int:
    """The units counted against ``max_active_machine_units`` (its own pooled connection; see the cursor variant)."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            return sum_active_machine_units_with_cursor(cur, user_id_prefix)


def sum_total_machine_disk_gb(user_id_prefix: str) -> int:
    """The data-disk GB counted against ``max_total_machine_disk_gb`` (its own pooled connection)."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            return sum_total_machine_disk_gb_with_cursor(cur, user_id_prefix)


# Quarantine status for a row whose host could not be reached at lease time
# (SSH key injection failed). Inert everywhere else: every other query filters
# on available/leased/removing, so a quarantined row simply sits out of
# rotation until an operator destroys it (the admin destroy claims this status
# too -- mirrors POOL_HOST_STATUS_UNREACHABLE in mngr_imbue_cloud's
# ``bare_metal_db``, duplicated because that module does not ship into the
# connector container).
_POOL_HOST_STATUS_UNREACHABLE: Final = "unreachable"

# How many candidate rows one lease request tries before giving up. Each
# attempt whose SSH key injection fails quarantines its row and moves on to
# the next-oldest match, so a single dead host can never wedge the pool (the
# 2026-08 production outage: the oldest available rows were on a box with a
# dead disk, and every lease retried the same dead row forever). The cap
# bounds the request's worst-case latency (each injection attempt can take up
# to two 15s SSH timeouts).
_LEASE_MAX_HOST_ATTEMPTS: Final = 3

# The ``host_lease_request`` metric's request-derived tags come from the client
# body, and metric tags must stay low-cardinality (each distinct combination
# is a separate series in OpenObserve). Values outside this conservative shape
# collapse into one "other" bucket instead of minting arbitrary series.
# Matched with fullmatch: a $-anchored match() would still accept a trailing
# newline.
_LEASE_METRIC_TAG_RE: Final = re.compile(r"[A-Za-z0-9._/-]{1,64}")

_LEASE_METRIC_TAG_OTHER: Final = "other"


def _lease_metric_tag_value(value: object) -> str:
    """Clamp one client-supplied tag value: '' when unset, 'other' when unsafe."""
    if value is None or value == "":
        return ""
    if isinstance(value, str) and _LEASE_METRIC_TAG_RE.fullmatch(value):
        return value
    return _LEASE_METRIC_TAG_OTHER


def build_lease_request_metric_tags(
    is_leased: bool,
    is_quota_refused: bool,
    is_pool_exhausted: bool,
    is_update_required: bool,
    is_missing_host_keys: bool,
    requested_region: str | None,
    requested_branch: object,
) -> dict[str, str]:
    """The ``host_lease_request`` metric's tags for one lease attempt (pure).

    The outcome precedence mirrors the response precedence in
    ``_lease_pool_host``: a lease beats every failure, a machine-sizing quota
    refusal beats missing host keys, missing host keys beat pool exhaustion
    (with the update-required sub-case of exhaustion its own outcome -- it
    means old clients are still trying, not that capacity ran out), and the
    remaining case is an injection failure (every candidate row was
    quarantined).
    """
    if is_leased:
        outcome = "leased"
    elif is_quota_refused:
        outcome = "quota_refused"
    elif is_missing_host_keys:
        outcome = "no_host_keys"
    elif is_update_required:
        outcome = "update_required"
    elif is_pool_exhausted:
        outcome = "pool_exhausted"
    else:
        outcome = "injection_failed"
    return {
        "outcome": outcome,
        "region": _lease_metric_tag_value(requested_region),
        "branch": _lease_metric_tag_value(requested_branch),
    }


def _parse_pinned_host_key(host: str, port: int, expected_host_public_key: str) -> tuple[str, paramiko.PKey]:
    """Return the known-hosts name for ``host:port`` and the row's pinned key parsed as a paramiko key.

    paramiko keys non-default ports under the ``[host]:port`` known-hosts name, so
    a container/forwarded port must be pinned under that bracketed name to match
    what ``connect`` looks up.
    """
    known_hosts_name = host if port == 22 else f"[{host}]:{port}"
    entry = HostKeyEntry.from_line(f"{known_hosts_name} {expected_host_public_key.strip()}")
    if entry is None or entry.key is None:
        # An SSHException (not PoolHostCleanupError) so this is handled uniformly by
        # every caller: the teardown sweep and reconcile already treat it as an SSH
        # failure, and the lease path's `except (paramiko.SSHException, OSError)`
        # maps it to a 502 (SSH key injection failed) rather than a misleading 500.
        raise paramiko.SSHException(
            f"could not parse expected host key for {known_hosts_name}: {expected_host_public_key!r}"
        )
    return known_hosts_name, entry.key


def _pin_expected_host_key(client: paramiko.SSHClient, host: str, port: int, expected_host_public_key: str) -> None:
    """Pin ``expected_host_public_key`` for ``host:port`` and reject any other host key.

    Replaces trust-on-first-use: a mismatched or unknown host key is rejected.
    """
    known_hosts_name, pinned_key = _parse_pinned_host_key(host, port, expected_host_public_key)
    client.get_host_keys().add(known_hosts_name, pinned_key.get_name(), pinned_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


# Unauthenticated handshake budget for the pre-share host-key probe.
_HOST_KEY_PROBE_TIMEOUT_SECONDS: Final[float] = 15.0


def _assert_container_serves_pinned_host_key(host: str, port: int, expected_host_public_key: str) -> None:
    """Handshake with the container sshd and raise ``paramiko.BadHostKeyException`` unless it serves the pinned key.

    Runs before a share is activated and its relay token rotated: a workspace
    the connector cannot reach (the minds desktop app adopted it and rotated the
    key) must keep the share its desktop established, not be left holding a
    token the rotation just invalidated.
    """
    known_hosts_name, pinned_key = _parse_pinned_host_key(host, port, expected_host_public_key)
    with socket.create_connection((host, port), timeout=_HOST_KEY_PROBE_TIMEOUT_SECONDS) as sock:
        transport = paramiko.Transport(sock)
        # Ask for the pinned key's type first (as SSHClient.connect does for a
        # known host), so an sshd serving several host keys presents the one
        # the row recorded rather than paramiko's default preference.
        security_options = transport.get_security_options()
        security_options.key_types = [pinned_key.get_name()] + [
            key_type for key_type in security_options.key_types if key_type != pinned_key.get_name()
        ]
        try:
            transport.start_client(timeout=_HOST_KEY_PROBE_TIMEOUT_SECONDS)
            served_key = transport.get_remote_server_key()
        finally:
            transport.close()
    if served_key.get_name() != pinned_key.get_name() or served_key.asbytes() != pinned_key.asbytes():
        raise paramiko.BadHostKeyException(known_hosts_name, served_key, pinned_key)


def _desktop_managed_workspace_conflict(host_db_id: UUID) -> HTTPException:
    """The 409 for a workspace whose container serves a host key the row never learned.

    The minds desktop app adopted it and rotated its host keys client-side (the
    connector never learns them by design), so a server-side share bring-up
    cannot reach the container; the desktop app enables sharing over the user's
    own SSH instead.
    """
    logger.info(
        "Host %s serves a rotated container host key; sharing must be enabled from the desktop app", host_db_id
    )
    return HTTPException(
        status_code=409,
        detail={
            "code": "workspace_managed_by_desktop",
            "message": (
                "This workspace is managed by the minds desktop app, which rotated its SSH host key; "
                "enable sharing from the desktop app."
            ),
        },
    )


@contextlib.contextmanager
def management_ssh_client(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    timeout_seconds: float,
    expected_host_public_key: str,
) -> Iterator[paramiko.SSHClient]:
    """Yield an SSHClient connected to ``host`` with the management credentials, closed on exit.

    Gen-2 credentials are a short-lived certificate (presented in place of the raw
    key); gen-1 ones the static pool key. The host is authenticated against
    ``expected_host_public_key`` (strict pinning, no trust-on-first-use); callers
    fail closed when no pinned key is available.
    """
    private_key = credentials.to_paramiko_key()
    client = paramiko.SSHClient()
    _pin_expected_host_key(client, host, port, expected_host_public_key)
    try:
        client.connect(hostname=host, port=port, username=user, pkey=private_key, timeout=timeout_seconds)
        yield client
    finally:
        client.close()


def _append_authorized_key(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    public_key_to_add: str,
    expected_host_public_key: str,
) -> None:
    """SSH into a host with the management credentials and append a public key to authorized_keys."""
    with management_ssh_client(
        host, port, user, credentials, timeout_seconds=15, expected_host_public_key=expected_host_public_key
    ) as client:
        key_line = public_key_to_add.strip()
        commands = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo {} >> ~/.ssh/authorized_keys && ".format(
                shlex.quote(key_line)
            )
            + "chmod 600 ~/.ssh/authorized_keys"
        )
        _stdin, _stdout, stderr = client.exec_command(commands)
        exit_status = _stdout.channel.recv_exit_status()
        if exit_status != 0:
            stderr_text = stderr.read().decode()
            raise paramiko.SSHException(f"SSH command failed (exit {exit_status}): {stderr_text}")


def _delete_pool_host_row(host_db_id: Any) -> None:
    """Delete a single pool_hosts row by id, on a fresh pooled connection.

    Deliberately not the connection the caller read the row on: the delete
    lands after the SSH teardown, and a connection held idle across that is
    the one most likely to have been dropped in the meantime.
    """
    with db.pooled_db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(host_db_id),))


def build_slice_teardown_commands(slice_instance_name: str, slice_disk_name: str | None) -> tuple[str, ...]:
    """Commands to run on the bare-metal box to destroy a gen-1 slice's lima VM + data disk."""
    commands = [f"limactl delete --force {shlex.quote(slice_instance_name)}"]
    if slice_disk_name:
        commands.append(f"limactl disk delete --force {shlex.quote(slice_disk_name)}")
    return tuple(commands)


def build_slice_teardown_commands_for_generation(
    box_generation: int, slice_instance_name: str, slice_disk_name: str | None
) -> tuple[str, ...]:
    """The box-side teardown command set for a slice, selected by the row's stamped generation."""
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return (build_qemu_destroy_script(slice_instance_name),)
    return build_slice_teardown_commands(slice_instance_name, slice_disk_name)


# What ``limactl delete`` / ``limactl disk delete`` print when the target is
# already gone. A teardown that reaches a box whose VM was already destroyed
# (a release interrupted between the teardown and the row delete) must count
# as done, not fail forever; mirrors ``mngr_imbue_cloud.slices.lima_slice_client``
# (duplicated, not imported). The shell's own "command not found" (no limactl
# on the box's PATH) also contains "not found" but means the VM was never
# touched, so it is excluded.
_LIMA_TARGET_ABSENT_MARKERS: Final[tuple[str, ...]] = ("not found", "does not exist")
_SHELL_COMMAND_NOT_FOUND_MARKER: Final[str] = "command not found"


def _is_lima_target_absent_error(stderr_text: str) -> bool:
    """Whether a failed limactl delete reports that its target was already absent."""
    lowered = stderr_text.lower()
    if _SHELL_COMMAND_NOT_FOUND_MARKER in lowered:
        return False
    return any(marker in lowered for marker in _LIMA_TARGET_ABSENT_MARKERS)


def run_ssh_commands_on_box(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    commands: tuple[str, ...],
    box_host_public_key: str,
    is_absent_target_tolerated: bool,
) -> None:
    """SSH into the box with the management credentials and run each command, raising on failure."""
    with management_ssh_client(
        host, port, user, credentials, timeout_seconds=30, expected_host_public_key=box_host_public_key
    ) as client:
        for command in commands:
            _stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status == 0:
                continue
            stderr_text = stderr.read().decode()
            if is_absent_target_tolerated and _is_lima_target_absent_error(stderr_text):
                logger.info("Slice teardown command %r found its target already absent on %s", command, host)
                continue
            raise PoolHostCleanupError(
                f"slice teardown command {command!r} failed (exit {exit_status}): {stderr_text}"
            )


def clean_up_slice_on_box(
    host_db_id: Any,
    bare_metal_server_id: Any,
    slice_instance_name: str | None,
    slice_disk_name: str | None,
    box_generation: int,
) -> None:
    """Destroy a slice's VM (and data disk) on its owning bare-metal box.

    Looks up the box's address + slice service user from ``bare_metal_servers``
    (on a short pooled checkout -- no connection is held across the SSH work),
    then SSHes in with the pool management key and runs the teardown for the
    row's stamped generation (limactl for gen-1, the systemd unit + instance-dir
    removal for gen-2). An instance or disk that is already absent counts as
    torn down. Raises ``PoolHostCleanupError`` if the slice's bookkeeping is
    incomplete or the box can't be reached, so the row stays ``removing`` and a
    retry (the client's, or the lease-record sweep's) finishes the job -- the
    slot is only freed once the VM is really gone.
    """
    if not (bare_metal_server_id and slice_instance_name):
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id} is missing bare_metal_server_id or slice_instance_name; cannot tear down its VM"
        )
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                # CLEANUP: drop the COALESCE fallbacks to the legacy lima_* columns
                # (here and in the other bare_metal_servers / pool_hosts reads of this
                # module) once every tier's pool DB has applied migration 041 and no
                # pre-rename checkout writes them anymore.
                "SELECT public_address, COALESCE(slice_service_user, lima_service_user), box_host_public_key "
                "FROM bare_metal_servers WHERE id = %s",
                (str(bare_metal_server_id),),
            )
            server_row = cur.fetchone()
    if server_row is None or not server_row[0]:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} is missing or has no public_address"
        )
    box_address, slice_service_user, box_host_public_key = server_row[0], server_row[1] or "root", server_row[2]
    # Fail closed: without the box's pinned host key we cannot reach it without
    # trust-on-first-use. The row stays ``removing`` and the sweep retries once
    # the one-time keyscan backfill has populated the column.
    if not box_host_public_key:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} has no box_host_public_key "
            "(run the one-time `minds-admin pool backfill-host-keys`)"
        )
    commands = build_slice_teardown_commands_for_generation(box_generation, slice_instance_name, slice_disk_name)
    try:
        credentials = management_credentials_for_generation(box_generation)
    except SshCertificateBundleMissingError as exc:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: no management SSH credentials available for box {box_address}: {exc}"
        ) from exc
    run_ssh_commands_on_box(
        box_address,
        22,
        slice_service_user,
        credentials,
        commands,
        box_host_public_key,
        is_absent_target_tolerated=True,
    )


# Slice lima resources are named ``mngr-slice-<env>-<host-hex>`` (the data disk
# adds a ``-data`` suffix). The host hex is a hyphen-free uuid -- truncated to 16
# chars on current slices so long (CI) env names fit limactl's name budget, the
# full 32 on slices baked before the truncation -- so the env stamp is everything
# between the prefix and the trailing ``-<host-hex>``. Longest-first so a legacy
# env that happens to end in ``-<16 hex>`` still parses as the 32-hex shape.
# Mirrors ``mngr_imbue_cloud.slices.bare_metal`` (the connector has no dependency
# on it).
_SLICE_INSTANCE_PREFIX = "mngr-slice-"
_SLICE_DISK_SUFFIX = "-data"
_STAMPED_SLICE_CORE_RES = (
    re.compile(r"^(?P<env>.+)-(?P<host>[0-9a-f]{32})$"),
    re.compile(r"^(?P<env>.+)-(?P<host>[0-9a-f]{16})$"),
)
# Non-login SSH may not source the service user's profile, so set PATH explicitly
# (limactl is extracted to /usr/local/bin by box prep).
_BOX_LIMACTL_PATH_PREFIX = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH"


def slice_name_env_owner(name: str) -> str | None:
    """The env a slice instance/disk name is stamped for, or None if legacy/foreign/not-a-slice."""
    if not name.startswith(_SLICE_INSTANCE_PREFIX):
        return None
    core = name[len(_SLICE_INSTANCE_PREFIX) :]
    if core.endswith(_SLICE_DISK_SUFFIX):
        core = core[: -len(_SLICE_DISK_SUFFIX)]
    for pattern in _STAMPED_SLICE_CORE_RES:
        match = pattern.match(core)
        if match is not None:
            return match.group("env")
    return None


def _list_box_gen2_instance_names(client: paramiko.SSHClient, host: str) -> set[str]:
    """Return the name of every gen-2 slice instance dir on the box.

    Runs over the caller's already-connected management SSH ``client`` (``host``
    is for error messages only). Raises ``PoolHostCleanupError`` on a non-zero
    exit so the caller treats the box as not reconciled rather than mistaking
    an SSH failure for "no VMs".
    """
    command = build_qemu_list_instances_command()
    _stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode()
    if exit_status != 0:
        raise PoolHostCleanupError(
            f"gen-2 slice listing on {host} failed (exit {exit_status}): {stderr.read().decode()}"
        )
    return {line.strip() for line in output.splitlines() if line.strip()}


def _list_box_lima_names(client: paramiko.SSHClient, host: str, json_subcommand: str) -> set[str]:
    """Return the ``name`` of every lima instance or disk on the box (per ``json_subcommand``).

    Runs over the caller's already-connected management SSH ``client`` (``host`` is
    for error messages only). ``json_subcommand`` is ``list --json`` (instances) or
    ``disk list --json`` (disks); both emit one JSON object per line. Raises
    ``PoolHostCleanupError`` on a non-zero exit so the caller treats the box as not
    reconciled rather than mistaking an SSH failure for "no VMs".
    """
    names: set[str] = set()
    command = f"{_BOX_LIMACTL_PATH_PREFIX} limactl {json_subcommand}"
    _stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode()
    if exit_status != 0:
        raise PoolHostCleanupError(
            f"`limactl {json_subcommand}` on {host} failed (exit {exit_status}): {stderr.read().decode()}"
        )
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable lima JSON line on %s: %r", host, stripped)
            continue
        name = parsed.get("name")
        if name:
            names.add(name)
    return names


# Box-health probe run over the same management SSH connection as the lima
# listing (the reconcile opens one connection per box and runs both on it):
# /proc/mdstat (degraded RAID arrays) and /proc/swaps (unmirrored raw-partition
# swap). Both files are world-readable, and one exec keeps the probe a single
# round-trip.
_BOX_HEALTH_SPLIT_MARKER: Final = "MNGR_BOX_HEALTH_SPLIT"
# Binaries the workspace stop/start transfer scripts need on every box (the
# prep script installs the pinned age + s5cmd releases; zstd comes from apt).
# A box missing one silently fails every upload/restore that lands on it, so
# the sweep surfaces the drift -- the 15.204.140.221 box shipped exactly this
# way (prepped before the transfer tooling existed, never re-prepped).
_BOX_TRANSFER_BINARIES: Final = ("s5cmd", "age", "zstd")
_BOX_HEALTH_COMMAND: Final = (
    f"cat /proc/mdstat && echo {_BOX_HEALTH_SPLIT_MARKER} && cat /proc/swaps"
    f" && echo {_BOX_HEALTH_SPLIT_MARKER} && export PATH=/usr/local/bin:$HOME/.local/bin:$PATH"
    + "".join(f" && (command -v {binary} >/dev/null || echo {binary})" for binary in _BOX_TRANSFER_BINARIES)
)

# /proc/mdstat structure: an array header line (``md3 : active raid1 ...``)
# followed by a status line whose ``[expected/active]`` bracket reports member
# counts. Mirrors ``mngr_imbue_cloud.slices.bare_metal`` (duplicated, not
# imported -- that module does not ship into the connector container).
_MD_ARRAY_HEADER_RE = re.compile(r"^(md\d+)\s*:")
_MD_MEMBER_COUNTS_RE = re.compile(r"\[(\d+)/(\d+)\]")


def _parse_degraded_md_arrays(mdstat_text: str) -> list[str]:
    """The md arrays in a ``/proc/mdstat`` dump running with a failed member."""
    degraded: list[str] = []
    current_array: str | None = None
    for line in mdstat_text.splitlines():
        header_match = _MD_ARRAY_HEADER_RE.match(line)
        if header_match:
            current_array = header_match.group(1)
            continue
        counts_match = _MD_MEMBER_COUNTS_RE.search(line)
        if counts_match and current_array is not None:
            expected_members, active_members = int(counts_match.group(1)), int(counts_match.group(2))
            if active_members < expected_members:
                degraded.append(current_array)
            current_array = None
    return degraded


def _parse_raw_swap_devices(proc_swaps_text: str) -> list[str]:
    """The swap devices in a ``/proc/swaps`` dump that are raw (non-md) partitions.

    Unmirrored swap is what turned one dead disk into a slow box-wide SIGBUS
    massacre in the 2026-08-07 production incident; the box prep now retires
    these, so any partition entry means the box needs a prep re-run. Swap on an
    md device is itself mirrored, so ``/dev/md*`` entries are not flagged.
    """
    raw_devices: list[str] = []
    # The first line is the fixed "Filename Type Size ..." header.
    for line in proc_swaps_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "partition" and not fields[0].startswith("/dev/md"):
            raw_devices.append(fields[0])
    return raw_devices


def _read_box_health_texts(client: paramiko.SSHClient, host: str) -> tuple[str, str, list[str]]:
    """Return (/proc/mdstat, /proc/swaps, missing transfer binaries) in one exec round-trip.

    Runs over the caller's already-connected management SSH ``client`` (``host`` is
    for error messages only). Raises ``PoolHostCleanupError`` on a non-zero exit so
    the caller treats an unreadable box as a failed reconcile rather than a healthy
    one.
    """
    _stdin, stdout, stderr = client.exec_command(_BOX_HEALTH_COMMAND)
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode()
    if exit_status != 0:
        raise PoolHostCleanupError(f"box health probe on {host} failed (exit {exit_status}): {stderr.read().decode()}")
    return _split_box_health_output(output)


def _split_box_health_output(output: str) -> tuple[str, str, list[str]]:
    mdstat_text, _, remainder = output.partition(f"{_BOX_HEALTH_SPLIT_MARKER}\n")
    proc_swaps_text, _, missing_binaries_text = remainder.partition(f"{_BOX_HEALTH_SPLIT_MARKER}\n")
    missing_binaries = [line.strip() for line in missing_binaries_text.splitlines() if line.strip()]
    return mdstat_text, proc_swaps_text, missing_binaries


def reconcile_slice_boxes(conn: Any, env_name: str) -> int:
    """Audit each box's lima slices against the DB, scoped to ``env_name``'s stamped slices.

    Returns the number of divergences found (and logged). Alert-only by design: for
    every bare-metal box it logs, at error level,

    * a slice stamped for ``env_name`` present on the box with no pool_hosts row,
    * a pool_hosts row whose VM is absent from the box, and
    * a box hardware-health problem: a degraded md RAID array, or swap on an
      unmirrored raw partition (both from the 2026-08-07 nvme incident, which ran
      undetected for six days).

    It deliberately does NOT auto-delete. A row-less stamped slice is most often a
    bake mid-flight (the carve creates the instance ~10-30 min before it inserts the
    row), and this cron runs on a fixed hourly schedule independent of bakes -- so
    auto-reaping here would race a live bake and destroy its slice. Actual reaping is
    left to the bake-time reaper (which runs in the bake's own ``finally``, where the
    in-flight set is known). If a box cannot be inspected (the health probe or the
    lima listing fails), this raises: a box we could not inspect was NOT reconciled,
    and that failure must surface rather than be mistaken for a clean audit. Other
    envs' slices and legacy un-stamped slices are never inspected, so this is safe
    on a shared box.
    """
    if not env_name:
        logger.info("Slice reconcile skipped: connector has no MINDS_ENV_NAME to scope to")
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, public_address, COALESCE(slice_service_user, lima_service_user), box_host_public_key, "
            "box_generation FROM bare_metal_servers"
        )
        servers = cur.fetchall()
    if not servers:
        return 0
    divergence_count = 0
    for server_id, public_address, slice_service_user, box_host_public_key, raw_box_generation in servers:
        if not public_address:
            continue
        # Fail closed on a box with no pinned host key: skipping it would look like
        # a clean audit, so surface it loudly instead. Cleared once the one-time
        # keyscan backfill populates the column.
        if not box_host_public_key:
            logger.error(
                "Slice reconcile skipped box %s: no box_host_public_key (run the one-time host-key backfill)",
                public_address,
            )
            divergence_count += 1
            continue
        user = slice_service_user or "root"
        # One management SSH connection per box serves both reads. Box hardware
        # health is read and logged first, before the lima listing is attempted: a
        # degraded RAID array or unmirrored raw-partition swap is exactly the
        # precursor state of the 2026-08-07 nvme incident, and nothing else on the
        # box surfaces it -- if a box's degraded hardware also breaks the lima
        # listing, the health diagnostics must already be logged rather than lost
        # to the exception. If either read fails we did NOT reconcile this box; let
        # the failure propagate rather than silently skipping (which would look
        # like a clean audit and could mask vanished/leaked VMs).
        with management_ssh_client(
            public_address,
            22,
            user,
            management_credentials_for_generation(int(raw_box_generation or 1)),
            timeout_seconds=30,
            expected_host_public_key=box_host_public_key,
        ) as box_client:
            mdstat_text, proc_swaps_text, missing_binaries = _read_box_health_texts(box_client, public_address)
            for degraded_array in _parse_degraded_md_arrays(mdstat_text):
                divergence_count += 1
                logger.error(
                    "Box health on %s: md array %s is degraded (a RAID member has failed); "
                    "the box needs a disk replacement",
                    public_address,
                    degraded_array,
                )
            for raw_swap_device in _parse_raw_swap_devices(proc_swaps_text):
                divergence_count += 1
                logger.error(
                    "Box health on %s: swap device %s is an unmirrored raw partition "
                    "(a disk death loses its pages and SIGBUS-kills processes); re-run box prep to retire it",
                    public_address,
                    raw_swap_device,
                )
            if missing_binaries:
                divergence_count += 1
                logger.error(
                    "Box health on %s: workspace transfer tooling missing (%s) -- every stop upload and "
                    "restore that lands on this box fails; re-run box prep to install it",
                    public_address,
                    ", ".join(missing_binaries),
                )
            if int(raw_box_generation or 1) >= FIRST_QEMU_BOX_GENERATION:
                box_instances = _list_box_gen2_instance_names(box_client, public_address)
            else:
                box_instances = _list_box_lima_names(box_client, public_address, "list --json")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(slice_instance_name, lima_instance_name) FROM pool_hosts "
                "WHERE bare_metal_server_id = %s",
                (str(server_id),),
            )
            tracked_instances = {row[0] for row in cur.fetchall() if row[0]}

        # This env's stamped slices on the box with no DB row (often a bake mid-flight).
        untracked = {
            name for name in box_instances if slice_name_env_owner(name) == env_name and name not in tracked_instances
        }
        for instance_name in sorted(untracked):
            divergence_count += 1
            logger.error(
                "Slice reconcile divergence on %s: stamped slice %s has no pool_hosts row "
                "(in-flight bake, or an orphan for the bake-time reaper)",
                public_address,
                instance_name,
            )
        # A DB row whose VM is gone is the other divergence direction.
        for missing_instance in sorted(tracked_instances - box_instances):
            divergence_count += 1
            logger.error(
                "Slice reconcile divergence on %s: pool_hosts row for %s has no VM on the box (needs manual rebake/cleanup)",
                public_address,
                missing_instance,
            )
    return divergence_count


@router.post("/hosts/lease")
def lease_host(request: Request, body: LeaseHostRequest) -> dict[str, object]:
    """Lease an available host from the pool, injecting the caller's SSH public key.

    Enforces the account's remote-workspace quota strictly: a per-user
    advisory lock (held for the lease transaction) serializes concurrent
    leases so two simultaneous requests cannot both squeeze past the count
    check. Stopped workspaces still hold their lease (and their slice), so
    they count against the quota too.

    Requires a verified email (spam/abuse mitigation): an unverified account
    gets the structured ``email_not_verified`` 403, and the refusal itself
    sends the verification email (under the server-side cooldown).
    """
    with handle_endpoint_errors():
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        auth_proxy_module.require_verified_email_for_remote_workspace(user, full_user_id)
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        return _lease_pool_host(
            user, full_user_id, entitlements, body, record_display_name=body.host_name
        ).model_dump()


def _lease_pool_host(
    user: UserAuth,
    full_user_id: str,
    entitlements: AccountEntitlements,
    body: LeaseHostRequest,
    # The display name the workspace's record stub starts with (the desktop
    # overwrites it with the form's name on its first push).
    record_display_name: str,
) -> LeaseHostResponse:
    """The lease flow shared by ``/hosts/lease`` and ``/hosts/claim``.

    Quota check (under the per-user advisory lock), then up to
    ``_LEASE_MAX_HOST_ATTEMPTS`` rounds of: row selection with
    ``FOR UPDATE SKIP LOCKED``, SSH key injection with strict host-key
    pinning, and the status flip to ``leased`` together with the workspace's
    metadata-only record stub (same transaction, so a lease without a record
    never exists). A row whose injection fails is quarantined (status
    ``unreachable``) and the next-oldest match is tried, so a dead host
    removes itself from rotation instead of wedging every lease. Raises
    ``HTTPException`` on every failure path.
    """
    # Rows this request quarantined; committed even when the lease ultimately
    # fails (the whole attempt loop exits the transaction normally), so a dead
    # host stays out of rotation for the caller's retry.
    quarantined_host_db_ids: list[Any] = []
    leased: LeaseHostResponse | None = None
    is_pool_exhausted = False
    is_update_required = False
    no_host_keys_detail: str | None = None
    # A machine-sizing quota refusal found mid-loop: (entitlement, limit,
    # current, noun). Raised only after the transaction commits so any
    # quarantines this request already flipped are never rolled back.
    machine_quota_refusal: tuple[str, int, int, str] | None = None
    # A gen-2 row selected while no management certificate is stored; likewise
    # carried out of the loop and raised (as the 503) only after the commit.
    missing_certificate_error: SshCertificateBundleMissingError | None = None
    with db.pooled_db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                # Serialize this user's leases for the duration of the
                # transaction, then enforce the workspace quota. The
                # advisory lock releases automatically at commit/rollback.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user.user_id_prefix,))
                cur.execute(COUNT_RUNNING_WORKSPACES_SQL, (user.user_id_prefix,))
                count_row = cur.fetchone()
                leased_count = int(count_row[0]) if count_row is not None else 0
                if leased_count >= entitlements.max_remote_workspaces:
                    raise_quota_exceeded(
                        "max_remote_workspaces",
                        entitlements.max_remote_workspaces,
                        leased_count,
                        "remote workspaces",
                    )
                # A new lease adds to the running AND total counts, so both
                # caps gate it (stopped workspaces consume only the total).
                cur.execute(_COUNT_TOTAL_WORKSPACES_SQL, (user.user_id_prefix,))
                total_row = cur.fetchone()
                total_count = int(total_row[0]) if total_row is not None else 0
                if total_count >= entitlements.max_total_workspaces:
                    raise_quota_exceeded(
                        "max_total_workspaces",
                        entitlements.max_total_workspaces,
                        total_count,
                        "total workspaces (running + stopped)",
                    )
                # Build the lease selection dynamically. The hard ``region``
                # requirement adds an equality filter; unset leaves the lease
                # unconstrained on it. The selection stays a single round-trip
                # (the fast path must not pay an extra query).
                where_clauses = ["status = 'available'", "attributes @> %s::jsonb"]
                query_params: list[object] = [json.dumps(body.attributes)]
                if body.region is not None:
                    where_clauses.append("region = %s")
                    query_params.append(body.region)
                where_clauses.append("box_generation <= %s")
                query_params.append(body.max_box_generation)
                order_by = "created_at ASC"
                lease_select_sql = (
                    "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, attributes, "
                    "outer_host_public_key, container_host_public_key, box_generation, memory_units, disk_gb "
                    "FROM pool_hosts "
                    f"WHERE {' AND '.join(where_clauses)} "
                    f"ORDER BY {order_by} LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                for _attempt in range(_LEASE_MAX_HOST_ATTEMPTS):
                    # An in-transaction quarantine below flips its row away from
                    # 'available', so re-running the same SELECT never returns a
                    # row this request already failed on.
                    cur.execute(lease_select_sql, tuple(query_params))
                    row = cur.fetchone()
                    if row is None:
                        is_pool_exhausted = True
                        # An exhaustion under a generation cap is permanent when
                        # the tier holds NO row at or below the client's
                        # generation (any status) while newer-generation rows
                        # exist: the fleet has moved past this client, and the
                        # remedy is an app update, not a retry. An entirely
                        # empty pool stays the ordinary no-capacity refusal.
                        if body.max_box_generation < FIRST_QEMU_BOX_GENERATION:
                            cur.execute(
                                "SELECT EXISTS(SELECT 1 FROM pool_hosts WHERE box_generation <= %s), "
                                "EXISTS(SELECT 1 FROM pool_hosts WHERE box_generation > %s)",
                                (body.max_box_generation, body.max_box_generation),
                            )
                            probe = cur.fetchone()
                            is_update_required = probe is not None and not probe[0] and bool(probe[1])
                        break
                    (
                        host_db_id,
                        vps_address,
                        ssh_port,
                        ssh_user,
                        container_ssh_port,
                        agent_id,
                        host_id,
                        attributes,
                        outer_host_public_key,
                        container_host_public_key,
                        lease_box_generation,
                        lease_memory_units,
                        lease_disk_gb,
                    ) = row

                    # Machine-sizing quotas (specs/slice-fleet), checked against
                    # the SELECTED row's actual size (a dev bake can carve
                    # odd-sized machines). Break rather than raise so the
                    # already-committed-on-exit quarantines survive; the 403 is
                    # raised after the commit.
                    row_units = int(lease_memory_units)
                    row_disk_gb = int(lease_disk_gb)
                    active_units = sum_active_machine_units_with_cursor(cur, user.user_id_prefix)
                    if active_units + row_units > entitlements.max_active_machine_units:
                        machine_quota_refusal = (
                            "max_active_machine_units",
                            entitlements.max_active_machine_units,
                            active_units,
                            "active machine units",
                        )
                        break
                    total_disk_gb = sum_total_machine_disk_gb_with_cursor(cur, user.user_id_prefix)
                    if total_disk_gb + row_disk_gb > entitlements.max_total_machine_disk_gb:
                        machine_quota_refusal = (
                            "max_total_machine_disk_gb",
                            entitlements.max_total_machine_disk_gb,
                            total_disk_gb,
                            "machine disk GB",
                        )
                        break

                    # Fail closed: a row without both pinned host keys cannot be
                    # leased without trust-on-first-use. This only happens for rows
                    # baked before the host-key columns existed; the one-time
                    # keyscan backfill populates them. Surface it as no-capacity so
                    # the caller (and the fast/slow path retry) treats it like an
                    # unavailable host rather than a hard error. Break rather than
                    # raise: an exception here would roll back the transaction and
                    # un-quarantine any dead rows this request already flipped
                    # (re-wedging the pool on them); the 503 is raised after the
                    # commit instead. Not skippable either -- the row stays
                    # 'available', so re-selecting would return it again.
                    if not outer_host_public_key or not container_host_public_key:
                        no_host_keys_detail = (
                            f"Pool host {host_db_id} has no pinned SSH host keys yet; "
                            "run the one-time `minds-admin pool backfill-host-keys`."
                        )
                        break

                    # The row is not at fault when the connector holds no
                    # certificate for its generation, so it stays available;
                    # break so the quarantines above outlive the 503.
                    try:
                        management_credentials = management_credentials_for_generation(int(lease_box_generation or 1))
                    except SshCertificateBundleMissingError as exc:
                        missing_certificate_error = exc
                        break

                    # Inject the user's SSH public key on VPS and container, pinning
                    # each sshd's recorded host key (strict, no trust-on-first-use).
                    try:
                        _append_authorized_key(
                            vps_address,
                            ssh_port,
                            ssh_user,
                            management_credentials,
                            body.ssh_public_key,
                            outer_host_public_key,
                        )
                        _append_authorized_key(
                            vps_address,
                            container_ssh_port,
                            ssh_user,
                            management_credentials,
                            body.ssh_public_key,
                            container_host_public_key,
                        )
                    except (paramiko.SSHException, OSError) as exc:
                        logger.error(
                            "Quarantining pool host %s (%s ports %s/%s): SSH key injection failed: %s",
                            host_db_id,
                            vps_address,
                            ssh_port,
                            container_ssh_port,
                            exc,
                        )
                        cur.execute(
                            f"UPDATE pool_hosts SET status = '{_POOL_HOST_STATUS_UNREACHABLE}' WHERE id = %s",
                            (host_db_id,),
                        )
                        quarantined_host_db_ids.append(host_db_id)
                        continue

                    # ``host_name`` is mutable per-lease: it gets overwritten with the
                    # user-supplied name each time the pool row is leased (and could
                    # later be patched by a rename endpoint).
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'leased', leased_to_user = %s, "
                        "leased_at = NOW(), host_name = %s WHERE id = %s",
                        (user.user_id_prefix, body.host_name, host_db_id),
                    )
                    sync_module.insert_lease_record_stub(
                        cur,
                        user_id=full_user_id,
                        host_id=host_id,
                        agent_id=agent_id,
                        display_name=record_display_name,
                        provider_kind=sync_module.lease_record_provider_kind(user.email),
                    )
                    leased = LeaseHostResponse(
                        host_db_id=host_db_id,
                        vps_address=vps_address,
                        ssh_port=ssh_port,
                        ssh_user=ssh_user,
                        container_ssh_port=container_ssh_port,
                        agent_id=agent_id,
                        host_id=host_id,
                        host_name=body.host_name,
                        attributes=attributes if isinstance(attributes, dict) else {},
                        outer_host_public_key=outer_host_public_key,
                        container_host_public_key=container_host_public_key,
                        box_generation=int(lease_box_generation or 1),
                        memory_units=row_units,
                        disk_gb=row_disk_gb,
                    )
                    break
    # One metric record per attempt that reached host selection: create demand
    # (and its failures) charted per requested region/branch. Workspace-count
    # quota and auth refusals raise before this point and stay visible via the
    # access log's status codes instead; the machine-sizing refusal is carried
    # out of the loop, so it is recorded here.
    emit_metric(
        "host_lease_request",
        1,
        build_lease_request_metric_tags(
            is_leased=leased is not None,
            is_quota_refused=machine_quota_refusal is not None,
            is_pool_exhausted=is_pool_exhausted,
            is_update_required=is_update_required,
            is_missing_host_keys=no_host_keys_detail is not None,
            requested_region=body.region,
            requested_branch=body.attributes.get("repo_branch_or_tag"),
        ),
    )
    if leased is not None:
        return leased
    # Nothing leased. The quarantines above are already committed (the
    # transaction exited normally), so these raises never roll them back.
    if machine_quota_refusal is not None:
        entitlement, limit, current, noun = machine_quota_refusal
        raise_quota_exceeded(entitlement, limit, current, noun)
    if missing_certificate_error is not None:
        # ``handle_endpoint_errors`` answers this as the 503 ``management_certificate_unavailable``.
        raise missing_certificate_error
    if no_host_keys_detail is not None:
        raise HTTPException(status_code=503, detail=no_host_keys_detail)
    if is_update_required:
        # A plain string detail: the only clients that can reach this branch
        # predate the capability field, and they render the 503 detail verbatim.
        raise HTTPException(
            status_code=503,
            detail=(
                "This version of the app is too old for the current workspace fleet: no compatible "
                "hosts remain. Update the minds app and try again."
            ),
        )
    if is_pool_exhausted:
        raise HTTPException(
            status_code=503,
            detail=(
                "No pre-created agents match the requested attributes. "
                "Please ask Josh to provision more, or relax the attribute filter."
            ),
        )
    raise HTTPException(
        status_code=502,
        detail=(
            f"Failed to inject SSH key on {len(quarantined_host_db_ids)} pool host(s); "
            "they were quarantined (status 'unreachable'). Retry to try further hosts."
        ),
    )


class _PoolRowForRelease(BaseModel):
    """The columns a release needs from a pool row: ownership, status, and the slice's teardown coordinates."""

    host_db_id: str
    leased_to_user: str | None
    status: str
    slice_instance_name: str | None
    slice_disk_name: str | None
    bare_metal_server_id: Any
    mngr_host_id: str | None
    agent_id: str | None
    box_generation: int


def _read_pool_row_for_release(cur: Any, host_db_id: Any) -> _PoolRowForRelease | None:
    """Read a pool row's release projection on the caller's cursor; None when the row is gone."""
    # ``str(host_db_id)`` because psycopg2 can't adapt the Python ``UUID``
    # type that FastAPI parses from the path (it raises "can't adapt type 'UUID'").
    cur.execute(
        "SELECT leased_to_user, status, COALESCE(slice_instance_name, lima_instance_name), "
        "COALESCE(slice_disk_name, lima_disk_name), bare_metal_server_id, host_id, agent_id, "
        "box_generation FROM pool_hosts WHERE id = %s",
        (str(host_db_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _PoolRowForRelease(
        host_db_id=str(host_db_id),
        leased_to_user=row[0],
        status=row[1],
        slice_instance_name=row[2],
        slice_disk_name=row[3],
        bare_metal_server_id=row[4],
        mngr_host_id=row[5],
        agent_id=row[6],
        box_generation=int(row[7] or 1),
    )


def _begin_release(host_db_id: Any) -> _PoolRowForRelease | None:
    """Phase 1 of a release: durably record the intent, in one short transaction.

    Flips the row to ``removing`` (clearing the box link for a ``crashed``
    row, whose teardown is best-effort) and retires the workspace's record
    (tombstoned, or deleted while still the lease-time stub) in the same
    transaction, so the two views of the workspace never disagree about a
    destroy. Returns the row as it was *before* the flip (the teardown
    rules key on the pre-flip status), or None when there is nothing to
    release.
    """
    with db.pooled_db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                row = _read_pool_row_for_release(cur, host_db_id)
                # Anything not holding a lease (``available``, ``unreachable``)
                # is not this user's row to release.
                if row is None or row.status not in sync_module.LEASE_HOLDING_POOL_STATUSES:
                    return None
                if row.status == "crashed":
                    # A crashed row's teardown is best-effort, so the flip to
                    # ``removing`` must also clear the box link: a retry after a
                    # partial failure reads ``removing`` -- not ``crashed`` --
                    # and must not turn into a must-succeed teardown against the
                    # permanently dead box. The teardown attempt still runs with
                    # the values read above; a VM left on a box that was
                    # actually alive surfaces in the box-reconcile sweep.
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'removing', released_at = NOW(), "
                        "bare_metal_server_id = NULL WHERE id = %s",
                        (str(host_db_id),),
                    )
                elif row.status != "removing":
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'removing', released_at = NOW() WHERE id = %s",
                        (str(host_db_id),),
                    )
                else:
                    # Already ``removing`` (a retry): the intent is on record.
                    pass
                # Runs on the retry path too: a release that failed after the
                # flip but before this point still retires the record.
                if row.leased_to_user and row.agent_id:
                    sync_module.retire_active_record_for_lease(
                        cur, user_id_prefix=row.leased_to_user, agent_id=row.agent_id
                    )
    return row


# The errors a release can legitimately fail with (teardown, SSH, the DB
# round-trips); callers that confine a failure to one row catch exactly these.
# Anything else is a programming error and propagates.
RELEASE_FAILURE_ERROR_TYPES: Final[tuple[type[Exception], ...]] = (
    PoolHostCleanupError,
    paramiko.SSHException,
    OSError,
    psycopg2.Error,
)


def release_pool_host_row(host_db_id: Any) -> str:
    """Release a lease end to end; returns ``released`` or ``already_released``.

    The shared release chain. Three phases, none of which holds a DB
    connection across the external work: record the intent
    (``removing`` + record retirement, one transaction), delete the stop/start
    artifacts and destroy the slice VM, then drop the row on a fresh
    connection.

    Returns ``released`` only once *every* step has succeeded -- it truly
    means the VM is destroyed. A teardown failure raises (the row stays
    ``removing``, so a retry finishes the job). The one exception is a
    ``crashed`` row (operator-asserted permanently dead box): its teardown is
    best-effort, so a failure is logged, the row is deleted anyway, and any VM
    actually left behind surfaces in the box-reconcile sweep.
    """
    row = _begin_release(host_db_id)
    if row is None:
        return "already_released"
    # leased/stopping rows always have a VM, so teardown runs -- and fails
    # loudly on corrupt bookkeeping. For every other status the box link says
    # whether a VM exists: it is NULL once the retention finalize frees the
    # slot (a stopped row keeps its link -- and its halted local VM -- until
    # then) and during a restore before its final CAS places the VM (starting,
    # and removing rows descended from such a release, including crashed ones
    # -- the flip above clears the link). With a NULL link there is nothing to
    # tear down, so forcing a teardown would just wedge the row in
    # ``removing`` forever; the artifacts (if any) are the only cloud
    # resource left.
    is_vm_expected = row.status in ("leased", "stopping") or (row.bare_metal_server_id is not None)
    _delete_workspace_artifacts(row.mngr_host_id)
    if is_vm_expected and row.status == "crashed":
        # ``crashed`` is the operator's assertion (via abandon) that the box is
        # permanently dead, so a must-succeed teardown adds no safety and would
        # wedge the row in ``removing`` forever. Try anyway with the usual
        # bounded SSH timeout; if the operator was wrong and the box is alive,
        # any VM this leaves behind is exactly what the box-reconcile sweep
        # surfaces.
        try:
            clean_up_slice_on_box(
                host_db_id, row.bare_metal_server_id, row.slice_instance_name, row.slice_disk_name, row.box_generation
            )
        except (PoolHostCleanupError, paramiko.SSHException, OSError) as e:
            logger.warning(
                "Releasing crashed workspace %s: teardown against box %s failed (%s); deleting the row anyway",
                host_db_id,
                row.bare_metal_server_id,
                e,
            )
    elif is_vm_expected:
        clean_up_slice_on_box(
            host_db_id, row.bare_metal_server_id, row.slice_instance_name, row.slice_disk_name, row.box_generation
        )
    else:
        pass
    _delete_pool_host_row(host_db_id)
    return "released"


@router.post("/hosts/{host_db_id}/release")
def release_host(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Release a leased host: destroy its slice lima VM, then drop the row.

    Runs the full cleanup chain (``release_pool_host_row``) inline and
    **synchronously**; a teardown failure surfaces as a 5xx so the client
    retries -- we never report success on a failed teardown.

    Idempotent at the HTTP layer: a release on a row that is already gone
    (deleted) or no longer leased returns 200 ``status: already_released``.
    Ownership is still enforced -- a row leased by another user returns 403.
    """
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                row = _read_pool_row_for_release(cur, host_db_id)
        # A missing row means cleanup already finished (idempotent).
        if row is None:
            return ReleaseHostResponse(status="already_released").model_dump()
        # Ownership check first: we don't want to leak a status signal to
        # other users via the response code.
        if row.leased_to_user != user.user_id_prefix:
            raise HTTPException(status_code=403, detail="You do not own this host lease")
        return ReleaseHostResponse(status=release_pool_host_row(host_db_id)).model_dump()


@router.post("/admin/workspaces/{host_db_id}/release")
def admin_release_workspace(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Operator release of one workspace, regardless of owner (admin-key authenticated).

    Exactly the owner's release chain -- artifacts deleted, slice VM
    destroyed, record retired, row dropped -- so a confirmed-abandoned
    lease in any lifecycle status (a ``stopped`` row included, which the
    pool-destroy tooling cannot claim) is retired through the production path.
    Idempotent: a row already gone answers ``already_released``.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        status = release_pool_host_row(host_db_id)
        logger.info("Workspace %s released by operator: %s", host_db_id, status)
        return ReleaseHostResponse(status=status).model_dump()


def _delete_workspace_artifacts(mngr_host_id: str | None) -> None:
    """Delete a released workspace's stop/start artifacts from the tier bucket.

    A no-op when storage is unconfigured for this env (nothing could have
    been uploaded) or the row has no host id recorded. Raises on a real
    deletion failure so the release stays retryable (row remains
    ``removing``).
    """
    if not mngr_host_id:
        return
    if not storage_module.is_storage_configured():
        return
    storage_config = storage_module.read_storage_config()
    storage_module.delete_prefix(
        storage_config, f"{storage_module.workspace_key_prefix(storage_config, mngr_host_id)}/"
    )


@router.post("/hosts/{host_db_id}/rename")
def rename_host(request: Request, host_db_id: UUID, body: RenameHostRequest) -> dict[str, object]:
    """Rename a leased host: update the mutable ``host_name`` column on its row.

    The lease's ``host_db_id`` is the durable identity; only the friendly
    ``host_name`` changes, so a rename never touches the VPS/VM or the lease
    state. Ownership is enforced (a row leased by another user returns 403);
    a missing or not-leased row returns 404. ``host_name`` is validated by the
    request model against mngr's SafeName regex.
    """
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT leased_to_user, status FROM pool_hosts WHERE id = %s",
                    (str(host_db_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="No such host")
                leased_to_user, status = row
                # Ownership check first, to avoid leaking a status signal.
                if leased_to_user != user.user_id_prefix:
                    raise HTTPException(status_code=403, detail="You do not own this host lease")
                if status != "leased":
                    raise HTTPException(status_code=404, detail="Host is not currently leased")
                cur.execute(
                    "UPDATE pool_hosts SET host_name = %s WHERE id = %s",
                    (body.host_name, str(host_db_id)),
                )
                conn.commit()
        return RenameHostResponse(host_db_id=host_db_id, host_name=body.host_name).model_dump()


@router.get("/hosts")
def list_leased_hosts(request: Request) -> list[dict[str, object]]:
    """List all hosts currently leased by the authenticated user.

    Deprecated in favor of ``GET /workspaces`` (which returns every
    lifecycle state with a ``status`` field). Kept leased-only forever so
    released clients -- which treat every returned row as a live,
    SSH-reachable lease -- never see a stopped workspace here.
    CLEANUP: remove this route (and the client fallbacks to it) once every
    supported mngr/minds client consumes /workspaces.
    """
    with handle_endpoint_errors():
        user = accounts_web_module.authenticate_web_request(request)
        with db.pooled_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, "
                    "host_name, attributes, leased_at, outer_host_public_key, container_host_public_key "
                    "FROM pool_hosts "
                    "WHERE status = 'leased' AND leased_to_user = %s",
                    (user.user_id_prefix,),
                )
                rows = cur.fetchall()
        return [
            LeasedHostInfo(
                host_db_id=r[0],
                vps_address=r[1],
                ssh_port=r[2],
                ssh_user=r[3],
                container_ssh_port=r[4],
                agent_id=r[5],
                host_id=r[6],
                host_name=r[7],
                attributes=r[8] if isinstance(r[8], dict) else {},
                leased_at=str(r[9]) if r[9] is not None else "",
                outer_host_public_key=r[10],
                container_host_public_key=r[11],
            ).model_dump()
            for r in rows
        ]


def count_leased_hosts(user_id_prefix: str) -> int:
    """Count the user's running workspaces (the max_remote_workspaces usage number)."""
    return _count_user_workspaces(user_id_prefix, COUNT_RUNNING_WORKSPACES_SQL)


def count_total_workspaces(user_id_prefix: str) -> int:
    """Count the user's running + stopped workspaces (the max_total_workspaces usage number)."""
    return _count_user_workspaces(user_id_prefix, _COUNT_TOTAL_WORKSPACES_SQL)


def _count_user_workspaces(user_id_prefix: str, count_sql: str) -> int:
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(count_sql, (user_id_prefix,))
            row = cur.fetchone()
    return int(row[0]) if row is not None else 0


# Server-driven sharing (the web client cannot inject share materials itself)

# Where the share stack reads its materials inside the workspace container.
# Absolute: the raw-SSH exec channel starts in the login user's home, NOT the
# workspace root, so a relative path would land the materials where nothing
# reads them (verified live -- they ended up in $HOME/data/.secrets).
_SHARE_ENV_REMOTE_PATH = "/home/user/workspace/data/.secrets/share.env"
_SHARE_GRANTS_REMOTE_PATH = "/home/user/workspace/data/.secrets/share_grants.toml"


class EnableSharingResponse(BaseModel):
    """Response from POST /hosts/{host_db_id}/enable-sharing."""

    host_id: str = Field(description="The workspace's current machine (mngr host id)")
    workspace_id: str | None = Field(default=None, description="The workspace's id (agent-<32hex>), when known")
    workspace_domain: str = Field(description="The share's bare workspace domain")
    region: str = Field(description="The relay region the share is pinned to")
    entry_label: str | None = Field(
        default=None,
        description=(
            "The workspace's shell-service origin label (the chrome's routable entry origin is "
            "<entry_label>.<workspace_domain>); None until the workspace's tunnel has claimed "
            "its service labels (the frps NewProxy callback records it)."
        ),
    )


def build_share_env_text(
    *,
    workspace_domain: str,
    relay_token: str,
    connector_url: str,
    broker_url: str,
    chrome_origin: str,
) -> str:
    """Render share.env in the shape the workspace's share-gateway parses.

    Mirrors the desktop's ``build_share_env_text``; ``SHARE_CHROME_ORIGIN`` is
    what lets the hosted chrome embed the workspace and probe ``/_health``.
    Deliberately carries NO relay endpoint: the gateway fetches its current
    relay set from ``GET /shares/assignment`` (relay-token auth) and re-polls,
    so fleet changes never require re-injecting materials. The keys are a wire
    contract with the share-gateway's ``parse_share_materials`` (duplicated,
    not imported -- the connector image ships none of the workspace code).
    """
    lines = [
        f"export SHARE_WORKSPACE_DOMAIN={workspace_domain}",
        f"export SHARE_RELAY_TOKEN={relay_token}",
        f"export SHARE_CONNECTOR_URL={connector_url}",
        f"export SHARE_BROKER_URL={broker_url}",
    ]
    if chrome_origin:
        lines.append(f"export SHARE_CHROME_ORIGIN={chrome_origin}")
    return "\n".join(lines) + "\n"


def build_owner_grants_toml(owner_email: str | None) -> str:
    """Render the initial grants document: the owner (when known) at the workspace scope.

    The owner reaches the workspace through the gateway's owner fast path
    regardless of grants, so this is belt-and-suspenders; a known email is
    still seeded so a non-owner-claim visit by the owner also works.
    """
    emails = f'["{owner_email}"]' if owner_email else "[]"
    return f"[workspace]\nemails = {emails}\nemail_domains = []\n"


def build_container_file_write_command(remote_path: str, content: str, is_seed_only: bool) -> str:
    """The shell command that atomically writes ``content`` to ``remote_path``.

    Contents are base64-encoded in transit so arbitrary bytes (tokens, emails)
    never need shell quoting; the file is written to a temp name and renamed
    into place so a reader never sees a partial write. With ``is_seed_only``
    the whole write is skipped when the file already exists (seed-if-absent).
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    directory = shlex.quote(os.path.dirname(remote_path) or ".")
    quoted_path = shlex.quote(remote_path)
    write_command = (
        f"mkdir -p {directory} && "
        f'tmp="$(mktemp {directory}/.share.XXXXXX)" && '
        f"printf '%s' {shlex.quote(encoded)} | base64 -d > \"$tmp\" && "
        f'chmod 600 "$tmp" && mv "$tmp" {quoted_path}'
    )
    if is_seed_only:
        return f"[ -e {quoted_path} ] || {{ {write_command}; }}"
    return write_command


def _write_files_on_container(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    files_by_remote_path: dict[str, str],
    expected_host_public_key: str,
    # Paths whose write is skipped when the file already exists on the
    # container (seed-if-absent). Used for the grants document: re-enabling
    # sharing must never clobber grants the user has edited since.
    seed_only_remote_paths: AbstractSet[str],
) -> None:
    """SSH into a container with the management key and atomically write each file.

    See ``build_container_file_write_command`` for the write semantics (base64
    transport, temp-file-and-rename atomicity, seed-if-absent skipping).
    """
    with management_ssh_client(
        host, port, user, credentials, timeout_seconds=30, expected_host_public_key=expected_host_public_key
    ) as client:
        for remote_path, content in files_by_remote_path.items():
            command = build_container_file_write_command(
                remote_path, content, is_seed_only=remote_path in seed_only_remote_paths
            )
            _stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                stderr_text = stderr.read().decode()
                raise paramiko.SSHException(f"writing {remote_path} failed (exit {exit_status}): {stderr_text}")


@router.post("/hosts/{host_db_id}/enable-sharing")
def enable_sharing(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Bring sharing up for one of the caller's leased hosts, server-side.

    The web client cannot inject share materials itself (no SSH in the
    browser), so the connector does it with the pool key: create/rotate the
    share record, then write ``share.env`` (with the chrome origin) and the
    owner-granted ``share_grants.toml`` into the container. Idempotent -- a
    re-enable rotates the relay token and replaces ``share.env``, while the
    grants document is only seeded when absent (the workspace owns it after
    the first enable).
    """
    with handle_endpoint_errors():
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        return _enable_sharing_core(request, user, full_user_id, host_db_id).model_dump()


def _enable_sharing_core(
    request: Request, user: UserAuth, full_user_id: str, host_db_id: UUID
) -> EnableSharingResponse:
    """The share bring-up shared by ``enable-sharing`` and ``claim``.

    Verifies ownership + lease state, creates/rotates the share record, and
    writes the share materials into the container with the pool key:
    ``share.env`` is replaced on every enable (the relay token rotates), while
    the owner-granted grants document is only seeded when absent. Raises
    ``HTTPException`` on every failure.
    """
    try:
        user_label = shares_module.derive_share_user_label(full_user_id)
    except InvalidShareCoordinateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT leased_to_user, status, vps_address, container_ssh_port, ssh_user, host_id, "
                "container_host_public_key, agent_id, box_generation FROM pool_hosts WHERE id = %s",
                (str(host_db_id),),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such host")
    (
        leased_to_user,
        status,
        vps_address,
        container_ssh_port,
        ssh_user,
        host_id,
        container_host_public_key,
        workspace_id,
        share_box_generation,
    ) = row
    if leased_to_user != user.user_id_prefix:
        raise HTTPException(status_code=403, detail="You do not own this host lease")
    if status != "leased":
        raise HTTPException(status_code=409, detail="Host is not leased (cannot enable sharing)")
    if not container_host_public_key:
        raise HTTPException(
            status_code=503,
            detail=f"Host {host_db_id} has no pinned container host key yet; run the host-key backfill.",
        )

    management_credentials = management_credentials_for_generation(int(share_box_generation or 1))
    store = shares_module.get_share_store()
    # The workspace id (the pool row's pre-provisioned agent id) is the
    # share's durable key; the host-keyed fallback inside only covers rows old
    # clients created, never a row claimed by a different workspace.
    existing_share = shares_module.find_share_for_workspace(
        store, host_id, user_label, workspace_id if workspace_id else None
    )
    relay_rows = shares_module.active_relay_rows()
    region = shares_module.resolve_share_region_for_share(
        existing_region=str(existing_share["region"]) if existing_share is not None else None,
        datacenter=store.get_pool_host_datacenter(host_id),
        preferred_region=None,
        eligible_regions=relays_module.eligible_regions(relay_rows),
        host_id=host_id,
    )
    if existing_share is not None:
        coordinate = shares_module.coordinate_from_stored_share(
            existing_share, user_label, workspace_id_backfill=workspace_id
        )
    elif workspace_id:
        coordinate = shares_module.make_workspace_share_coordinate(
            host_id=host_id,
            workspace_id=workspace_id,
            share_label=shares_module.generate_share_label(),
            user_id=full_user_id,
            region=region,
            content_domain=shares_module.share_content_domain(),
        )
    else:
        # A pool row without an agent id should not exist; keep the legacy
        # host-led coordinate as the safe degradation.
        coordinate = shares_module.make_share_coordinate(
            host_id=host_id,
            user_label=user_label,
            region=region,
            content_domain=shares_module.share_content_domain(),
        )
    # Reach the container BEFORE touching the share: activating the share
    # rotates its relay token, and a rotation the container never receives
    # kills the tunnel a desktop-established share is already running on.
    try:
        _assert_container_serves_pinned_host_key(vps_address, container_ssh_port, container_host_public_key)
    except paramiko.BadHostKeyException as exc:
        raise _desktop_managed_workspace_conflict(host_db_id) from exc
    except (paramiko.SSHException, OSError) as exc:
        logger.warning("Could not reach host %s to enable sharing: %s", host_db_id, exc)
        raise HTTPException(status_code=502, detail=f"Failed to enable sharing on host: {exc}") from exc
    relay_token = shares_module.generate_relay_token()
    # No entry label is supplied here: the frps NewProxy callback records it
    # once the workspace's tunnel claims its service labels, so the connector
    # never has to read anything from inside the workspace. The COALESCE in
    # the activation keeps a label an earlier tunnel already recorded.
    store.activate_share_and_rotate_token(
        coordinate,
        shares_module.DEFAULT_MAX_SHARED_WORKSPACES_PER_USER,
        shares_module.hash_relay_token(relay_token),
        None,
    )

    base_url = accounts_web_module.accounts_public_base_url(request)
    share_env_text = build_share_env_text(
        workspace_domain=coordinate.workspace_domain,
        relay_token=relay_token,
        connector_url=base_url,
        broker_url=base_url,
        chrome_origin=shares_module.share_chrome_origin(),
    )
    grants_text = build_owner_grants_toml(user.email)
    try:
        _write_files_on_container(
            vps_address,
            container_ssh_port,
            ssh_user,
            management_credentials,
            {_SHARE_GRANTS_REMOTE_PATH: grants_text, _SHARE_ENV_REMOTE_PATH: share_env_text},
            container_host_public_key,
            # share.env must be replaced (each enable rotates the relay token),
            # but the grants document is only seeded on first enable: the
            # workspace owns it afterwards, and a re-enable overwriting it
            # would silently revoke every grant the user added since.
            seed_only_remote_paths=frozenset({_SHARE_GRANTS_REMOTE_PATH}),
        )
    except paramiko.BadHostKeyException as exc:
        # The key changed between the probe above and this write (an adoption
        # racing the enable); same answer, the desktop app owns the share now.
        raise _desktop_managed_workspace_conflict(host_db_id) from exc
    except (paramiko.SSHException, OSError) as exc:
        logger.warning("Failed to inject share materials on host %s: %s", host_db_id, exc)
        raise HTTPException(status_code=502, detail=f"Failed to enable sharing on host: {exc}") from exc

    # Report whatever entry label an earlier tunnel already recorded (None on
    # a first enable, until the tunnel comes up and NewProxy records one).
    share_row = store.get_share(coordinate.host_id, user_label)
    recorded_entry_label = share_row.get("entry_label") if share_row is not None else None
    return EnableSharingResponse(
        host_id=host_id,
        workspace_id=coordinate.workspace_id,
        workspace_domain=coordinate.workspace_domain,
        region=region,
        entry_label=recorded_entry_label,
    )


# Web workspace creation (POST /hosts/claim)

# The in-container host_dir layouts pool hosts have been baked with, newest
# first. Mirrors mngr's ``KNOWN_WORKSPACE_HOST_DIRS`` (libs/mngr/imbue/mngr/
# providers/host_dir_layouts.py) -- duplicated, not imported, because
# ``imbue.mngr`` does not ship into the connector container.
_KNOWN_WORKSPACE_HOST_DIRS: Final = ("/home/user/.mngr", "/mngr")

# Env vars carrying the tier's pinned web-create template + blessed compute
# shape, pushed into the connector's Modal secret by ``minds-admin env deploy`` from
# the tier's ``deploy.toml`` ``[web_workspaces]`` block. The repo value is the
# canonical ``host/org/repo`` key the pool bake stamps into row attributes.
_WEB_TEMPLATE_REPO_ENV_VAR: Final = "MINDS_WEB_TEMPLATE_REPO"
_WEB_TEMPLATE_REF_ENV_VAR: Final = "MINDS_WEB_TEMPLATE_REF"
_WEB_SHAPE_CPUS_ENV_VAR: Final = "MINDS_WEB_SHAPE_CPUS"
_WEB_SHAPE_MEMORY_GB_ENV_VAR: Final = "MINDS_WEB_SHAPE_MEMORY_GB"
_WEB_SHAPE_GPU_COUNT_ENV_VAR: Final = "MINDS_WEB_SHAPE_GPU_COUNT"


class ClaimHostRequest(BaseModel):
    ssh_public_key: str = Field(description="SSH public key to authorize on the claimed host")
    host_name: str = Field(
        description=(
            "User-chosen slug name for the workspace host. Must satisfy mngr's SafeName "
            "regex (alphanumeric, dashes/underscores allowed in the middle). Required."
        )
    )
    display_name: str | None = Field(
        default=None,
        description=(
            "Arbitrary human-readable workspace display name, stamped as the "
            "``workspace_display_name`` label on the adopted agent. Defaults to ``host_name``."
        ),
    )
    region: str | None = Field(
        default=None,
        description="Hard region requirement (lease-region label). Unset means region-agnostic.",
    )

    _validate_host_name = field_validator("host_name")(_validate_host_name)


class ClaimHostResponse(BaseModel):
    """Response from POST /hosts/claim: the lease plus the share bring-up result."""

    host_db_id: UUID = Field(description="Database ID of the claimed lease")
    vps_address: str = Field(description="SSH-reachable address of the host's bare-metal box")
    ssh_port: int = Field(description="SSH port on the VPS")
    ssh_user: str = Field(description="SSH user on the VPS")
    container_ssh_port: int = Field(description="SSH port mapped to the Docker container")
    agent_id: str = Field(description="Pre-provisioned mngr agent ID (the services agent)")
    host_id: str = Field(description="Host ID in the mngr provider")
    host_name: str = Field(description="User-chosen slug name for the host")
    display_name: str = Field(description="Human-readable workspace display name")
    outer_host_public_key: str = Field(description="Pinned VPS sshd host public key")
    container_host_public_key: str = Field(description="Pinned container sshd host public key")
    workspace_domain: str = Field(description="The share's bare workspace domain")
    region: str = Field(description="The relay region the share is pinned to")
    entry_label: str | None = Field(
        default=None,
        description=(
            "The workspace's shell-service origin label (the chrome's routable entry origin is "
            "<entry_label>.<workspace_domain>); None until the workspace's tunnel has claimed "
            "its service labels (the frps NewProxy callback records it)."
        ),
    )


def _web_claim_pinned_attributes() -> dict[str, Any] | None:
    """The lease-attribute filter for web creates, from the tier's pinned config.

    Returns None when the tier has no pinned template (web creation disabled).
    The shape pins are optional: when unset, the filter leaves them
    unconstrained (JSONB containment only matches fields present in the
    filter), so a tier with one uniform slice size does not have to restate it.
    """
    template_repo = os.environ.get(_WEB_TEMPLATE_REPO_ENV_VAR, "").strip()
    template_ref = os.environ.get(_WEB_TEMPLATE_REF_ENV_VAR, "").strip()
    if not template_repo or not template_ref:
        return None
    attributes: dict[str, Any] = {"repo_url": template_repo, "repo_branch_or_tag": template_ref}
    for env_var, attribute_name in (
        (_WEB_SHAPE_CPUS_ENV_VAR, "cpus"),
        (_WEB_SHAPE_MEMORY_GB_ENV_VAR, "memory_gb"),
        (_WEB_SHAPE_GPU_COUNT_ENV_VAR, "gpu_count"),
    ):
        raw_value = os.environ.get(env_var, "").strip()
        if raw_value:
            attributes[attribute_name] = int(raw_value)
    return attributes


# The first default-workspace-template release line that runs on gen-2 boxes:
# minds-v0.6.x+ releases bake onto gen-2, minds-v0.5.x and older onto gen-1
# (the bake-time guard in minds-admin keeps the pools disjoint).
_FIRST_GEN2_RELEASE: Final[tuple[int, int]] = (0, 6)

_RELEASE_TAG_RE: Final = re.compile(r"^minds-v(\d+)\.(\d+)\.")


def _web_claim_max_box_generation(template_ref: str) -> int:
    """The generation cap a web create carries, derived from the tier's pinned template ref.

    A ``minds-v*`` tag below the first gen-2 release line pins the claim to
    gen-1 rows (the pre-0.6 template pairs with gen-1 boxes); 0.6+ tags and
    non-tag refs (dev branches, always current code) are gen-2-capable.
    CLEANUP: collapse to unrestricted with the request field (see
    ``LeaseHostRequest.max_box_generation``).
    """
    match = _RELEASE_TAG_RE.match(template_ref)
    if match is not None and (int(match.group(1)), int(match.group(2))) < _FIRST_GEN2_RELEASE:
        return 1
    return FIRST_QEMU_BOX_GENERATION


def _replace_env_file_line(existing_content: str, key: str, value: str) -> str:
    """Replace-or-append one ``KEY=VALUE`` line, preserving every other line verbatim."""
    kept_lines = [line for line in existing_content.splitlines() if not line.startswith(f"{key}=")]
    kept_lines.append(f"{key}={value}")
    return "\n".join(kept_lines) + "\n"


def _adopt_workspace_on_container(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    expected_host_public_key: str,
    agent_id: str,
    host_name: str,
    display_name: str,
    connector_url: str,
) -> None:
    """Adopt a freshly leased pool container for a web-created workspace.

    The connector-side port of the plugin's fast-path adopt (see
    ``mngr_imbue_cloud.providers.instance`` / ``hosts.host``): rewrite the
    host record's placeholder ``host_name`` (the dwt system_interface reads
    it as the workspace's name), stamp the minds labels on the pre-baked
    services agent, and write the connector URL into the host env file so
    everything on the host can reach this tier's connector. All writes go
    through SFTP (no shell quoting of user-controlled names).

    Raises ``paramiko.SSHException`` / ``OSError`` on any failure; the caller
    releases the lease and surfaces the error.
    """
    with management_ssh_client(
        host, port, user, credentials, timeout_seconds=30, expected_host_public_key=expected_host_public_key
    ) as client:
        sftp = client.open_sftp()
        try:
            # Locate the host record: the bake wrote exactly one of the known
            # layouts, and finding it is the only proof of which layout this
            # pool generation carries.
            host_dir = None
            host_record_raw = None
            for candidate in _KNOWN_WORKSPACE_HOST_DIRS:
                try:
                    with sftp.open(f"{candidate}/data.json", "r") as remote:
                        host_record_raw = remote.read()
                    host_dir = candidate
                    break
                except FileNotFoundError:
                    continue
            if host_dir is None or host_record_raw is None:
                raise paramiko.SSHException(
                    f"no host record found on claimed container at any of: {', '.join(_KNOWN_WORKSPACE_HOST_DIRS)}"
                )

            # Rewrite the bake's placeholder host name to the user's choice.
            host_record = json.loads(host_record_raw)
            if not isinstance(host_record, dict):
                raise paramiko.SSHException(f"{host_dir}/data.json did not parse to an object")
            host_record["host_name"] = host_name
            with sftp.open(f"{host_dir}/data.json", "w") as remote:
                remote.write(json.dumps(host_record, indent=2).encode())

            # Stamp the minds labels on the pre-baked services agent, merging
            # over the bake's labels exactly like the desktop create does.
            agent_data_path = f"{host_dir}/agents/{agent_id}/data.json"
            with sftp.open(agent_data_path, "r") as remote:
                agent_record_raw = remote.read()
            agent_record = json.loads(agent_record_raw)
            if not isinstance(agent_record, dict):
                raise paramiko.SSHException(f"{agent_data_path} did not parse to an object")
            merged_labels = dict(agent_record.get("labels") or {})
            merged_labels.update(
                {
                    "workspace_display_name": display_name,
                    "user_created": "true",
                    "is_primary": "true",
                }
            )
            agent_record["labels"] = merged_labels
            with sftp.open(agent_data_path, "w") as remote:
                remote.write(json.dumps(agent_record, indent=2).encode())

            # Merge the connector URL into the host env file (line-level
            # replace-or-append so the bake's entries survive verbatim).
            env_path = f"{host_dir}/env"
            try:
                with sftp.open(env_path, "r") as remote:
                    env_content = remote.read().decode("utf-8")
            except FileNotFoundError:
                env_content = ""
            with sftp.open(env_path, "w") as remote:
                remote.write(
                    _replace_env_file_line(env_content, "REMOTE_SERVICE_CONNECTOR_URL", connector_url).encode()
                )
        finally:
            sftp.close()


# The template-committed start script for the services agent. Baked slices are
# left stopped (the bake SIGTERMs supervisord and tmux dies with it), and the
# desktop's ``mngr create --reuse`` is what normally boots the agent -- a web
# claim has no desktop, so the connector runs the same script the template's
# VM-boot autostart units use. It sources the host + agent env exactly like
# mngr does and then runs the idempotent, flock-serialized
# ``mngr start system-services``.
_START_SERVICES_AGENT_SCRIPT: Final = "/home/user/workspace/system/scripts/minds_start_services_agent.sh"


def _start_workspace_agent_on_container(
    host: str,
    port: int,
    user: str,
    credentials: ManagementSshCredentials,
    expected_host_public_key: str,
) -> None:
    """Start the claimed workspace's pre-baked services agent over SSH.

    Runs the template's shared start script through a login shell (so uv/mngr
    are on PATH), mirroring the outer-VM autostart unit's ``docker exec``.
    Raises ``paramiko.SSHException`` on a non-zero exit so the caller releases
    the lease -- a claim that cannot boot the workspace must not hand the user
    a dead lease.
    """
    with management_ssh_client(
        host, port, user, credentials, timeout_seconds=30, expected_host_public_key=expected_host_public_key
    ) as client:
        command = f"bash -lc 'exec {_START_SERVICES_AGENT_SCRIPT}'"
        _stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            stderr_text = stderr.read().decode()
            raise paramiko.SSHException(f"starting the services agent failed (exit {exit_status}): {stderr_text}")


def _release_lease_after_failed_claim(host_db_id: UUID) -> None:
    """Best-effort release of a lease whose claim failed partway.

    A claim that cannot finish adopting must not leave the user holding a
    half-configured lease (it counts against their quota and the browser has
    no way to repair it). Runs the shared release chain; failures here are
    logged and swallowed -- the original claim error is what the caller
    surfaces, and the lease-record sweep retries rows stuck in ``removing``.
    """
    try:
        release_pool_host_row(host_db_id)
    except RELEASE_FAILURE_ERROR_TYPES as exc:
        logger.warning("Failed to release lease %s after a failed claim (sweep will retry): %s", host_db_id, exc)


@router.post("/hosts/claim")
def claim_host(request: Request, body: ClaimHostRequest) -> dict[str, object]:
    """Create a web-reachable workspace in one synchronous call.

    The browser-driven create primitive: lease a pool host matching the
    tier's pinned template + shape (fast path only -- no rebuild), adopt the
    pre-baked workspace over SSH with the pool key (host_name rewrite,
    display-name label, connector URL in the host env), then bring sharing up
    (share record + materials injection) so the workspace is reachable from
    the web the moment this returns.

    A failure after the lease releases it (slice teardown) before the error
    propagates, so a retry starts clean. Refused with 503 when the tier has
    no pinned web template configured. Like ``/hosts/lease``, requires a
    verified email: an unverified account gets the structured
    ``email_not_verified`` 403 and the refusal sends the verification email.
    """
    with handle_endpoint_errors():
        user, full_user_id = accounts_web_module.resolve_web_user_identity(request)
        auth_proxy_module.require_verified_email_for_remote_workspace(user, full_user_id)
        entitlements = entitlements_module.resolve_entitlements_for_user(full_user_id, user)
        pinned_attributes = _web_claim_pinned_attributes()
        if pinned_attributes is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Web workspace creation is not configured on this tier (no pinned template in the deploy config)."
                ),
            )
        display_name = body.display_name or body.host_name
        lease = _lease_pool_host(
            user,
            full_user_id,
            entitlements,
            LeaseHostRequest(
                ssh_public_key=body.ssh_public_key,
                host_name=body.host_name,
                attributes=pinned_attributes,
                region=body.region,
                max_box_generation=_web_claim_max_box_generation(str(pinned_attributes.get("repo_branch_or_tag", ""))),
            ),
            record_display_name=display_name,
        )
        # Everything between the lease and a fully-shared workspace runs under
        # the completion flag: ANY failure past this point (adopt, share
        # bring-up, or anything unexpected) releases the lease in the finally,
        # so the error never leaves the user holding a half-configured lease.
        is_claim_complete = False
        try:
            management_credentials = management_credentials_for_generation(lease.box_generation)
            base_url = accounts_web_module.accounts_public_base_url(request)
            try:
                _adopt_workspace_on_container(
                    lease.vps_address,
                    lease.container_ssh_port,
                    lease.ssh_user,
                    management_credentials,
                    lease.container_host_public_key,
                    lease.agent_id,
                    body.host_name,
                    display_name,
                    base_url,
                )
            except (paramiko.SSHException, OSError, json.JSONDecodeError) as exc:
                logger.warning("Adopt failed for claimed host %s: %s", lease.host_db_id, exc)
                raise HTTPException(status_code=502, detail=f"Failed to adopt the claimed host: {exc}") from exc
            # Boot the adopted agent: the bake leaves slices stopped and there
            # is no desktop here to start them. Runs after the adopt so the
            # tmux session sources the rewritten host env (connector URL), and
            # before the share bring-up so a boot failure leaves no share row.
            try:
                _start_workspace_agent_on_container(
                    lease.vps_address,
                    lease.container_ssh_port,
                    lease.ssh_user,
                    management_credentials,
                    lease.container_host_public_key,
                )
            except (paramiko.SSHException, OSError) as exc:
                logger.warning("Agent start failed for claimed host %s: %s", lease.host_db_id, exc)
                raise HTTPException(
                    status_code=502, detail=f"Failed to start the claimed workspace's agent: {exc}"
                ) from exc
            sharing = _enable_sharing_core(request, user, full_user_id, lease.host_db_id)
            is_claim_complete = True
        finally:
            if not is_claim_complete:
                logger.warning("Claim of host %s failed after the lease; releasing it", lease.host_db_id)
                _release_lease_after_failed_claim(lease.host_db_id)
        return ClaimHostResponse(
            host_db_id=lease.host_db_id,
            vps_address=lease.vps_address,
            ssh_port=lease.ssh_port,
            ssh_user=lease.ssh_user,
            container_ssh_port=lease.container_ssh_port,
            agent_id=lease.agent_id,
            host_id=lease.host_id,
            host_name=lease.host_name,
            display_name=display_name,
            outer_host_public_key=lease.outer_host_public_key,
            container_host_public_key=lease.container_host_public_key,
            workspace_domain=sharing.workspace_domain,
            region=sharing.region,
            entry_label=sharing.entry_label,
        ).model_dump()
