from datetime import datetime
from datetime import timezone

import psycopg2
import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.box_registry import BoxRegistryImportError
from imbue.minds_admin.slices.box_registry import assert_registry_servers_importable
from imbue.minds_admin.slices.box_registry import import_ready_servers
from imbue.minds_admin.slices.testing import RecordingConnection
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


def _ready_server(region: str) -> BareMetalServer:
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId("33333333-3333-3333-3333-333333333333"),
        plan_code="24sys032-us",
        region=region,
        public_address="135.148.55.218",
        slot_count=14,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
        uplink_mbps=1000,
    )


@pytest.mark.parametrize("regions", [(), ("vin", "hil")])
def test_registry_servers_in_known_datacenters_are_importable(regions: tuple[str, ...]) -> None:
    assert_registry_servers_importable([_ready_server(region) for region in regions])


def test_registry_servers_in_an_unmapped_datacenter_refuse_the_whole_import() -> None:
    known = _ready_server("vin")
    unmapped = known.model_copy_update(
        to_update(known.field_ref().id, BareMetalServerDbId("44444444-4444-4444-4444-444444444444")),
        to_update(known.field_ref().region, "gra"),
    )
    with pytest.raises(BoxRegistryImportError, match="44444444-4444-4444-4444-444444444444 \\('gra'\\)"):
        assert_registry_servers_importable([known, unmapped])


def test_import_ready_servers_skips_a_box_the_target_already_registers() -> None:
    # A registry box the target DB already holds under another row id trips the
    # ovh_service_name unique index; the import must roll that statement back,
    # report the box as skipped, and still import the rest.
    source = _ready_server("vin")
    fresh = source.model_copy_update(to_update(source.field_ref().ovh_service_name, "ns-fresh"))
    duplicate = fresh.model_copy_update(
        to_update(fresh.field_ref().id, BareMetalServerDbId("22222222-2222-2222-2222-222222222222")),
        to_update(fresh.field_ref().ovh_service_name, "ns-duplicate"),
    )
    conn = RecordingConnection(
        [],
        rowcount=1,
        error_by_param={
            "ns-duplicate": psycopg2.errors.UniqueViolation(
                'duplicate key value violates unique constraint "bare_metal_servers_service_name_idx"'
            )
        },
    )
    report = import_ready_servers(conn, [duplicate, fresh])
    assert [server.id for server in report.imported] == [fresh.id]
    assert [
        (skipped.server.id, "bare_metal_servers_service_name_idx" in skipped.reason) for skipped in report.skipped
    ] == [(duplicate.id, True)]
    assert conn.rollback_count == 1
    assert [params[0] for _sql, params in conn.recording_cursor.executed] == [str(fresh.id)]


def test_import_ready_servers_rolls_back_and_raises_on_any_other_database_error() -> None:
    # Only a unique-index collision is a per-box skip; any other failure of the
    # upsert (here a missing column) must roll the statement back and abort
    # the import as a BoxRegistryImportError naming the box.
    server = _ready_server("vin")
    conn = RecordingConnection(
        [],
        rowcount=1,
        error_by_param={str(server.id): psycopg2.errors.UndefinedColumn('column "uplink_mbps" does not exist')},
    )
    with pytest.raises(BoxRegistryImportError, match=f"{server.id} .*uplink_mbps"):
        import_ready_servers(conn, [server])
    assert conn.rollback_count == 1
    assert conn.commit_count == 0
