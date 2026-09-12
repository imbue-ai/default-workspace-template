from uuid import uuid4

from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.cutover_db import _FINISH_RESTORE_POOL_HOST_SQL
from imbue.minds_admin.slices.cutover_db import _PARK_POOL_HOST_SQL
from imbue.minds_admin.slices.cutover_db import _ROLLBACK_PARK_POOL_HOST_SQL
from imbue.minds_admin.slices.cutover_db import _ROLLBACK_RESTORE_ARTIFACT_SQL
from imbue.minds_admin.slices.cutover_db import _pool_row_from_tuple
from imbue.minds_admin.slices.cutover_db import fetch_unplaced_gen1_pool_rows
from imbue.minds_admin.slices.cutover_db import finish_restore_pool_host
from imbue.minds_admin.slices.cutover_db import park_pool_host
from imbue.minds_admin.slices.cutover_db import rollback_park_pool_host
from imbue.minds_admin.slices.cutover_db import rollback_restore_artifact
from imbue.minds_admin.slices.testing import RecordingConnection


def test_park_cas_clears_placement_artifact_and_transitions_from_stopped_gen1_only() -> None:
    assert "status = 'stopped'" in _PARK_POOL_HOST_SQL
    for cleared in (
        "vps_address = NULL",
        "ssh_port = NULL",
        "container_ssh_port = NULL",
        "bare_metal_server_id = NULL",
        "transition_heartbeat_at = NULL",
        "transition_id = NULL",
        "artifact_manifest = NULL",
        "wrapped_dek = NULL",
        "transition_error = NULL",
    ):
        assert cleared in _PARK_POOL_HOST_SQL
    # Only 'stopped' matches: the product stop precedes every park, so any
    # other status (a user start racing the migrate) must refuse.
    assert "WHERE id = %s AND status = 'stopped' AND box_generation < 2" in _PARK_POOL_HOST_SQL
    conn = RecordingConnection([], rowcount=1)
    row_id = str(uuid4())
    assert park_pool_host(conn, row_id) is True
    assert conn.recording_cursor.executed == [(_PARK_POOL_HOST_SQL, (row_id,))]
    assert conn.commit_count == 1
    assert park_pool_host(RecordingConnection([], rowcount=0), row_id) is False


def test_finish_restore_cas_requires_a_parked_row_at_the_stamped_disk_size() -> None:
    assert "status = 'leased'" in _FINISH_RESTORE_POOL_HOST_SQL
    assert "WHERE id = %s AND status = 'stopped' AND bare_metal_server_id IS NULL AND disk_gb = %s" in (
        _FINISH_RESTORE_POOL_HOST_SQL
    )
    # The identity columns are never rewritten by the restore.
    for untouched in ("attributes", "agent_id", "host_id", "host_name", "slice_instance_name"):
        assert f"{untouched} =" not in _FINISH_RESTORE_POOL_HOST_SQL
    # The host-key columns ARE rewritten, with the harvested keys the replay
    # put on the new endpoints (see the SQL's comment for why).
    assert "outer_host_public_key = %s, container_host_public_key = %s" in _FINISH_RESTORE_POOL_HOST_SQL
    conn = RecordingConnection([], rowcount=1)
    row_id = str(uuid4())
    server_id = str(uuid4())
    assert (
        finish_restore_pool_host(
            conn,
            row_id,
            vps_address="15.204.1.2",
            vm_ssh_port=22010,
            container_ssh_port=22011,
            server_id=server_id,
            box_generation=2,
            memory_units=8,
            outer_host_public_key="ssh-ed25519 AAAAVM vm-host",
            container_host_public_key="ssh-ed25519 AAAAC container",
            disk_gb=44,
        )
        is True
    )
    (executed,) = conn.recording_cursor.executed
    assert executed == (
        _FINISH_RESTORE_POOL_HOST_SQL,
        (
            "15.204.1.2",
            22010,
            22011,
            server_id,
            2,
            8,
            "ssh-ed25519 AAAAVM vm-host",
            "ssh-ed25519 AAAAC container",
            row_id,
            44,
        ),
    )


def test_fetch_unplaced_gen1_pool_rows_selects_gen1_rows_on_no_box() -> None:
    # A finalized stop: gen-1, stopped, placement and box link cleared.
    row_id = str(uuid4())
    finalized = (
        row_id,
        "stopped",
        "host-" + "a" * 32,
        f"agent-{uuid4().hex}",
        "slice-a",
        "0123456789abcdef",
        None,
        None,
        None,
        None,
        "mngr-slice-x-" + "a" * 16,
        "mngr-slice-x-" + "a" * 16 + "-data",
        None,
        None,
        1,
        8,
        44,
        {"repo_branch_or_tag": "minds-v0.4.2"},
        2,
        "US-WEST-OR",
        {"generation": 2, "key_prefix": "dev-x/host-a/gen-2"},
        "d2VkZWs=",
    )
    conn = RecordingConnection([finalized], rowcount=0)
    rows = fetch_unplaced_gen1_pool_rows(conn)
    assert [row.id for row in rows] == [row_id]
    assert rows[0].bare_metal_server_id is None
    assert rows[0].status == "stopped"
    assert rows[0].artifact_manifest == {"generation": 2, "key_prefix": "dev-x/host-a/gen-2"}
    assert rows[0].wrapped_dek == "d2VkZWs="
    (executed,) = conn.recording_cursor.executed
    assert "WHERE bare_metal_server_id IS NULL AND box_generation < 2" in executed[0]
    assert executed[1] == ()


def test_pool_row_from_tuple_tolerates_null_optional_columns() -> None:
    row_id = str(uuid4())
    row = _pool_row_from_tuple(
        (
            row_id,
            "stopped",
            "host-" + "a" * 32,
            None,
            "slice-a",
            None,
            None,
            None,
            None,
            None,
            "mngr-slice-x-" + "a" * 16,
            "mngr-slice-x-" + "a" * 16 + "-data",
            None,
            None,
            None,
            8,
            44,
            {"repo_branch_or_tag": "minds-v0.4.2"},
            None,
            "US-WEST-OR",
            None,
            None,
        )
    )
    assert row.id == row_id
    assert row.agent_id is None
    assert row.bare_metal_server_id is None
    assert row.box_generation == 1
    assert row.artifact_generation == 0
    assert row.artifact_manifest is None
    assert row.wrapped_dek is None
    assert row.attributes == {"repo_branch_or_tag": "minds-v0.4.2"}
    assert row.baked_version == "minds-v0.4.2"
    assert row.model_copy_update(to_update(row.field_ref().attributes, {})).baked_version is None


def test_rollback_park_cas_returns_the_row_to_parked_gen1_at_the_default_size() -> None:
    assert "box_generation = 1" in _ROLLBACK_PARK_POOL_HOST_SQL
    assert "memory_units = %s" in _ROLLBACK_PARK_POOL_HOST_SQL
    for cleared in ("vps_address = NULL", "bare_metal_server_id = NULL", "artifact_manifest = NULL"):
        assert cleared in _ROLLBACK_PARK_POOL_HOST_SQL
    # A completed migration (leased on gen-2) and a mid-migration parked row
    # both match; a leased gen-1 row never does (the read-only "already back"
    # branch owns that state, so a leased-gen-1 CAS hit is a racing user start
    # that must refuse rather than rug-pull the running workspace).
    assert (
        "WHERE id = %s AND (status = 'stopped' OR (status = 'leased' AND box_generation >= 2))"
        in _ROLLBACK_PARK_POOL_HOST_SQL
    )
    conn = RecordingConnection([], rowcount=1)
    row_id = str(uuid4())
    assert rollback_park_pool_host(conn, row_id, memory_units=8) is True
    assert conn.recording_cursor.executed == [(_ROLLBACK_PARK_POOL_HOST_SQL, (8, row_id))]
    assert conn.commit_count == 1
    assert rollback_park_pool_host(RecordingConnection([], rowcount=0), row_id, memory_units=8) is False


def test_rollback_artifact_cas_writes_the_saved_pointers_onto_a_parked_gen1_row_only() -> None:
    # Writing the pointers makes the row an ordinary finalized-stopped gen-1
    # row; the guard keeps a concurrent flip from racing it onto the wrong shape.
    assert "artifact_manifest = %s::jsonb" in _ROLLBACK_RESTORE_ARTIFACT_SQL
    assert (
        "WHERE id = %s AND status = 'stopped' AND bare_metal_server_id IS NULL AND box_generation < 2"
        in _ROLLBACK_RESTORE_ARTIFACT_SQL
    )
    # A None key leaves that column alone.
    assert "outer_host_public_key = COALESCE(%s, outer_host_public_key)" in _ROLLBACK_RESTORE_ARTIFACT_SQL
    assert "container_host_public_key = COALESCE(%s, container_host_public_key)" in _ROLLBACK_RESTORE_ARTIFACT_SQL
    conn = RecordingConnection([], rowcount=1)
    row_id = str(uuid4())
    assert (
        rollback_restore_artifact(
            conn,
            row_id,
            artifact_manifest_json='{"generation": 2}',
            wrapped_dek="d2VkZWs=",
            artifact_generation=2,
            outer_host_public_key="ssh-ed25519 AAAAvm",
            container_host_public_key=None,
        )
        is True
    )
    (executed,) = conn.recording_cursor.executed
    assert executed == (
        _ROLLBACK_RESTORE_ARTIFACT_SQL,
        ('{"generation": 2}', "d2VkZWs=", 2, "ssh-ed25519 AAAAvm", None, row_id),
    )
