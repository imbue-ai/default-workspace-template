from datetime import datetime
from datetime import timezone

from imbue.minds_admin.slices.bare_metal_db import _CLAIM_POOL_HOST_FOR_REMOVAL_SQL
from imbue.minds_admin.slices.bare_metal_db import _COUNT_SLICES_SQL
from imbue.minds_admin.slices.bare_metal_db import _FINISH_BAKING_SLICE_POOL_HOST_SQL
from imbue.minds_admin.slices.bare_metal_db import _INSERT_BAKING_SLICE_POOL_HOST_SQL
from imbue.minds_admin.slices.bare_metal_db import _INSERT_BARE_METAL_SERVER_SQL
from imbue.minds_admin.slices.bare_metal_db import _INSERT_SLICE_POOL_HOST_SQL
from imbue.minds_admin.slices.bare_metal_db import _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL
from imbue.minds_admin.slices.bare_metal_db import _UPSERT_BARE_METAL_SERVER_SQL
from imbue.minds_admin.slices.bare_metal_db import _server_from_row
from imbue.minds_admin.slices.bare_metal_db import build_baking_slice_pool_host_insert_values
from imbue.minds_admin.slices.bare_metal_db import build_bare_metal_server_insert_values
from imbue.minds_admin.slices.bare_metal_db import build_bare_metal_server_upsert_values
from imbue.minds_admin.slices.bare_metal_db import build_slice_pool_host_insert_values
from imbue.minds_admin.slices.bare_metal_db import claim_pool_host_for_removal
from imbue.minds_admin.slices.bare_metal_db import delete_baking_slice_pool_host
from imbue.minds_admin.slices.bare_metal_db import destroy_eligible_pool_host_statuses
from imbue.minds_admin.slices.bare_metal_db import fetch_leased_slice_hosts
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_destroy_target
<<<<<<< HEAD
from imbue.minds_admin.slices.bare_metal_db import fetch_pool_host_ids_on_server_by_status
=======
from imbue.minds_admin.slices.bare_metal_db import fetch_slice_hosts_by_host_id
>>>>>>> origin/main
from imbue.minds_admin.slices.bare_metal_db import fetch_unleased_slice_teardown_row_ids
from imbue.minds_admin.slices.bare_metal_db import finish_baking_slice_pool_host
from imbue.minds_admin.slices.testing import RecordingConnection
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


def _ready_server() -> BareMetalServer:
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("11111111-1111-1111-1111-111111111111"),
        ovh_order_id="8144904",
        ovh_service_name="ns1012536.ip-15-204-140.us",
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.140.221",
        cpu_cores=8,
        cpu_threads=16,
        ram_gb=64,
        disk_gb=477,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        slot_count=8,
        raid_level="RAID1",
        slice_service_user="slicehost",
        box_host_public_key="ssh-ed25519 AAAAbox",
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
        box_generation=2,
        uplink_mbps=1000,
        wireguard_address="10.202.1.7",
        wireguard_public_key="wgboxpubkey=",
    )


def test_server_insert_placeholder_count_matches_builder() -> None:
    # Every %s placeholder must line up with exactly one builder value.
    values = build_bare_metal_server_insert_values(_ready_server())
    assert _INSERT_BARE_METAL_SERVER_SQL.count("%s") == len(values)


def test_server_insert_values_are_in_column_order() -> None:
    values = build_bare_metal_server_insert_values(_ready_server())
    assert values == (
        "11111111-1111-1111-1111-111111111111",
        "8144904",
        "ns1012536.ip-15-204-140.us",
        "24rise02-v1-us",
        "vin",
        "15.204.140.221",
        8,
        16,
        64,
        477,
        8,
        1.5,
        8,
        "RAID1",
        "slicehost",
        # The legacy lima_service_user column is dual-written with the same value.
        "slicehost",
        "ready",
        2,
        1000,
    )


def test_server_upsert_placeholder_count_matches_builder() -> None:
    # Every %s placeholder must line up with exactly one builder value.
    values = build_bare_metal_server_upsert_values(_ready_server())
    assert _UPSERT_BARE_METAL_SERVER_SQL.count("%s") == len(values)


def test_server_upsert_values_are_the_insert_values_plus_the_key_and_wireguard_identity() -> None:
    # The upsert column list is the insert's plus box_host_public_key then
    # wireguard_address / wireguard_public_key: an imported row must arrive lease-ready with
    # its pinned host key intact, and the copy must be faithful to the source
    # row's prep-assigned WireGuard identity. The WireGuard values repeat once
    # for the legacy wg_address / wg_public_key columns' dual write.
    server = _ready_server()
    values = build_bare_metal_server_upsert_values(server)
    assert values == build_bare_metal_server_insert_values(server) + (
        "ssh-ed25519 AAAAbox",
        "10.202.1.7",
        "wgboxpubkey=",
        "10.202.1.7",
        "wgboxpubkey=",
    )


def test_server_upsert_converges_on_the_source_row_by_id() -> None:
    # import-boxes must be id-preserving and idempotent: conflicting on id and
    # updating (not ignoring) is what makes a re-run converge the target on the
    # source after a box changed (address, host key, status).
    assert "ON CONFLICT (id) DO UPDATE SET" in _UPSERT_BARE_METAL_SERVER_SQL
    assert "box_host_public_key = EXCLUDED.box_host_public_key" in _UPSERT_BARE_METAL_SERVER_SQL


def test_slice_pool_host_insert_placeholder_count_matches_builder() -> None:
    values = build_slice_pool_host_insert_values(
        row_id="row-1",
        box_public_address="15.204.140.221",
        agent_id="agent-1",
        host_id="host-1",
        host_name="ws-1",
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
        attributes_json='{"memory_gb": 8}',
        region="vin",
        bare_metal_server_id="srv-1",
        slice_instance_name="mngr-slice-abc",
        slice_disk_name="mngr-slice-abc-data",
        outer_host_public_key="ssh-ed25519 AAAAouter",
        container_host_public_key="ssh-ed25519 AAAAcontainer",
        box_generation=2,
        memory_units=8,
        disk_gb=28,
    )
    assert _INSERT_SLICE_POOL_HOST_SQL.count("%s") == len(values)


def test_baking_slice_pool_host_insert_placeholder_count_matches_builder() -> None:
    values = build_baking_slice_pool_host_insert_values(
        row_id="row-1",
        box_public_address="15.204.140.221",
        host_id="host-1",
        host_name="ws-1",
        attributes_json='{"memory_gb": 8}',
        region="US-WEST-OR",
        bare_metal_server_id="srv-1",
        slice_instance_name="mngr-slice-abc",
        slice_disk_name="mngr-slice-abc-data",
        box_generation=2,
        memory_units=8,
        disk_gb=42,
    )
    assert _INSERT_BAKING_SLICE_POOL_HOST_SQL.count("%s") == len(values)
    # The pre-carve row is inserted as 'baking' with the bake-result columns NULL;
    # finishing flips exactly a still-baking row to available.
    assert "'baking'" in _INSERT_BAKING_SLICE_POOL_HOST_SQL
    assert _FINISH_BAKING_SLICE_POOL_HOST_SQL.endswith("WHERE id = %s AND status = 'baking'")
    assert "status = 'available'" in _FINISH_BAKING_SLICE_POOL_HOST_SQL


def test_finish_and_delete_baking_row_report_whether_the_row_was_still_baking() -> None:
    finished_conn = RecordingConnection([], rowcount=1)
    assert (
        finish_baking_slice_pool_host(
            finished_conn,
            "row-1",
            agent_id="agent-1",
            vm_ssh_host_port=22001,
            container_ssh_host_port=22002,
            outer_host_public_key="ssh-ed25519 AAAAouter",
            container_host_public_key="ssh-ed25519 AAAAcontainer",
        )
        is True
    )
    assert finished_conn.commit_count == 1
    claimed_conn = RecordingConnection([], rowcount=0)
    assert delete_baking_slice_pool_host(claimed_conn, "row-1") is False
    assert claimed_conn.commit_count == 1


def test_slice_pool_host_insert_uses_lima_instance_as_vps_instance_id() -> None:
    values = build_slice_pool_host_insert_values(
        row_id="row-1",
        box_public_address="15.204.140.221",
        agent_id="agent-1",
        host_id="host-1",
        host_name="ws-1",
        vm_ssh_host_port=22001,
        container_ssh_host_port=22002,
        attributes_json="{}",
        region="vin",
        bare_metal_server_id="srv-1",
        slice_instance_name="mngr-slice-abc",
        slice_disk_name="mngr-slice-abc-data",
        outer_host_public_key="ssh-ed25519 AAAAouter",
        container_host_public_key="ssh-ed25519 AAAAcontainer",
        box_generation=1,
        memory_units=8,
        disk_gb=44,
    )
    # vps_address is the box; vps_instance_id is the (non-null) lima instance;
    # the two forwarded ports are carried verbatim; the owning box's
    # generation and the machine's sizes are stamped last.
    assert values[1] == "15.204.140.221"
    assert values[2] == "mngr-slice-abc"
    assert values[6] == 22001
    assert values[7] == 22002
    assert values[-3] == 1
    assert values[-2] == 8
    assert values[-1] == 44


def test_server_from_row_round_trips() -> None:
    server = _ready_server()
    # _server_from_row reads the SELECT column order: the insert values (minus
    # the dual-written legacy service-user column and the trailing generation
    # columns), then created_at / updated_at, then box_host_public_key /
    # box_generation / uplink_mbps / wireguard_address / wireguard_public_key.
    insert_values = build_bare_metal_server_insert_values(server)
    row = (
        insert_values[:15]
        + insert_values[16:-2]
        + (
            server.created_at,
            server.updated_at,
            server.box_host_public_key,
            server.box_generation,
            server.uplink_mbps,
            server.wireguard_address,
            server.wireguard_public_key,
        )
    )
    reconstructed = _server_from_row(row)
    assert reconstructed.id == server.id
    assert reconstructed.ovh_service_name == server.ovh_service_name
    assert reconstructed.slot_count == 8
    assert str(reconstructed.status) == "ready"
    assert reconstructed.ram_gb == 64
    assert reconstructed.box_host_public_key == "ssh-ed25519 AAAAbox"
    assert reconstructed.box_generation == 2
    assert reconstructed.uplink_mbps == 1000
    assert reconstructed.wireguard_address == "10.202.1.7"
    assert reconstructed.wireguard_public_key == "wgboxpubkey="


def test_fetch_unleased_slice_teardown_row_ids_maps_rows_to_strings() -> None:
    fake_conn = RecordingConnection([("row-1",), ("row-2",)], rowcount=0)
    eligible = destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    row_ids = fetch_unleased_slice_teardown_row_ids(fake_conn, eligible)
    assert row_ids == ["row-1", "row-2"]
    # The status filter is the SAME claimable set the destroy uses, passed as a bind
    # parameter -- so a selected row is always claimable and the predicates can't drift.
    assert fake_conn.recording_cursor.executed_params == (list(eligible),)


def test_fetch_pool_host_ids_on_server_by_status_scopes_to_the_box_and_status_set() -> None:
    # The drain command's row listing: ids come back as strings, and the
    # filter rides bind parameters (the server id stringified, the statuses
    # as a list for ANY(%s)).
    fake_conn = RecordingConnection([("row-1",), ("row-2",)], rowcount=0)
    server_id = BareMetalServerDbId("11111111-1111-1111-1111-111111111111")
    row_ids = fetch_pool_host_ids_on_server_by_status(fake_conn, server_id, ("available", "removing"))
    assert row_ids == ["row-1", "row-2"]
    assert fake_conn.recording_cursor.executed_params == (str(server_id), ["available", "removing"])


def test_unleased_teardown_query_filters_by_the_claimable_status_set() -> None:
    # Leased slices are torn down by their agent's release path and must be excluded --
    # which falls out of the claimable set (it omits 'leased' without --force). 'removing'
    # rows ARE included: a row stranded mid-teardown (crashed release or a destroy whose
    # box was unreachable) must not leak when the env is destroyed.
    assert "p.status = ANY(%s)" in _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL
    assert "leased" not in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    assert "removing" in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False)
    # The inner JOIN on bare_metal_server_id already restricts to slice rows.
    assert "JOIN bare_metal_servers" in _SELECT_UNLEASED_SLICE_TEARDOWN_ROW_IDS_SQL


def test_destroy_eligible_statuses_require_force_for_leased() -> None:
    # The default claim set covers available rows, stale 'removing' rows (a retry of a
    # prior failed teardown), the legacy 'released' value, and 'unreachable' rows the
    # connector quarantined at lease time; only --force adds leased.
    assert destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=False) == (
        "available",
        "baking",
        "released",
        "removing",
        "unreachable",
    )
    assert "leased" in destroy_eligible_pool_host_statuses(is_leased_destroy_allowed=True)


def test_claim_pool_host_for_removal_is_a_single_conditional_update() -> None:
    # The claim must flip the status and check eligibility in ONE statement -- that
    # atomicity (vs the connector's lease, which only selects 'available' rows) is
    # what closes the destroy-vs-lease race.
    assert _CLAIM_POOL_HOST_FOR_REMOVAL_SQL.startswith("UPDATE pool_hosts SET status = 'removing'")
    assert "status = ANY(%s)" in _CLAIM_POOL_HOST_FOR_REMOVAL_SQL
    # A 'baking' row is claimable only once it is older than a bake could take, so a
    # destroy never claims a live bake's row out from under it.
    assert "status <> 'baking' OR created_at < NOW() - make_interval(secs => %s)" in _CLAIM_POOL_HOST_FOR_REMOVAL_SQL


def test_claim_pool_host_for_removal_reports_whether_the_row_was_claimed() -> None:
    claimed_conn = RecordingConnection([], rowcount=1)
    assert claim_pool_host_for_removal(claimed_conn, "row-1", ("available",)) is True
    assert claimed_conn.commit_count == 1
    missed_conn = RecordingConnection([], rowcount=0)
    assert claim_pool_host_for_removal(missed_conn, "row-1", ("available",)) is False
    # The miss is still committed so the (no-op) transaction does not linger.
    assert missed_conn.commit_count == 1


def test_fetch_pool_host_destroy_target_maps_null_box_columns() -> None:
    # A row whose box record was deleted still comes back (LEFT JOIN), with null box
    # columns, so the caller can report it precisely instead of erroring on lookup.
    target = fetch_pool_host_destroy_target(
        RecordingConnection([("mngr-slice-dev-a", None, None, None, 2, None, None)], rowcount=0), "row-1"
    )
    assert target is not None
    assert target.slice_instance_name == "mngr-slice-dev-a"
    assert target.box_public_address is None
    assert target.slice_service_user is None
    assert target.box_wireguard_address is None
    # The row's stamped generation rides along so the teardown client dispatches;
    # a pre-migration NULL maps to generation 1.
    assert target.box_generation == 2
    null_generation_target = fetch_pool_host_destroy_target(
        RecordingConnection([("mngr-slice-dev-a", None, None, None, None, None, None)], rowcount=0), "row-1"
    )
    assert null_generation_target is not None
    assert null_generation_target.box_generation == 1
    assert fetch_pool_host_destroy_target(RecordingConnection([], rowcount=0), "row-gone") is None


def test_fetch_pool_host_destroy_target_carries_the_box_wireguard_address() -> None:
    # The teardown resolves its dial address from the overlay address when the
    # operator's tunnel reaches it (locked-down boxes drop direct :22).
    target = fetch_pool_host_destroy_target(
        RecordingConnection(
            [("mngr-slice-dev-a", "15.204.140.221", "slicehost", "ssh-ed25519 AAAAbox", 2, "10.112.1.1", "boxpub=")],
            rowcount=0,
        ),
        "row-1",
    )
    assert target is not None
    assert target.box_public_address == "15.204.140.221"
    assert target.box_wireguard_address == "10.112.1.1"
    assert target.box_wireguard_public_key == "boxpub="


def test_count_slices_counts_removing_rows_as_occupied() -> None:
    # A 'removing' row's VM may still be tearing down; it holds its box slot until the
    # VM is destroyed and the row deleted, so slot accounting must not exclude it.
    assert "status" not in _COUNT_SLICES_SQL


def _slice_host_row(host_id: str, status: str) -> tuple:
    server_columns = (
        "11111111-1111-1111-1111-111111111111",
        None,
        "ns1.example",
        "plan",
        "hil",
        "203.0.113.7",
        32,
        64,
        256,
        2000,
        16,
        1.5,
        14,
        "RAID1",
        "limahost",
        "ready",
        None,
        None,
        "ssh-ed25519 AAAA box",
    )
    return (host_id, "workspace-1", status, f"mngr-slice-production-{host_id[5:21]}", *server_columns)


def test_fetch_slice_hosts_by_host_id_maps_the_row_and_its_box() -> None:
    fake_conn = _FakeConn([_slice_host_row("host-abcdef0123456789abcdef0123456789", "leased")], rowcount=0)
    rows = fetch_slice_hosts_by_host_id(fake_conn, ["host-abcdef0123456789abcdef0123456789", "host-unknown"])
    assert [row.host_id for row in rows] == ["host-abcdef0123456789abcdef0123456789"]
    assert rows[0].lima_instance_name == "mngr-slice-production-abcdef0123456789"
    assert rows[0].server.public_address == "203.0.113.7"
    assert rows[0].server.lima_service_user == "limahost"
    assert rows[0].server.box_host_public_key == "ssh-ed25519 AAAA box"
    # The ids go down as one array bind parameter; a missing id is simply absent from the result.
    assert fake_conn._cursor.executed_params == (["host-abcdef0123456789abcdef0123456789", "host-unknown"],)


def test_fetch_leased_slice_hosts_binds_the_leased_status() -> None:
    fake_conn = _FakeConn([_slice_host_row("host-abcdef0123456789abcdef0123456789", "leased")], rowcount=0)
    rows = fetch_leased_slice_hosts(fake_conn)
    assert [row.status for row in rows] == ["leased"]
    assert fake_conn._cursor.executed_params == ("leased",)
