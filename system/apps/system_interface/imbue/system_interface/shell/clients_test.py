from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.clients import client_wire_json
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import ViewId

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _report(client_id: str, view: str, device_kind: DeviceKind = DeviceKind.DESKTOP) -> ClientStateReport:
    return ClientStateReport(client_id=ClientId(client_id), device_kind=device_kind, active_view=ViewId(view))


def test_reports_are_recorded_and_listed_newest_first(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(_report("c1", "everything"), _NOW)
    store.record_report(_report("c2", "alpha", DeviceKind.MOBILE), _NOW + timedelta(minutes=1))
    recorded = store.record_report(_report("c1", "alpha"), _NOW + timedelta(minutes=2))
    assert [str(client.id) for client in store.list_clients()] == ["c1", "c2"]
    assert recorded.active_view == "alpha"
    second = store.get_client("c2")
    assert second is not None and second.device_kind is DeviceKind.MOBILE
    assert store.get_client("missing") is None
    assert client_wire_json(recorded) == {
        "id": "c1",
        "device_kind": "desktop",
        "active_view": "alpha",
        "last_seen": "2026-09-04T12:02:00+00:00",
    }


def test_clients_unseen_for_the_retention_period_are_pruned(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(_report("old", "everything"), _NOW - CLIENT_RETENTION - timedelta(days=1))
    store.record_report(_report("fresh", "everything"), _NOW - timedelta(days=1))
    assert store.prune_unseen(_NOW) == [ClientId("old")]
    assert [str(client.id) for client in store.list_clients()] == ["fresh"]
    assert store.prune_unseen(_NOW) == []
