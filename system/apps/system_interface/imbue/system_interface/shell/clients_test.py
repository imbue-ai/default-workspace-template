import json
from datetime import timedelta
from pathlib import Path

import pytest
from app_manifest.manifest import EntryMode
from app_manifest.manifest import PinStyle

from imbue.system_interface.shell.clients import CLIENTS_FILENAME
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.clients import ClientStore
from imbue.system_interface.shell.clients import client_wire_json
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import EntryPresentation
from imbue.system_interface.shell.data_types import FloatingPosition
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.testing import TEST_NOW


def _report(client_id: str, desktop: str) -> ClientStateReport:
    return ClientStateReport(client_id=ClientId(client_id), active_desktop=DesktopId(desktop))


def test_reports_are_recorded_and_listed_newest_first(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    assert store.record_report(_report("c1", "home"), TEST_NOW).is_active_desktop_changed is True
    store.record_report(_report("c2", "research"), TEST_NOW + timedelta(minutes=1))
    outcome = store.record_report(_report("c1", "research"), TEST_NOW + timedelta(minutes=2))
    recorded = outcome.record
    assert outcome.is_active_desktop_changed is True
    assert (
        store.record_report(_report("c1", "research"), TEST_NOW + timedelta(minutes=3)).is_active_desktop_changed
        is False
    )
    assert [str(client.id) for client in store.list_clients()] == ["c1", "c2"]
    assert recorded.active_desktop == "research"
    assert store.get_client("missing") is None
    assert client_wire_json(recorded, True) == {
        "id": "c1",
        "active_desktop": "research",
        "last_seen": "2026-09-04T00:02:00+00:00",
        "is_connected": True,
        "entries": {},
    }
    assert json.loads((tmp_path / CLIENTS_FILENAME).read_text())["version"] == 2


def test_set_active_desktop_moves_a_recorded_client_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(_report("c1", "home"), TEST_NOW)
    moved = store.set_active_desktop(ClientId("c1"), DesktopId("research"), TEST_NOW + timedelta(minutes=1))
    assert moved.is_active_desktop_changed is True and moved.record.active_desktop == "research"
    assert store.set_active_desktop(ClientId("c1"), DesktopId("research"), TEST_NOW).is_active_desktop_changed is False
    with pytest.raises(ClientNotFoundError):
        store.set_active_desktop(ClientId("nobody"), DesktopId("research"), TEST_NOW)


def test_an_entry_presentation_is_kept_on_the_client_across_its_reports_and_refused_for_an_unknown_client(
    tmp_path: Path,
) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(_report("c1", "home"), TEST_NOW)
    floating = EntryPresentation(
        mode=EntryMode.FLOATING, style=PinStyle.AVATAR, position=FloatingPosition(x=0.9, y=0.85)
    )
    record = store.set_entry_presentation(ClientId("c1"), "chat", floating, TEST_NOW + timedelta(minutes=1))
    assert record.entries == {"chat": floating}
    assert record.last_seen == TEST_NOW + timedelta(minutes=1)
    # A later report and a desktop move keep the presentation; a second app's entry sits beside it.
    store.record_report(_report("c1", "research"), TEST_NOW + timedelta(minutes=2))
    store.set_active_desktop(ClientId("c1"), DesktopId("home"), TEST_NOW + timedelta(minutes=3))
    bar = EntryPresentation(mode=EntryMode.BAR, style=PinStyle.PLAIN, position=None)
    both = store.set_entry_presentation(ClientId("c1"), "notes", bar, TEST_NOW + timedelta(minutes=4))
    assert both.entries == {"chat": floating, "notes": bar}
    assert client_wire_json(both, False)["entries"] == {
        "chat": {"mode": "floating", "style": "avatar", "position": {"x": 0.9, "y": 0.85}},
        "notes": {"mode": "bar", "style": "plain", "position": None},
    }
    stored = json.loads((tmp_path / CLIENTS_FILENAME).read_text())["clients"]["c1"]["entries"]
    assert set(stored) == {"chat", "notes"}
    with pytest.raises(ClientNotFoundError):
        store.set_entry_presentation(ClientId("nobody"), "chat", bar, TEST_NOW)


def test_clients_unseen_for_the_retention_period_are_pruned(tmp_path: Path) -> None:
    store = ClientStore(state_directory=tmp_path)
    store.record_report(_report("old", "home"), TEST_NOW - CLIENT_RETENTION - timedelta(days=1))
    store.record_report(_report("fresh", "home"), TEST_NOW - timedelta(days=1))
    assert store.prune_unseen(TEST_NOW) == [ClientId("old")]
    assert [str(client.id) for client in store.list_clients()] == ["fresh"]
    assert store.prune_unseen(TEST_NOW) == []


def test_a_version_one_file_is_read_with_the_view_as_the_desktop_and_rewritten_at_version_two(tmp_path: Path) -> None:
    """The tabbed shell's file carried a device kind and a view per client; a client that reported a desktop keeps
    it, one that never did lands on the desktop of its view's id (or the first desktop, when none has it)."""
    legacy = {
        "version": 1,
        "clients": {
            "viewer": {"device_kind": "mobile", "active_view": "alpha", "last_seen": "2026-09-01T00:00:00+00:00"},
            "desktopper": {
                "device_kind": "desktop",
                "active_view": "everything",
                "active_desktop": "home",
                "last_seen": "2026-09-02T00:00:00+00:00",
            },
        },
    }
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps(legacy))
    store = ClientStore(state_directory=tmp_path)

    by_id = {str(client.id): client for client in store.list_clients()}
    assert by_id["viewer"].active_desktop == "alpha"
    assert by_id["desktopper"].active_desktop == "home"

    store.record_report(_report("c3", "home"), TEST_NOW)
    written = json.loads((tmp_path / CLIENTS_FILENAME).read_text())
    assert written["version"] == 2
    assert set(written["clients"]) == {"viewer", "desktopper", "c3"}
    assert set(written["clients"]["viewer"]) == {"active_desktop", "last_seen", "entries"}


def test_a_file_of_an_unknown_version_or_shape_is_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps({"version": 7, "clients": {}}))
    assert ClientStore(state_directory=tmp_path).list_clients() == []
    (tmp_path / CLIENTS_FILENAME).write_text(json.dumps({"version": 2, "clients": "nope"}))
    assert ClientStore(state_directory=tmp_path).list_clients() == []
