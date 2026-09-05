"""Tests for ``ShellState``: the referenced-lifetime deletion and the pruning that follows an app's list shrinking."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from app_instances.data_types import InstanceLifetime
from app_instances.testing import StubInstanceSource
from app_instances.testing import wait_until

from imbue.imbue_common.model_update import to_update
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
_STUB_1 = Address("app:stub?instance=stub-1")
_STUB_2 = Address("app:stub?instance=stub-2")


def _shell_over_stub(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_app_url: str, clock: list[float]
) -> ShellState:
    registry_path = write_registry(
        tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True, actions=[("new", "New")])
    )
    inventory = build_inventory(registry_path, broadcaster, fetcher=HttpInstanceFetcher(), clock=lambda: clock[0])
    inventory.refetch_now("stub")
    return build_shell_state(tmp_path / "state", registry_path, broadcaster, inventory=inventory)


def _layout_showing(*addresses: Address) -> LayoutRecord:
    return LayoutRecord(
        dockview={"panels": {f"p{index}": {} for index in range(len(addresses))}},
        tabs={
            f"p{index}": TabRecord(address=address, tab_id=TabId(f"tab-{index:016x}"), last_focused_ms=0)
            for index, address in enumerate(addresses)
        },
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )


def test_unreferenced_referenced_instances_are_deleted_after_the_grace_period(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_source: StubInstanceSource, stub_app_url: str
) -> None:
    stub_source.records.extend(
        [
            instance_record("stub-1", lifetime=InstanceLifetime.REFERENCED),
            instance_record("stub-2", lifetime=InstanceLifetime.REFERENCED),
            instance_record("stub-3", lifetime=InstanceLifetime.EXPLICIT),
        ]
    )
    clock = [1000.0]
    shell = _shell_over_stub(tmp_path, broadcaster, stub_app_url, clock)
    try:
        shell.projects.create_project("Alpha", "#111111", 0, ())
        shell.projects.add_tab("alpha", _STUB_1)
        shell.layouts.save_layout("everything", "c1", _layout_showing(_STUB_2), _NOW)
        # Everything is referenced, and stub-3 is explicit: nothing goes.
        assert shell.delete_unreferenced_instances() == []
        shell.layouts.save_layout("everything", "c1", _layout_showing(), _NOW)
        # stub-2 is unreferenced now but within its grace period.
        assert shell.delete_unreferenced_instances() == []
        clock[0] += 60.0
        assert shell.delete_unreferenced_instances() == [_STUB_2]
        assert [str(record.key) for record in stub_source.records] == ["stub-1", "stub-3"]
        assert shell.inventory.listed_addresses() == {_STUB_1, Address("app:stub?instance=stub-3")}
    finally:
        shell.stop()


def test_instances_an_app_stopped_listing_leave_the_tab_sets_and_layouts(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_source: StubInstanceSource, stub_app_url: str
) -> None:
    stub_source.records.extend([instance_record("stub-1"), instance_record("stub-2")])
    shell = _shell_over_stub(tmp_path, broadcaster, stub_app_url, [0.0])
    try:
        shell.projects.create_project("Alpha", "#111111", 0, ())
        shell.projects.add_tab("alpha", _STUB_1)
        shell.projects.add_tab("alpha", _STUB_2)
        shell.layouts.save_layout("alpha", "c1", _layout_showing(_STUB_1, _STUB_2), _NOW)
        shell.inventory.add_removed_listener(shell.on_instances_removed)
        client_queue = broadcaster.register()

        stub_source.records = [record for record in stub_source.records if str(record.key) != "stub-1"]
        shell.inventory.refetch_now("stub")

        assert shell.projects.get_project("alpha").tabs == (_STUB_2,)
        assert set(shell.layouts.read_layout("alpha", "c1", DeviceKind.DESKTOP).tabs) == {"p1"}
        types = [message["type"] for message in drain_messages(client_queue)]
        assert "projects_updated" in types and "apps_updated" in types
    finally:
        shell.stop()


def test_start_prunes_stale_clients_and_their_layouts_now_and_on_the_interval(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_app_url: str
) -> None:
    registry_path = write_registry(tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True))
    inventory = build_inventory(registry_path, broadcaster)
    built = build_shell_state(tmp_path / "state", registry_path, broadcaster, inventory=inventory)
    shell = built.model_copy_update(to_update(built.field_ref().client_prune_interval_seconds, 0.05))
    stale_at = _NOW - CLIENT_RETENTION - timedelta(days=1)
    shell.clients.record_report(
        ClientStateReport(client_id=ClientId("old"), device_kind=DeviceKind.DESKTOP, active_view=ViewId("everything")),
        stale_at,
    )
    shell.layouts.save_layout("everything", "old", _layout_showing(_STUB_1), stale_at)
    shell.start()
    try:
        # The prune at start took the stale client and its layout file.
        assert shell.clients.get_client("old") is None
        assert shell.layouts.all_client_layouts() == []
        # A client that goes stale while the shell runs is taken by the periodic prune.
        shell.clients.record_report(
            ClientStateReport(
                client_id=ClientId("later"), device_kind=DeviceKind.DESKTOP, active_view=ViewId("everything")
            ),
            stale_at,
        )
        assert wait_until(lambda: shell.clients.get_client("later") is None, timeout_seconds=5.0)
    finally:
        shell.stop()
