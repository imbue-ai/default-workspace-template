from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import BareMetalServerCapacity
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyTarget
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.slices.bare_metal import ORPHAN_SLICE_MIN_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_capacity
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB

# Wire / DB values for pool_hosts.status. Rows are inserted 'available', flipped to
# 'leased' by the connector's /hosts/lease, and to 'removing' (the durable, retryable
# in-progress teardown marker) by both the connector's release path and the admin
# destroy's atomic claim. 'unreachable' is the connector's lease-time quarantine (a
# row whose SSH key injection failed): inert to leasing and quota queries, claimable
# by the admin destroy so drained dead boxes clean up normally. 'released' is a
# legacy value nothing writes anymore (release deletes the row); it stays claimable
# so historical rows can still be destroyed.
POOL_HOST_STATUS_AVAILABLE: Final[str] = "available"
# A slice whose bake is in flight: the row is inserted before the carve so the
# orphan reap sees the VM as tracked, and flipped to available when the bake
# finishes (or deleted when it fails). Never leasable.
POOL_HOST_STATUS_BAKING: Final[str] = "baking"
POOL_HOST_STATUS_LEASED: Final[str] = "leased"
POOL_HOST_STATUS_REMOVING: Final[str] = "removing"
POOL_HOST_STATUS_RELEASED: Final[str] = "released"
POOL_HOST_STATUS_UNREACHABLE: Final[str] = "unreachable"

# Admin tooling writes bare_metal_servers + slice pool_hosts rows directly to the
# connector's host_pool Neon DB (laptop-side), mirroring how `minds-admin pool create`
# writes VPS pool_hosts rows. The connector only reads these (plus its release
# writes). Keep the column lists in sync with migrations 008 / 009 / 031 / 041.
#
# CLEANUP: stop dual-writing the legacy lima_service_user / lima_instance_name /
# lima_disk_name columns and drop the COALESCE fallbacks to them (throughout this
# module) once every tier's pool DB has applied migration 041 and no pre-rename
# checkout is in use (then drop the columns in a follow-up migration).

_INSERT_BARE_METAL_SERVER_SQL: Final[str] = (
    "INSERT INTO bare_metal_servers "
    "(id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, slice_service_user, lima_service_user, status, box_generation, uplink_mbps, "
    "created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())"
)

# A slice is an ordinary pool_hosts row plus the instance/disk names the connector
# needs to tear it down. ssh_port / container_ssh_port are the box-forwarded ports (not
# the default 22 / 2222), so they are params.
_INSERT_SLICE_POOL_HOST_SQL: Final[str] = (
    "INSERT INTO pool_hosts "
    "(id, vps_address, vps_instance_id, agent_id, host_id, host_name, ssh_port, ssh_user, "
    "container_ssh_port, status, attributes, region, bare_metal_server_id, "
    "slice_instance_name, slice_disk_name, lima_instance_name, lima_disk_name, "
    "outer_host_public_key, container_host_public_key, "
    "box_generation, memory_units, disk_gb, created_at) "
    f"VALUES (%s, %s, %s, %s, %s, %s, %s, 'root', %s, '{POOL_HOST_STATUS_AVAILABLE}', %s::jsonb, %s, "
    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())"
)

# The in-flight form of a slice row (see POOL_HOST_STATUS_BAKING): identity and
# sizing are known before the carve; the agent id, forwarded ports and sshd host
# keys arrive with the bake result (_FINISH_BAKING_SLICE_POOL_HOST_SQL).
_INSERT_BAKING_SLICE_POOL_HOST_SQL: Final[str] = (
    "INSERT INTO pool_hosts "
    "(id, vps_address, vps_instance_id, agent_id, host_id, host_name, ssh_port, ssh_user, "
    "container_ssh_port, status, attributes, region, bare_metal_server_id, "
    "slice_instance_name, slice_disk_name, lima_instance_name, lima_disk_name, "
    "box_generation, memory_units, disk_gb, created_at) "
    f"VALUES (%s, %s, %s, NULL, %s, %s, NULL, 'root', NULL, '{POOL_HOST_STATUS_BAKING}', %s::jsonb, %s, "
    "%s, %s, %s, %s, %s, %s, %s, %s, NOW())"
)

_FINISH_BAKING_SLICE_POOL_HOST_SQL: Final[str] = (
    "UPDATE pool_hosts SET agent_id = %s, ssh_port = %s, container_ssh_port = %s, "
    "outer_host_public_key = %s, container_host_public_key = %s, "
    f"status = '{POOL_HOST_STATUS_AVAILABLE}' WHERE id = %s AND status = '{POOL_HOST_STATUS_BAKING}'"
)

_DELETE_BAKING_SLICE_POOL_HOST_SQL: Final[str] = (
    f"DELETE FROM pool_hosts WHERE id = %s AND status = '{POOL_HOST_STATUS_BAKING}'"
)

# Id-preserving copy of a box row between host_pool DBs (the CI standing boxes
# live canonically in the CI infra DB and are imported into each per-run ci
# env's DB so its connector can SSH them at lease/release time -- see
# specs/remote-workspaces-in-ci.md). Unlike the plain insert above this carries
# box_host_public_key (normally recorded later via update_server), because an
# imported row must arrive lease-ready with its pinned host key intact, and
# wireguard_address / wireguard_public_key (assigned at prep), so the copy is faithful to
# the source row.
_UPSERT_BARE_METAL_SERVER_SQL: Final[str] = (
    "INSERT INTO bare_metal_servers "
    "(id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, slice_service_user, lima_service_user, status, box_generation, uplink_mbps, "
    # CLEANUP: stop writing the legacy wg_address / wg_public_key columns once
    # every tier's pool DB has applied migration 037 and no pre-rename checkout
    # is in use (then drop the columns in a follow-up migration).
    "box_host_public_key, wireguard_address, wireguard_public_key, wg_address, wg_public_key, "
    "created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "NOW(), NOW()) "
    "ON CONFLICT (id) DO UPDATE SET "
    "ovh_order_id = EXCLUDED.ovh_order_id, ovh_service_name = EXCLUDED.ovh_service_name, "
    "plan_code = EXCLUDED.plan_code, region = EXCLUDED.region, public_address = EXCLUDED.public_address, "
    "cpu_cores = EXCLUDED.cpu_cores, cpu_threads = EXCLUDED.cpu_threads, ram_gb = EXCLUDED.ram_gb, "
    "disk_gb = EXCLUDED.disk_gb, memory_per_slice_gb = EXCLUDED.memory_per_slice_gb, "
    "cpu_overcommit_ratio = EXCLUDED.cpu_overcommit_ratio, slot_count = EXCLUDED.slot_count, "
    "raid_level = EXCLUDED.raid_level, slice_service_user = EXCLUDED.slice_service_user, "
    "lima_service_user = EXCLUDED.lima_service_user, "
    "status = EXCLUDED.status, box_generation = EXCLUDED.box_generation, uplink_mbps = EXCLUDED.uplink_mbps, "
    "box_host_public_key = EXCLUDED.box_host_public_key, wireguard_address = EXCLUDED.wireguard_address, "
    "wireguard_public_key = EXCLUDED.wireguard_public_key, wg_address = EXCLUDED.wg_address, "
    "wg_public_key = EXCLUDED.wg_public_key, updated_at = NOW()"
)

# Column order is what _server_from_row indexes into.
_SERVER_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "ovh_order_id",
    "ovh_service_name",
    "plan_code",
    "region",
    "public_address",
    "cpu_cores",
    "cpu_threads",
    "ram_gb",
    "disk_gb",
    "memory_per_slice_gb",
    "cpu_overcommit_ratio",
    "slot_count",
    "raid_level",
    "lima_service_user",
    "status",
    "created_at",
    "updated_at",
    "box_host_public_key",
)
_SELECT_SERVERS_SQL: Final[str] = (
<<<<<<< HEAD
    "SELECT id, ovh_order_id, ovh_service_name, plan_code, region, public_address, "
    "cpu_cores, cpu_threads, ram_gb, disk_gb, memory_per_slice_gb, cpu_overcommit_ratio, "
    "slot_count, raid_level, COALESCE(slice_service_user, lima_service_user), status, "
    # CLEANUP: drop the COALESCE fallbacks to the legacy wg_address /
    # wg_public_key columns once every tier's pool DB has applied migration 037
    # and no pre-rename checkout writes them anymore.
    "created_at, updated_at, box_host_public_key, box_generation, uplink_mbps, "
    "COALESCE(wireguard_address, wg_address), COALESCE(wireguard_public_key, wg_public_key) "
    "FROM bare_metal_servers ORDER BY created_at ASC"
=======
    f"SELECT {', '.join(_SERVER_COLUMNS)} FROM bare_metal_servers ORDER BY created_at ASC"
>>>>>>> origin/main
)

# Count the baked slices currently on a server. Every row -- including 'removing'
# ones, whose VM teardown may still be in flight -- occupies its box slot until the
# VM is destroyed and the row deleted, so a server's free slots = slot_count - this
# count stays truthful while destroys run.
_COUNT_SLICES_SQL: Final[str] = "SELECT COUNT(*) FROM pool_hosts WHERE bare_metal_server_id = %s"

# The DB-visible machine-unit and disk usage on a server (specs/slice-fleet):
# every row counts (a 'removing' row's VM still holds its budget share until
# destroyed). The disk figure is each machine's budget footprint -- boot disk
# plus data disk -- so it is comparable to the box's disk budget the way the
# on-box reserve guard sums it.
_SUM_MACHINE_USAGE_SQL: Final[str] = (
    "SELECT COALESCE(SUM(memory_units), 0), COALESCE(SUM(%s + disk_gb), 0) "
    "FROM pool_hosts WHERE bare_metal_server_id = %s"
)

# Every slice's instance name on a server (any status). Used to reconcile the
# box's running VMs against the DB and reap orphans (VMs with no row).
_SELECT_SLICE_INSTANCE_NAMES_SQL: Final[str] = (
    "SELECT COALESCE(slice_instance_name, lima_instance_name) FROM pool_hosts WHERE bare_metal_server_id = %s "
    "AND COALESCE(slice_instance_name, lima_instance_name) IS NOT NULL"
)

# Sibling of the instance-name query, for reconciling the box's slice data disks
# against the DB and reaping orphan disks (disks with no row).
_SELECT_SLICE_DISK_NAMES_SQL: Final[str] = (
    "SELECT COALESCE(slice_disk_name, lima_disk_name) FROM pool_hosts WHERE bare_metal_server_id = %s "
    "AND COALESCE(slice_disk_name, lima_disk_name) IS NOT NULL"
)


@pure
def build_bare_metal_server_insert_values(server: BareMetalServer) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_INSERT_BARE_METAL_SERVER_SQL` from a server."""
    return (
        str(server.id),
        server.ovh_order_id,
        server.ovh_service_name,
        server.plan_code,
        server.region,
        server.public_address,
        server.cpu_cores,
        server.cpu_threads,
        server.ram_gb,
        server.disk_gb,
        server.memory_per_slice_gb,
        server.cpu_overcommit_ratio,
        server.slot_count,
        server.raid_level,
        server.slice_service_user,
        # The legacy lima_service_user column gets the same value (the dual write).
        server.slice_service_user,
        str(server.status),
        server.box_generation,
        server.uplink_mbps,
    )


@pure
def build_slice_pool_host_insert_values(
    *,
    row_id: str,
    box_public_address: str,
    agent_id: str,
    host_id: str,
    host_name: str,
    vm_ssh_host_port: int,
    container_ssh_host_port: int,
    attributes_json: str,
    region: str,
    bare_metal_server_id: str,
    slice_instance_name: str,
    slice_disk_name: str,
    # Baked sshd host public keys (deterministic, from `mngr create --format json`):
    # the VM-root key and the inner container key, persisted so leasing pins them.
    outer_host_public_key: str,
    container_host_public_key: str,
    # The owning box's slice-fleet generation, stamped so the connector's
    # box-side operations dispatch per row without a join.
    box_generation: int,
    # The machine's size (specs/slice-fleet): units (1 unit = 1GiB guest RAM)
    # and its data-disk GB (for a gen-1 row, the size its disk has after the
    # gen-2 cutover; see ``compute_gen1_migrated_data_disk_gib``).
    memory_units: int,
    disk_gb: int,
) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_INSERT_SLICE_POOL_HOST_SQL`.

    ``vps_address`` is the box's public address and ``vps_instance_id`` is set to
    the slice instance name (the column is NOT NULL and slice teardown keys on the
    instance/disk names, not on an OVH service name). ``ssh_port`` / ``container_ssh_port``
    are the box-forwarded ports for the VM's root sshd and the inner container sshd.
    """
    return (
        row_id,
        box_public_address,
        # vps_instance_id: non-null placeholder; slices are torn down via the instance/disk names.
        slice_instance_name,
        agent_id,
        host_id,
        host_name,
        vm_ssh_host_port,
        container_ssh_host_port,
        attributes_json,
        region,
        bare_metal_server_id,
        slice_instance_name,
        slice_disk_name,
        # The legacy lima_instance_name / lima_disk_name columns get the same values (the dual write).
        slice_instance_name,
        slice_disk_name,
        outer_host_public_key,
        container_host_public_key,
        box_generation,
        memory_units,
        disk_gb,
    )


@pure
def _as_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.now(timezone.utc)


@pure
def _server_from_row(row: tuple[Any, ...]) -> BareMetalServer:
    return BareMetalServer(
        id=BareMetalServerDbId(str(row[0])),
        ovh_order_id=row[1],
        ovh_service_name=row[2],
        plan_code=str(row[3]),
        region=str(row[4]),
        public_address=row[5],
        cpu_cores=row[6],
        cpu_threads=row[7],
        ram_gb=row[8],
        disk_gb=row[9],
        memory_per_slice_gb=row[10],
        cpu_overcommit_ratio=float(row[11]) if row[11] is not None else None,
        slot_count=int(row[12]) if row[12] is not None else 0,
        raid_level=row[13],
        slice_service_user=row[14],
        status=BareMetalServerStatus(str(row[15])),
        created_at=_as_datetime(row[16]),
        updated_at=_as_datetime(row[17]),
        box_host_public_key=row[18],
        box_generation=int(row[19]) if row[19] is not None else 1,
        uplink_mbps=int(row[20]),
        wireguard_address=row[21],
        wireguard_public_key=row[22],
    )


def insert_bare_metal_server(conn: Any, server: BareMetalServer) -> None:
    """Insert a new bare_metal_servers row."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_BARE_METAL_SERVER_SQL, build_bare_metal_server_insert_values(server))
    conn.commit()


@pure
def build_bare_metal_server_upsert_values(server: BareMetalServer) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_UPSERT_BARE_METAL_SERVER_SQL` from a server."""
    return build_bare_metal_server_insert_values(server) + (
        server.box_host_public_key,
        server.wireguard_address,
        server.wireguard_public_key,
        # The legacy wg_address / wg_public_key columns get the same values
        # (the upsert's dual write; see the CLEANUP on the SQL above).
        server.wireguard_address,
        server.wireguard_public_key,
    )


def upsert_bare_metal_server(conn: Any, server: BareMetalServer) -> None:
    """Insert-or-update a bare_metal_servers row by id, preserving the source row's identity."""
    with conn.cursor() as cur:
        cur.execute(_UPSERT_BARE_METAL_SERVER_SQL, build_bare_metal_server_upsert_values(server))
    conn.commit()


def update_server(conn: Any, server_id: BareMetalServerDbId, **fields: Any) -> None:
    """Update the named columns of a bare_metal_servers row (always bumps updated_at)."""
    if not fields:
        return
    assignments = ", ".join(f"{column} = %s" for column in fields)
    params = [*fields.values(), str(server_id)]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE bare_metal_servers SET {assignments}, updated_at = NOW() WHERE id = %s",
            tuple(params),
        )
    conn.commit()


def fetch_servers(conn: Any) -> list[BareMetalServer]:
    """Return all bare_metal_servers rows, oldest first."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SERVERS_SQL)
        rows = cur.fetchall()
    return [_server_from_row(row) for row in rows]


def fetch_server_by_id(conn: Any, server_id: BareMetalServerDbId) -> BareMetalServer | None:
    """Return a single bare_metal_servers row by id, or None if it does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SERVERS_SQL.replace("ORDER BY created_at ASC", "WHERE id = %s"), (str(server_id),))
        row = cur.fetchone()
    return _server_from_row(row) if row else None


class SlicePoolHostRow(FrozenModel):
    """A slice pool_hosts row joined with the box it lives on (the coordinates a per-VM repair needs)."""

    host_id: str = Field(description="pool_hosts.host_id")
    host_name: str = Field(description="pool_hosts.host_name")
    status: str = Field(description="pool_hosts.status")
    lima_instance_name: str = Field(description="The slice's lima instance name on its box")
    server: BareMetalServer = Field(description="The bare_metal_servers row of the slice's box")


# The projection _slice_pool_host_row indexes into: the four pool_hosts columns,
# then the box's _SERVER_COLUMNS.
_SELECT_SLICE_HOSTS_SQL_PREFIX: Final[str] = (
    "SELECT p.host_id, p.host_name, p.status, p.lima_instance_name, "
    + ", ".join(f"s.{column}" for column in _SERVER_COLUMNS)
    + " FROM pool_hosts p JOIN bare_metal_servers s ON p.bare_metal_server_id = s.id"
)
_SELECT_SLICE_HOSTS_BY_HOST_ID_SQL: Final[str] = (
    f"{_SELECT_SLICE_HOSTS_SQL_PREFIX} WHERE p.host_id = ANY(%s) AND p.lima_instance_name IS NOT NULL"
)
_SELECT_LEASED_SLICE_HOSTS_SQL: Final[str] = (
    f"{_SELECT_SLICE_HOSTS_SQL_PREFIX} WHERE p.status = %s AND p.lima_instance_name IS NOT NULL "
    "ORDER BY p.leased_at ASC"
)


@pure
def _slice_pool_host_row(row: tuple[Any, ...]) -> SlicePoolHostRow:
    return SlicePoolHostRow(
        host_id=str(row[0]),
        host_name=str(row[1]),
        status=str(row[2]),
        lima_instance_name=str(row[3]),
        server=_server_from_row(tuple(row[4:])),
    )


def fetch_slice_hosts_by_host_id(conn: Any, host_ids: Sequence[str]) -> list[SlicePoolHostRow]:
    """Return the slice pool_hosts rows (with their boxes) for ``host_ids``; VPS rows and unknown ids are absent."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SLICE_HOSTS_BY_HOST_ID_SQL, (list(host_ids),))
        rows = cur.fetchall()
    return [_slice_pool_host_row(tuple(row)) for row in rows]


def fetch_leased_slice_hosts(conn: Any) -> list[SlicePoolHostRow]:
    """Return every leased slice pool_hosts row (with its box), oldest lease first."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_LEASED_SLICE_HOSTS_SQL, (POOL_HOST_STATUS_LEASED,))
        rows = cur.fetchall()
    return [_slice_pool_host_row(tuple(row)) for row in rows]


def count_slices_on_server(conn: Any, server_id: BareMetalServerDbId) -> int:
    """Count the baked (non-removing) slices currently on a server."""
    with conn.cursor() as cur:
        cur.execute(_COUNT_SLICES_SQL, (str(server_id),))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def fetch_machine_usage_on_server(conn: Any, server_id: BareMetalServerDbId) -> tuple[int, int]:
    """The (units, boot+data disk GB) the server's pool rows consume."""
    with conn.cursor() as cur:
        cur.execute(_SUM_MACHINE_USAGE_SQL, (GEN2_BOOT_DISK_GIB, str(server_id)))
        row = cur.fetchone()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])


def fetch_server_capacities(conn: Any) -> list[BareMetalServerCapacity]:
    """Return every server paired with its slice-slot accounting (used / free)."""
    return [compute_capacity(server, count_slices_on_server(conn, server.id)) for server in fetch_servers(conn)]


def fetch_slice_instance_names_for_server(conn: Any, server_id: BareMetalServerDbId) -> set[str]:
    """Return the slice_instance_name of every slice pool_hosts row for ``server_id`` (any status)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SLICE_INSTANCE_NAMES_SQL, (str(server_id),))
        return {row[0] for row in cur.fetchall() if row[0]}


def fetch_slice_disk_names_for_server(conn: Any, server_id: BareMetalServerDbId) -> set[str]:
    """Return the slice_disk_name of every slice pool_hosts row for ``server_id`` (any status)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SLICE_DISK_NAMES_SQL, (str(server_id),))
        return {row[0] for row in cur.fetchall() if row[0]}


def insert_slice_pool_host(conn: Any, values: tuple[Any, ...]) -> None:
    """Insert a slice pool_hosts row (values from build_slice_pool_host_insert_values)."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_SLICE_POOL_HOST_SQL, values)
    conn.commit()


@pure
def build_baking_slice_pool_host_insert_values(
    *,
    row_id: str,
    box_public_address: str,
    host_id: str,
    host_name: str,
    attributes_json: str,
    region: str,
    bare_metal_server_id: str,
    slice_instance_name: str,
    slice_disk_name: str,
    box_generation: int,
    memory_units: int,
    disk_gb: int,
) -> tuple[Any, ...]:
    """Build the value tuple for :data:`_INSERT_BAKING_SLICE_POOL_HOST_SQL` (the pre-carve row)."""
    return (
        row_id,
        box_public_address,
        slice_instance_name,
        host_id,
        host_name,
        attributes_json,
        region,
        bare_metal_server_id,
        slice_instance_name,
        slice_disk_name,
        # The legacy lima_instance_name / lima_disk_name columns get the same values (the dual write).
        slice_instance_name,
        slice_disk_name,
        box_generation,
        memory_units,
        disk_gb,
    )


def insert_baking_slice_pool_host(conn: Any, values: tuple[Any, ...]) -> None:
    """Insert a slice's ``baking`` row before its carve (values from build_baking_slice_pool_host_insert_values)."""
    with conn.cursor() as cur:
        cur.execute(_INSERT_BAKING_SLICE_POOL_HOST_SQL, values)
    conn.commit()


def finish_baking_slice_pool_host(
    conn: Any,
    row_id: str,
    *,
    agent_id: str,
    vm_ssh_host_port: int,
    container_ssh_host_port: int,
    outer_host_public_key: str,
    container_host_public_key: str,
) -> bool:
    """Record the bake result on a ``baking`` row and make it available; False if the row is no longer baking.

    False means an operator destroy claimed the row mid-bake (or it is gone): the
    caller must then treat its freshly baked VM as unwanted and roll it back.
    """
    with conn.cursor() as cur:
        cur.execute(
            _FINISH_BAKING_SLICE_POOL_HOST_SQL,
            (
                agent_id,
                vm_ssh_host_port,
                container_ssh_host_port,
                outer_host_public_key,
                container_host_public_key,
                row_id,
            ),
        )
        is_finished = cur.rowcount == 1
    conn.commit()
    return is_finished


def delete_baking_slice_pool_host(conn: Any, row_id: str) -> bool:
    """Drop a ``baking`` row whose bake failed; False if it was no longer baking (a destroy claimed it)."""
    with conn.cursor() as cur:
        cur.execute(_DELETE_BAKING_SLICE_POOL_HOST_SQL, (row_id,))
        is_deleted = cur.rowcount == 1
    conn.commit()
    return is_deleted


# Unleased slice row ids -- the pool backlog that an env destroy must tear down so it
# does not leak VMs once the env's DB is gone. The status filter is the SAME
# claimable set the destroy uses (see destroy_eligible_pool_host_statuses), so a row
# this query selects is always claimable -- the two predicates cannot drift. Leased
# slices are deliberately excluded: they are torn down via their agent's release path
# (`mngr destroy` -> connector release), and tearing their VM down here would race
# that path. ``removing`` rows ARE included: a row stranded mid-teardown (a crashed
# release, or a prior destroy whose box was unreachable) would otherwise never be
# cleaned up -- both the VM destroy and the row delete are idempotent, so re-tearing
# one down is harmless even against an in-flight release.
_SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL: Final[str] = (
    "SELECT p.id "
    "FROM pool_hosts p JOIN bare_metal_servers s ON p.bare_metal_server_id = s.id "
    "WHERE p.status = ANY(%s) "
    "AND COALESCE(p.slice_instance_name, p.lima_instance_name) IS NOT NULL AND s.public_address IS NOT NULL"
)


def fetch_unleased_slice_teardown_row_ids(conn: Any, eligible_statuses: Sequence[str]) -> list[str]:
    """Return the row id of every claimable (unleased) slice whose box is still reachable in the DB."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL, (list(eligible_statuses),))
        return [str(row[0]) for row in cur.fetchall()]


# Atomically claim a row for teardown by flipping it to 'removing', but only from a
# caller-approved status set -- the WHERE makes the claim and the eligibility check a
# single statement, so it cannot race the connector's lease (which only ever selects
# 'available' rows, under FOR UPDATE): either the lease commits first and the claim
# matches nothing, or the claim commits first and the row is invisible to leasing.
# A 'baking' row is only claimable once its bake could not still be running: the
# age threshold is the reaper's (a bake's carve + build fit well inside it).
_CLAIM_POOL_HOST_FOR_REMOVAL_SQL: Final[str] = (
    f"UPDATE pool_hosts SET status = '{POOL_HOST_STATUS_REMOVING}' WHERE id = %s AND status = ANY(%s) "
    f"AND (status <> '{POOL_HOST_STATUS_BAKING}' OR created_at < NOW() - make_interval(secs => %s))"
)

_SELECT_POOL_HOST_STATUS_SQL: Final[str] = "SELECT status FROM pool_hosts WHERE id = %s"

# The claimed row's teardown coordinates. LEFT JOIN so a row whose box record was
# deleted still comes back (with null box columns) and can be reported precisely.
_SELECT_POOL_HOST_DESTROY_TARGET_SQL: Final[str] = (
    "SELECT COALESCE(p.slice_instance_name, p.lima_instance_name), s.public_address, "
    "COALESCE(s.slice_service_user, s.lima_service_user), s.box_host_public_key, p.box_generation, "
    # CLEANUP: drop the COALESCE fallbacks to the legacy wg_* columns once
    # every tier's pool DB has applied migration 037 (see _SELECT_SERVERS_SQL).
    "COALESCE(s.wireguard_address, s.wg_address), COALESCE(s.wireguard_public_key, s.wg_public_key) "
    "FROM pool_hosts p LEFT JOIN bare_metal_servers s ON p.bare_metal_server_id = s.id "
    "WHERE p.id = %s"
)


@pure
def destroy_eligible_pool_host_statuses(is_leased_destroy_allowed: bool) -> tuple[str, ...]:
    """The statuses an admin destroy may atomically claim.

    'removing' is always claimable so a destroy that failed mid-teardown can be
    retried by re-running with the same id; 'unreachable' (the connector's
    lease-time quarantine) is claimable so a dead box's rows drain without a
    manual status flip; 'baking' is claimable only once stale (older than a bake,
    see :func:`claim_pool_host_for_removal`), so a killed bake's leftover row
    drains while a live bake's row is left alone; 'leased' requires the explicit
    ``--force`` opt-in (it tears down a user's live workspace).
    """
    base = (
        POOL_HOST_STATUS_AVAILABLE,
        POOL_HOST_STATUS_BAKING,
        POOL_HOST_STATUS_RELEASED,
        POOL_HOST_STATUS_REMOVING,
        POOL_HOST_STATUS_UNREACHABLE,
    )
    if is_leased_destroy_allowed:
        return base + (POOL_HOST_STATUS_LEASED,)
    return base


def claim_pool_host_for_removal(conn: Any, row_id: str, eligible_statuses: Sequence[str]) -> bool:
    """Atomically flip a row to 'removing' (committing immediately); True if this call claimed it.

    False means the row is gone or in a non-eligible status (e.g. it was leased
    between the operator listing it and the destroy running).
    """
    with conn.cursor() as cur:
        cur.execute(
            _CLAIM_POOL_HOST_FOR_REMOVAL_SQL, (row_id, list(eligible_statuses), float(ORPHAN_SLICE_MIN_AGE_SECONDS))
        )
        is_claimed = cur.rowcount == 1
    conn.commit()
    return is_claimed


def fetch_pool_host_status(conn: Any, row_id: str) -> str | None:
    """Return a pool_hosts row's status, or None if the row does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_POOL_HOST_STATUS_SQL, (row_id,))
        row = cur.fetchone()
    return str(row[0]) if row else None


def fetch_pool_host_destroy_target(conn: Any, row_id: str) -> PoolHostDestroyTarget | None:
    """Return a row's teardown coordinates (box columns None when the box record is gone)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_POOL_HOST_DESTROY_TARGET_SQL, (row_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return PoolHostDestroyTarget(
        slice_instance_name=str(row[0]) if row[0] else None,
        box_public_address=str(row[1]) if row[1] else None,
        slice_service_user=str(row[2]) if row[2] else None,
        box_host_public_key=row[3],
        box_generation=int(row[4]) if row[4] is not None else 1,
        box_wireguard_address=str(row[5]) if row[5] else None,
        box_wireguard_public_key=str(row[6]) if row[6] else None,
    )


def delete_pool_host_row(conn: Any, row_id: str) -> None:
    """Delete a single pool_hosts row by id (committing immediately)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pool_hosts WHERE id = %s", (row_id,))
    conn.commit()


def fetch_pool_host_ids_on_server_by_status(
    conn: Any, server_id: BareMetalServerDbId, statuses: Sequence[str]
) -> list[str]:
    """Return the id of every pool_hosts row on one box whose status is in ``statuses``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM pool_hosts WHERE bare_metal_server_id = %s AND status = ANY(%s) ORDER BY created_at",
            (str(server_id), list(statuses)),
        )
        return [str(row[0]) for row in cur.fetchall()]
