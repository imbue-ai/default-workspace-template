"""The pool DB reads and the two guarded CAS writes the gen-1 -> gen-2 cutover makes.

Row transitions follow the connector's convention (``stop_start.py``): one
``UPDATE ... WHERE id = %s AND status = ...`` statement per transition, and the
caller checks ``rowcount == 1``. One-time tooling, deleted in phase 6 of
blueprint/slice-fleet-cutover.
"""

from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION

# CLEANUP: drop the COALESCE fallbacks to the legacy lima_instance_name /
# lima_disk_name columns once every tier's pool DB has applied migration 041
# and no pre-rename checkout writes them anymore.
_POOL_ROW_COLUMNS: Final[str] = (
    "id, status, host_id, agent_id, host_name, leased_to_user, vps_address, ssh_port, container_ssh_port, "
    "bare_metal_server_id, COALESCE(slice_instance_name, lima_instance_name), "
    "COALESCE(slice_disk_name, lima_disk_name), outer_host_public_key, "
    "container_host_public_key, box_generation, memory_units, disk_gb, attributes, artifact_generation, region, "
    "artifact_manifest, wrapped_dek"
)

# Park a stopped gen-1 row mid-migration: stopped with no placement, no box
# link, no artifact (the connector's start endpoint reads that combination as
# "migrating"); the transition fields are cleared so nothing re-drives it.
# Only 'stopped' matches: the product stop always precedes the park, so any
# other status means the row changed underneath the migrate (a user start)
# and must refuse rather than rug-pull a running workspace.
_PARK_POOL_HOST_SQL: Final[str] = (
    "UPDATE pool_hosts SET status = 'stopped', vps_address = NULL, ssh_port = NULL, container_ssh_port = NULL, "
    "bare_metal_server_id = NULL, transition_heartbeat_at = NULL, transition_id = NULL, "
    "artifact_manifest = NULL, wrapped_dek = NULL, stop_requested_at = NOW(), stopped_at = NOW(), "
    "transition_error = NULL "
    f"WHERE id = %s AND status = 'stopped' AND box_generation < {FIRST_QEMU_BOX_GENERATION}"
)

# Land a migrated row on leased at the target box's coordinates, on gen-2, at
# the default machine size. The disk_gb predicate asserts the row still
# carries the size the migration stamped (which is the size the transplanted
# disk was created at). The host-key columns are stamped with the HARVESTED
# keys: they are what the replay put on the new endpoints, and the row's
# recorded values can be stale (a client-side adopt rotates the on-disk keys
# without updating the row) -- record-sync clients pin the row's values for
# the new address, so serving the old ones would hard-reject their SSH.
_FINISH_RESTORE_POOL_HOST_SQL: Final[str] = (
    "UPDATE pool_hosts SET status = 'leased', vps_address = %s, ssh_port = %s, container_ssh_port = %s, "
    "bare_metal_server_id = %s, box_generation = %s, memory_units = %s, "
    "outer_host_public_key = %s, container_host_public_key = %s, transition_error = NULL, "
    "transition_failure_count = 0, stop_requested_at = NULL, stopped_at = NULL "
    "WHERE id = %s AND status = 'stopped' AND bare_metal_server_id IS NULL AND disk_gb = %s"
)

# A rollback's first flip: from leased-on-gen-2 (a completed migration) or the
# parked shape (a failed one) to parked-on-gen-1, so the connector's
# ``workspace_migrating`` guard covers the window while the gen-2 slice is
# destroyed. The machine size returns to the gen-1 default. A leased gen-1 row
# never matches: the rollback handles that state read-only ("already back"), so
# one reaching this CAS is a start that raced the pre-CAS fetch, and parking it
# would rug-pull the running workspace.
_ROLLBACK_PARK_POOL_HOST_SQL: Final[str] = (
    "UPDATE pool_hosts SET status = 'stopped', vps_address = NULL, ssh_port = NULL, container_ssh_port = NULL, "
    "bare_metal_server_id = NULL, transition_heartbeat_at = NULL, transition_id = NULL, "
    "artifact_manifest = NULL, wrapped_dek = NULL, stop_requested_at = NOW(), stopped_at = NOW(), "
    "transition_error = NULL, box_generation = 1, memory_units = %s "
    "WHERE id = %s AND (status = 'stopped' "
    f"OR (status = 'leased' AND box_generation >= {FIRST_QEMU_BOX_GENERATION}))"
)

# A rollback's second flip: write the saved product artifact pointers back so
# the row becomes an ordinary finalized-stopped gen-1 row, which the product's
# own gen-1 restore brings back on the next start.
_ROLLBACK_RESTORE_ARTIFACT_SQL: Final[str] = (
    "UPDATE pool_hosts SET artifact_manifest = %s::jsonb, wrapped_dek = %s, artifact_generation = %s "
    "WHERE id = %s AND status = 'stopped' AND bare_metal_server_id IS NULL "
    f"AND box_generation < {FIRST_QEMU_BOX_GENERATION}"
)


class CutoverPoolRow(FrozenModel):
    """The pool_hosts columns the cutover reads for one row."""

    id: str = Field(description="pool_hosts row id")
    status: str = Field(description="Lifecycle status")
    host_id: str = Field(description="mngr host id")
    agent_id: str | None = Field(description="The workspace's services agent id (None mid-bake)")
    host_name: str = Field(description="The row's host name")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix")
    vps_address: str | None = Field(description="Box public address (None once parked/finalized)")
    ssh_port: int | None = Field(description="VM-root forwarded port")
    container_ssh_port: int | None = Field(description="Container forwarded port")
    bare_metal_server_id: str | None = Field(description="Owning box row id (None once parked/finalized)")
    slice_instance_name: str | None = Field(description="The slice's instance name on the box")
    slice_disk_name: str | None = Field(description="The gen-1 data-disk name")
    outer_host_public_key: str | None = Field(
        description="The row's recorded VM host key (bake-time; re-stamped with the harvested key by migrate)"
    )
    container_host_public_key: str | None = Field(
        description="The row's recorded container host key (bake-time; re-stamped with the harvested key by migrate)"
    )
    box_generation: int = Field(description="The placement's slice-fleet generation")
    memory_units: int = Field(description="Machine size in units")
    disk_gb: int = Field(description="Data-disk size (for a gen-1 row: the post-cutover size)")
    attributes: dict[str, Any] = Field(description="Lease attributes (repo_branch_or_tag is the baked version)")
    artifact_generation: int = Field(description="Last uploaded artifact generation")
    region: str | None = Field(description="Lease-region label")
    artifact_manifest: dict[str, Any] | None = Field(
        description="The last product stop's artifact manifest (JSONB), when one exists"
    )
    wrapped_dek: str | None = Field(description="The KEK-wrapped age identity of the last product artifact")

    @property
    def baked_version(self) -> str | None:
        """The default-workspace-template version the row was baked at (``attributes.repo_branch_or_tag``)."""
        return str(self.attributes.get("repo_branch_or_tag") or "") or None


@pure
def _pool_row_from_tuple(row: tuple[Any, ...]) -> CutoverPoolRow:
    return CutoverPoolRow(
        id=str(row[0]),
        status=str(row[1]),
        host_id=str(row[2]),
        agent_id=str(row[3]) if row[3] is not None else None,
        host_name=str(row[4]),
        leased_to_user=row[5],
        vps_address=row[6],
        ssh_port=int(row[7]) if row[7] is not None else None,
        container_ssh_port=int(row[8]) if row[8] is not None else None,
        bare_metal_server_id=str(row[9]) if row[9] is not None else None,
        slice_instance_name=row[10],
        slice_disk_name=row[11],
        outer_host_public_key=row[12],
        container_host_public_key=row[13],
        box_generation=int(row[14]) if row[14] is not None else 1,
        memory_units=int(row[15]),
        disk_gb=int(row[16]),
        attributes=dict(row[17]) if isinstance(row[17], dict) else {},
        artifact_generation=int(row[18] or 0),
        region=row[19],
        artifact_manifest=dict(row[20]) if isinstance(row[20], dict) else None,
        wrapped_dek=row[21],
    )


def fetch_gen1_servers(conn: Any) -> list[BareMetalServer]:
    """Every box row still on generation 1 (the cutover's scope), oldest first."""
    return [server for server in fetch_servers(conn) if server.box_generation < FIRST_QEMU_BOX_GENERATION]


def fetch_pool_rows_on_server(conn: Any, server_id: BareMetalServerDbId) -> list[CutoverPoolRow]:
    """Every pool row placed on one box, any status, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_POOL_ROW_COLUMNS} FROM pool_hosts WHERE bare_metal_server_id = %s ORDER BY created_at",
            (str(server_id),),
        )
        rows = cur.fetchall()
    return [_pool_row_from_tuple(row) for row in rows]


def fetch_unplaced_gen1_pool_rows(conn: Any) -> list[CutoverPoolRow]:
    """Every gen-1 pool row on no box (a finalized stop, a parked row, a row mid-transition), oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_POOL_ROW_COLUMNS} FROM pool_hosts WHERE bare_metal_server_id IS NULL "
            f"AND box_generation < {FIRST_QEMU_BOX_GENERATION} ORDER BY created_at"
        )
        rows = cur.fetchall()
    return [_pool_row_from_tuple(row) for row in rows]


def fetch_pool_row(conn: Any, row_id: str) -> CutoverPoolRow | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_POOL_ROW_COLUMNS} FROM pool_hosts WHERE id = %s", (row_id,))
        row = cur.fetchone()
    return _pool_row_from_tuple(row) if row is not None else None


def fetch_gen1_pool_rows_for_user(conn: Any, user_id_prefix: str) -> list[CutoverPoolRow]:
    """Every gen-1 pool row leased to one user (any status), oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_POOL_ROW_COLUMNS} FROM pool_hosts WHERE leased_to_user = %s "
            f"AND box_generation < {FIRST_QEMU_BOX_GENERATION} ORDER BY created_at",
            (user_id_prefix,),
        )
        rows = cur.fetchall()
    return [_pool_row_from_tuple(row) for row in rows]


def park_pool_host(conn: Any, row_id: str) -> bool:
    """Park a stopped gen-1 row mid-migration (see ``_PARK_POOL_HOST_SQL``); True when this call flipped it."""
    with conn.cursor() as cur:
        cur.execute(_PARK_POOL_HOST_SQL, (row_id,))
        is_parked = cur.rowcount == 1
    conn.commit()
    return is_parked


def rollback_park_pool_host(conn: Any, row_id: str, *, memory_units: int) -> bool:
    """Park a migrated (or mid-migration) row back onto gen-1 (see ``_ROLLBACK_PARK_POOL_HOST_SQL``)."""
    with conn.cursor() as cur:
        cur.execute(_ROLLBACK_PARK_POOL_HOST_SQL, (memory_units, row_id))
        is_parked = cur.rowcount == 1
    conn.commit()
    return is_parked


def rollback_restore_artifact(
    conn: Any, row_id: str, *, artifact_manifest_json: str, wrapped_dek: str, artifact_generation: int
) -> bool:
    """Write the saved artifact pointers back onto a rollback-parked gen-1 row (see ``_ROLLBACK_RESTORE_ARTIFACT_SQL``)."""
    with conn.cursor() as cur:
        cur.execute(_ROLLBACK_RESTORE_ARTIFACT_SQL, (artifact_manifest_json, wrapped_dek, artifact_generation, row_id))
        is_restored = cur.rowcount == 1
    conn.commit()
    return is_restored


def finish_restore_pool_host(
    conn: Any,
    row_id: str,
    *,
    vps_address: str,
    vm_ssh_port: int,
    container_ssh_port: int,
    server_id: str,
    box_generation: int,
    memory_units: int,
    outer_host_public_key: str,
    container_host_public_key: str,
    disk_gb: int,
) -> bool:
    """Land a restored row on leased at its old coordinates (see ``_FINISH_RESTORE_POOL_HOST_SQL``)."""
    with conn.cursor() as cur:
        cur.execute(
            _FINISH_RESTORE_POOL_HOST_SQL,
            (
                vps_address,
                vm_ssh_port,
                container_ssh_port,
                server_id,
                box_generation,
                memory_units,
                outer_host_public_key,
                container_host_public_key,
                row_id,
                disk_gb,
            ),
        )
        is_finished = cur.rowcount == 1
    conn.commit()
    return is_finished
