"""Pool-host leasing: lease/release/rename/list endpoints and slice cleanup.

A pool host is a "slice": a lima VM on one of our bare-metal boxes. Releasing
it (the inline release path) destroys the VM by SSHing the box and running
limactl. The connector makes no provider-API calls of its own.
"""

import contextlib
import io
import json
import logging
import os
import re
import shlex
from collections.abc import Iterator
from typing import Any
from typing import Final
from uuid import UUID

import paramiko
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from paramiko.hostkeys import HostKeyEntry
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

import imbue.remote_service_connector.entitlements as entitlements_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import authenticate_request
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.errors import InvalidHostNameError
from imbue.remote_service_connector.errors import PoolHostCleanupError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()


# Mirror of mngr's SafeName regex (libs/mngr/imbue/mngr/primitives.py:_SAFE_NAME_RE).
# Duplicated here -- not imported -- because the shipped connector package
# must not depend on the monorepo. Keep this in sync if the mngr-side rule
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


# What counts as one "remote workspace": a pool-host row leased to the user
# (running or stopped -- stopped workspaces still hold their lease and slice).
# Shared by the lease-time quota check and the /account usage display so the
# two can never drift.
_COUNT_LEASED_HOSTS_SQL: Final = "SELECT COUNT(*) FROM pool_hosts WHERE leased_to_user = %s AND status = 'leased'"


def _pin_expected_host_key(client: paramiko.SSHClient, host: str, port: int, expected_host_public_key: str) -> None:
    """Pin ``expected_host_public_key`` for ``host:port`` and reject any other host key.

    paramiko keys non-default ports under the ``[host]:port`` known-hosts name, so
    a container/forwarded port must be pinned under that bracketed name to match
    what ``connect`` looks up. Replaces trust-on-first-use: a mismatched or
    unknown host key is rejected.
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
    client.get_host_keys().add(known_hosts_name, entry.key.get_name(), entry.key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())


@contextlib.contextmanager
def _management_ssh_client(
    host: str,
    port: int,
    user: str,
    management_key_pem: str,
    timeout_seconds: float,
    expected_host_public_key: str,
) -> Iterator[paramiko.SSHClient]:
    """Yield an SSHClient connected to ``host`` with the pool management key, closed on exit.

    The host is authenticated against ``expected_host_public_key`` (strict pinning,
    no trust-on-first-use); callers fail closed when no pinned key is available.
    """
    private_key = paramiko.Ed25519Key.from_private_key(io.StringIO(management_key_pem))
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
    management_key_pem: str,
    public_key_to_add: str,
    expected_host_public_key: str,
) -> None:
    """SSH into a host using the management key and append a public key to authorized_keys."""
    with _management_ssh_client(
        host, port, user, management_key_pem, timeout_seconds=15, expected_host_public_key=expected_host_public_key
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


def _delete_pool_host_row(conn: Any, host_db_id: Any) -> None:
    """Delete a single pool_hosts row by id (committing immediately)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (str(host_db_id),))
    conn.commit()


def build_slice_teardown_commands(lima_instance_name: str, lima_disk_name: str | None) -> tuple[str, ...]:
    """Commands to run on the bare-metal box to destroy a slice's lima VM + data disk."""
    commands = [f"limactl delete --force {shlex.quote(lima_instance_name)}"]
    if lima_disk_name:
        commands.append(f"limactl disk delete --force {shlex.quote(lima_disk_name)}")
    return tuple(commands)


def _run_ssh_commands_on_box(
    host: str, port: int, user: str, management_key_pem: str, commands: tuple[str, ...], box_host_public_key: str
) -> None:
    """SSH into the box with the pool management key and run each command, raising on failure."""
    with _management_ssh_client(
        host, port, user, management_key_pem, timeout_seconds=30, expected_host_public_key=box_host_public_key
    ) as client:
        for command in commands:
            _stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                stderr_text = stderr.read().decode()
                raise PoolHostCleanupError(
                    f"slice teardown command {command!r} failed (exit {exit_status}): {stderr_text}"
                )


def clean_up_slice_on_box(
    conn: Any,
    host_db_id: Any,
    bare_metal_server_id: Any,
    lima_instance_name: str | None,
    lima_disk_name: str | None,
) -> None:
    """Destroy a slice's lima VM (and data disk) on its owning bare-metal box.

    Looks up the box's address + lima service user from ``bare_metal_servers``,
    then SSHes in with the pool management key and runs limactl. Raises
    ``PoolHostCleanupError`` if the slice's bookkeeping is incomplete or the box
    can't be reached, so the row stays ``removing`` and the sweep retries (the
    slot is only freed once the VM is really gone).
    """
    if not (bare_metal_server_id and lima_instance_name):
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id} is missing bare_metal_server_id or lima_instance_name; cannot tear down its VM"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_address, lima_service_user, box_host_public_key FROM bare_metal_servers WHERE id = %s",
            (str(bare_metal_server_id),),
        )
        server_row = cur.fetchone()
    if server_row is None or not server_row[0]:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} is missing or has no public_address"
        )
    box_address, lima_service_user, box_host_public_key = server_row[0], server_row[1] or "root", server_row[2]
    # Fail closed: without the box's pinned host key we cannot reach it without
    # trust-on-first-use. The row stays ``removing`` and the sweep retries once
    # the one-time keyscan backfill has populated the column.
    if not box_host_public_key:
        raise PoolHostCleanupError(
            f"slice pool host {host_db_id}: bare_metal_servers row {bare_metal_server_id} has no box_host_public_key "
            "(run the one-time `mngr imbue_cloud admin` host-key backfill)"
        )
    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
    commands = build_slice_teardown_commands(lima_instance_name, lima_disk_name)
    _run_ssh_commands_on_box(box_address, 22, lima_service_user, management_key_pem, commands, box_host_public_key)


# Slice lima resources are named ``mngr-slice-<env>-<host-hex>`` (the data disk
# adds a ``-data`` suffix). The host hex is a hyphen-free uuid, so the env stamp is
# everything between the prefix and the trailing ``-<host-hex>``. Mirrors
# ``mngr_imbue_cloud.slices.bare_metal`` (the connector has no dependency on it).
_SLICE_LIMA_PREFIX = "mngr-slice-"
_SLICE_LIMA_DISK_SUFFIX = "-data"
_STAMPED_SLICE_CORE_RE = re.compile(r"^(?P<env>.+)-(?P<host>[0-9a-f]{32})$")
# Non-login SSH may not source the lima user's profile, so set PATH explicitly
# (limactl is extracted to /usr/local/bin by box prep).
_BOX_LIMACTL_PATH_PREFIX = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH"


def slice_name_env_owner(name: str) -> str | None:
    """The env a slice instance/disk name is stamped for, or None if legacy/foreign/not-a-slice."""
    if not name.startswith(_SLICE_LIMA_PREFIX):
        return None
    core = name[len(_SLICE_LIMA_PREFIX) :]
    if core.endswith(_SLICE_LIMA_DISK_SUFFIX):
        core = core[: -len(_SLICE_LIMA_DISK_SUFFIX)]
    match = _STAMPED_SLICE_CORE_RE.match(core)
    return match.group("env") if match else None


def _list_box_lima_names(
    host: str, user: str, management_key_pem: str, json_subcommand: str, box_host_public_key: str
) -> set[str]:
    """SSH the box and return the ``name`` of every lima instance or disk (per ``json_subcommand``).

    ``json_subcommand`` is ``list --json`` (instances) or ``disk list --json`` (disks);
    both emit one JSON object per line. Raises ``PoolHostCleanupError`` on a non-zero exit
    so the caller skips this box rather than mistaking an SSH failure for "no VMs".
    """
    names: set[str] = set()
    command = f"{_BOX_LIMACTL_PATH_PREFIX} limactl {json_subcommand}"
    with _management_ssh_client(
        host, 22, user, management_key_pem, timeout_seconds=30, expected_host_public_key=box_host_public_key
    ) as client:
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


def reconcile_slice_boxes(conn: Any, env_name: str) -> int:
    """Audit each box's lima slices against the DB, scoped to ``env_name``'s stamped slices.

    Returns the number of divergences found (and logged). Alert-only by design: for
    every bare-metal box it logs, at error level,

    * a slice stamped for ``env_name`` present on the box with no pool_hosts row, and
    * a pool_hosts row whose VM is absent from the box.

    It deliberately does NOT auto-delete. A row-less stamped slice is most often a
    bake mid-flight (the carve creates the instance ~10-30 min before it inserts the
    row), and this cron runs on a fixed hourly schedule independent of bakes -- so
    auto-reaping here would race a live bake and destroy its slice. Actual reaping is
    left to the bake-time reaper (which runs in the bake's own ``finally``, where the
    in-flight set is known). If a box's lima resources cannot be listed, this raises:
    a box we could not inspect was NOT reconciled, and that failure must surface
    rather than be mistaken for a clean audit. Other envs' slices and legacy
    un-stamped slices are never inspected, so this is safe on a shared box.
    """
    if not env_name:
        logger.info("Slice reconcile skipped: connector has no MINDS_ENV_NAME to scope to")
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT id, public_address, lima_service_user, box_host_public_key FROM bare_metal_servers")
        servers = cur.fetchall()
    # Read the pool key only once we know there are boxes to inspect: a deployment
    # with no slice infrastructure (no boxes, no POOL_SSH_PRIVATE_KEY) must not fail
    # here.
    if not servers:
        return 0
    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
    divergence_count = 0
    for server_id, public_address, lima_service_user, box_host_public_key in servers:
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
        user = lima_service_user or "root"
        # If we cannot list a box's lima resources we did NOT reconcile it; let the
        # failure propagate rather than silently skipping (which would look like a
        # clean audit and could mask vanished/leaked VMs).
        box_instances = _list_box_lima_names(
            public_address, user, management_key_pem, "list --json", box_host_public_key
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lima_instance_name FROM pool_hosts WHERE bare_metal_server_id = %s",
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
    """
    with handle_endpoint_errors():
        user = authenticate_request(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(request, user)
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize this user's leases for the duration of the
                    # transaction, then enforce the workspace quota. The
                    # advisory lock releases automatically at commit/rollback.
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user.user_id_prefix,))
                    cur.execute(_COUNT_LEASED_HOSTS_SQL, (user.user_id_prefix,))
                    count_row = cur.fetchone()
                    leased_count = int(count_row[0]) if count_row is not None else 0
                    if leased_count >= entitlements.max_remote_workspaces:
                        raise_quota_exceeded(
                            "max_remote_workspaces",
                            entitlements.max_remote_workspaces,
                            leased_count,
                            "remote workspaces",
                        )
                    # Build the lease selection dynamically. A hard ``region``
                    # adds an equality filter; when unset the lease is
                    # region-agnostic. The selection stays a single round-trip
                    # (the fast path must not pay an extra query).
                    where_clauses = ["status = 'available'", "attributes @> %s::jsonb"]
                    query_params: list[object] = [json.dumps(body.attributes)]
                    if body.region is not None:
                        where_clauses.append("region = %s")
                        query_params.append(body.region)
                    order_by = "created_at ASC"
                    lease_select_sql = (
                        "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, attributes, "
                        "outer_host_public_key, container_host_public_key "
                        "FROM pool_hosts "
                        f"WHERE {' AND '.join(where_clauses)} "
                        f"ORDER BY {order_by} LIMIT 1 FOR UPDATE SKIP LOCKED"
                    )
                    cur.execute(lease_select_sql, tuple(query_params))
                    row = cur.fetchone()
                    if row is None:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "No pre-created agents match the requested attributes. "
                                "Please ask Josh to provision more, or relax the attribute filter."
                            ),
                        )
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
                    ) = row

                    # Fail closed: a row without both pinned host keys cannot be
                    # leased without trust-on-first-use. This only happens for rows
                    # baked before the host-key columns existed; the one-time
                    # keyscan backfill populates them. Surface it as no-capacity so
                    # the caller (and the fast/slow path retry) treats it like an
                    # unavailable host rather than a hard error.
                    if not outer_host_public_key or not container_host_public_key:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"Pool host {host_db_id} has no pinned SSH host keys yet; "
                                "run the one-time `mngr imbue_cloud admin` host-key backfill."
                            ),
                        )

                    # Inject the user's SSH public key on VPS and container, pinning
                    # each sshd's recorded host key (strict, no trust-on-first-use).
                    management_key_pem = os.environ["POOL_SSH_PRIVATE_KEY"]
                    try:
                        _append_authorized_key(
                            vps_address,
                            ssh_port,
                            ssh_user,
                            management_key_pem,
                            body.ssh_public_key,
                            outer_host_public_key,
                        )
                        _append_authorized_key(
                            vps_address,
                            container_ssh_port,
                            ssh_user,
                            management_key_pem,
                            body.ssh_public_key,
                            container_host_public_key,
                        )
                    except (paramiko.SSHException, OSError) as exc:
                        logger.warning("SSH key injection failed for host %s: %s", host_db_id, exc)
                        raise HTTPException(
                            status_code=502, detail=f"Failed to inject SSH key on host: {exc}"
                        ) from exc

                    # ``host_name`` is mutable per-lease: it gets overwritten with the
                    # user-supplied name each time the pool row is leased (and could
                    # later be patched by a rename endpoint).
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'leased', leased_to_user = %s, "
                        "leased_at = NOW(), host_name = %s WHERE id = %s",
                        (user.user_id_prefix, body.host_name, host_db_id),
                    )
        finally:
            conn.close()
        attrs_dict = attributes if isinstance(attributes, dict) else {}
        return LeaseHostResponse(
            host_db_id=host_db_id,
            vps_address=vps_address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            agent_id=agent_id,
            host_id=host_id,
            host_name=body.host_name,
            attributes=attrs_dict,
            outer_host_public_key=outer_host_public_key,
            container_host_public_key=container_host_public_key,
        ).model_dump()


@router.post("/hosts/{host_db_id}/release")
def release_host(request: Request, host_db_id: UUID) -> dict[str, object]:
    """Release a leased host: destroy its slice lima VM, then drop the row.

    Runs the full cleanup chain inline and **synchronously**: flip the row to
    ``removing`` (the durable, retryable in-progress marker), destroy the slice's
    lima VM on its bare-metal box, then delete the row.

    Returns 200 only once *every* step has succeeded -- a "released" result
    truly means the VM is destroyed. If any teardown step fails, the row stays
    ``removing`` and the endpoint returns an error (5xx) so the client retries;
    we never report success on a failed teardown. A failure before ``removing``
    is committed (lookup, ownership, the status flip) surfaces as an error too.

    Idempotent at the HTTP layer: a release on a row that is already gone
    (deleted) or no longer leased returns 200 ``status: already_released``.
    Ownership is still enforced -- a row leased by another user returns 403.
    """
    with handle_endpoint_errors():
        user = authenticate_request(request)
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                # ``str(host_db_id)`` because psycopg2 can't adapt the
                # Python ``UUID`` type that FastAPI parsed from the path
                # (it raises "can't adapt type 'UUID'").
                cur.execute(
                    "SELECT leased_to_user, status, "
                    "lima_instance_name, lima_disk_name, bare_metal_server_id "
                    "FROM pool_hosts WHERE id = %s",
                    (str(host_db_id),),
                )
                row = cur.fetchone()
                # A missing row means cleanup already finished (idempotent).
                if row is None:
                    return ReleaseHostResponse(status="already_released").model_dump()
                (
                    leased_to_user,
                    status,
                    lima_instance_name,
                    lima_disk_name,
                    bare_metal_server_id,
                ) = row
                # Ownership check first: we don't want to leak a status
                # signal to other users via the response code.
                if leased_to_user != user.user_id_prefix:
                    raise HTTPException(status_code=403, detail="You do not own this host lease")
                # Only a leased or already-removing row is eligible for
                # cleanup; anything else is treated as already released.
                if status not in ("leased", "removing"):
                    return ReleaseHostResponse(status="already_released").model_dump()
                if status == "leased":
                    cur.execute(
                        "UPDATE pool_hosts SET status = 'removing', released_at = NOW() WHERE id = %s",
                        (str(host_db_id),),
                    )
                    conn.commit()
            # Past the commit point: the row is durably ``removing``. A teardown
            # failure below leaves the row ``removing`` and surfaces a 5xx so the
            # client retries.
            _finish_releasing_pool_host(
                conn,
                host_db_id,
                lima_instance_name,
                lima_disk_name,
                bare_metal_server_id,
            )
        finally:
            conn.close()
        return ReleaseHostResponse(status="released").model_dump()


def _finish_releasing_pool_host(
    conn: Any,
    host_db_id: Any,
    lima_instance_name: str | None,
    lima_disk_name: str | None,
    bare_metal_server_id: Any,
) -> None:
    """Destroy a slice's lima VM (host already marked ``removing``), then delete the row.

    **Raises** on any failure rather than swallowing it -- the caller has already
    committed the row to ``removing`` (a durable, retryable in-progress marker), so
    a failure here propagates to the HTTP layer: the release reports failure, the
    row stays ``removing``, and the client retries. A release that cannot actually
    destroy the slice VM must never report success.
    """
    clean_up_slice_on_box(conn, host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name)
    _delete_pool_host_row(conn, host_db_id)


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
        user = authenticate_request(request)
        conn = db.get_pool_db_connection()
        try:
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
        finally:
            conn.close()
        return RenameHostResponse(host_db_id=host_db_id, host_name=body.host_name).model_dump()


@router.get("/hosts")
def list_leased_hosts(request: Request) -> list[dict[str, object]]:
    """List all hosts currently leased by the authenticated user."""
    with handle_endpoint_errors():
        user = authenticate_request(request)
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, vps_address, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, "
                    "host_name, attributes, leased_at, outer_host_public_key, container_host_public_key "
                    "FROM pool_hosts "
                    "WHERE status = 'leased' AND leased_to_user = %s",
                    (user.user_id_prefix,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
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
    """Count the user's current pool-host leases (the remote-workspace usage number)."""
    conn = db.get_pool_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_COUNT_LEASED_HOSTS_SQL, (user_id_prefix,))
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0
