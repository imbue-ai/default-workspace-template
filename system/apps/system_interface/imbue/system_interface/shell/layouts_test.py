from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.errors import StaleLayoutSaveError
from imbue.system_interface.shell.layouts import LayoutStore
from imbue.system_interface.shell.layouts import empty_layout
from imbue.system_interface.shell.layouts import is_stale_save
from imbue.system_interface.shell.layouts import rebind_tab_in_layout
from imbue.system_interface.shell.layouts import strip_address_from_layout
from imbue.system_interface.shell.layouts import strip_panel_from_dockview
from imbue.system_interface.shell.layouts import unreferenced_addresses
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.testing import TEST_NOW

_FILES = Address("app:files")
_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_TAB_A = TabId("tab-000000000000000a")
_TAB_B = TabId("tab-000000000000000b")


def _dockview(*panel_ids: str) -> dict[str, Any]:
    return {
        "grid": {
            "root": {
                "type": "branch",
                "data": [{"type": "leaf", "data": {"views": list(panel_ids), "activeView": panel_ids[0]}}],
            },
            "orientation": "HORIZONTAL",
        },
        "panels": {panel_id: {"id": panel_id} for panel_id in panel_ids},
        "activeGroup": "g1",
    }


def _layout(device_kind: DeviceKind = DeviceKind.DESKTOP) -> LayoutRecord:
    return LayoutRecord(
        dockview=_dockview("p1", "p2"),
        tabs={
            "p1": TabRecord(address=_FILES, tab_id=_TAB_A, last_focused_ms=10),
            "p2": TabRecord(address=_TERMINAL_1, tab_id=_TAB_B, last_focused_ms=20),
        },
        device_kind=device_kind,
        updated_at=None,
    )


def test_read_falls_back_from_own_to_seed_to_empty(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    assert store.read_layout("everything", "c1", DeviceKind.DESKTOP) == empty_layout(DeviceKind.DESKTOP)

    saved = store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    assert saved is not None and saved.updated_at == TEST_NOW
    assert store.read_layout("everything", "c1", DeviceKind.MOBILE) == saved
    # Another desktop client inherits the seed; a mobile one has no seed yet.
    assert store.read_layout("everything", "c2", DeviceKind.DESKTOP) == saved
    assert store.read_layout("everything", "c2", DeviceKind.MOBILE) == empty_layout(DeviceKind.MOBILE)
    assert (tmp_path / "layouts" / "everything" / "c1.json").is_file()
    assert (tmp_path / "layouts" / "everything" / "seed.desktop.json").is_file()


def test_all_client_layouts_skips_seeds_and_unreadable_files(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    store.save_browser_layout("alpha", "c2", _layout(DeviceKind.MOBILE), None, TEST_NOW)
    (tmp_path / "layouts" / "alpha" / "broken.json").write_text("{")
    stored = store.all_client_layouts()
    assert [(str(item.view_id), str(item.client_id)) for item in stored] == [("alpha", "c2"), ("everything", "c1")]
    assert store.referenced_addresses() == {_FILES, _TERMINAL_1}


def test_tabs_are_found_and_rebound_by_id(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    store.save_browser_layout("alpha", "c1", _layout(), None, TEST_NOW)
    assert [(str(stored.view_id), panel_id) for stored, panel_id in store.find_tab(_TAB_B)] == [
        ("alpha", "p2"),
        ("everything", "p2"),
    ]
    rebound = Address("app:terminal?instance=terminal-2")
    rewritten = store.rebind_tab(_TAB_B, rebound, TEST_NOW)
    assert {str(stored.view_id) for stored in rewritten} == {"alpha", "everything"}
    assert store.read_layout("alpha", "c1", DeviceKind.DESKTOP).tabs["p2"].address == rebound
    assert store.read_layout("alpha", "c1", DeviceKind.DESKTOP).tabs["p2"].tab_id == _TAB_B
    assert store.find_tab(TabId("tab-00000000000000ff")) == []
    # The seeds follow, so a client that arrives later starts from the rebound tab too.
    assert store.read_layout("alpha", "c9", DeviceKind.DESKTOP).tabs["p2"].address == rebound
    assert rebind_tab_in_layout(_layout(), TabId("tab-00000000000000ff"), rebound) == _layout()


def test_removed_addresses_leave_every_layout_and_its_grid(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    rewritten = store.remove_addresses_everywhere([_TERMINAL_1], TEST_NOW)
    assert len(rewritten) == 1
    layout = store.read_layout("everything", "c1", DeviceKind.DESKTOP)
    assert set(layout.tabs) == {"p1"}
    assert layout.dockview is not None
    assert set(layout.dockview["panels"]) == {"p1"}
    assert layout.dockview["grid"]["root"]["data"][0]["data"]["views"] == ["p1"]
    # Removing the last panel leaves the empty layout rather than a grid with nothing in it.
    store.remove_addresses_everywhere([_FILES], TEST_NOW)
    emptied = store.read_layout("everything", "c1", DeviceKind.DESKTOP)
    assert emptied.dockview is None and emptied.tabs == {}
    # Nothing to remove rewrites nothing.
    assert store.remove_addresses_everywhere([_FILES], TEST_NOW) == []
    # The seed was stripped too, rather than overwritten with a client's layout.
    assert store.read_layout("everything", "c9", DeviceKind.DESKTOP).tabs == {}


def test_a_browser_save_is_refused_when_the_stored_arrangement_is_newer(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    first = store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    assert first is not None
    later = TEST_NOW + timedelta(seconds=5)
    # The shell edited the file after the window fetched it.
    store.write_client_layout("everything", "c1", strip_address_from_layout(_layout(), _FILES), later)
    with pytest.raises(StaleLayoutSaveError):
        store.save_browser_layout("everything", "c1", _layout(), TEST_NOW, later + timedelta(seconds=1))
    # A window that fetched the newer arrangement may save over it.
    saved = store.save_browser_layout("everything", "c1", _layout(), later, later + timedelta(seconds=2))
    assert saved is not None and saved.updated_at == later + timedelta(seconds=2)
    # A window that never fetched anything is stale against any stored arrangement.
    with pytest.raises(StaleLayoutSaveError):
        store.save_browser_layout("everything", "c1", _layout(), None, later + timedelta(seconds=3))
    assert is_stale_save(None, None) is False
    assert is_stale_save(empty_layout(DeviceKind.DESKTOP), None) is False


def test_a_browser_save_that_changes_nothing_is_skipped(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    first = store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    assert first is not None
    assert store.save_browser_layout("everything", "c1", _layout(), TEST_NOW, TEST_NOW + timedelta(seconds=1)) is None
    assert store.read_layout("everything", "c1", DeviceKind.DESKTOP).updated_at == TEST_NOW


def test_an_edit_reads_the_stored_arrangement_at_the_write_and_skips_a_change_of_nothing(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    seen: list[LayoutRecord] = []

    def drop_files(layout: LayoutRecord) -> LayoutRecord:
        seen.append(layout)
        return strip_address_from_layout(layout, _FILES)

    # A client with no arrangement of the view starts from the seed of its device kind, then from its own file.
    store.save_browser_layout("everything", "seed-maker", _layout(), None, TEST_NOW)
    first = store.edit_client_layout(
        "everything", "c1", DeviceKind.DESKTOP, drop_files, TEST_NOW + timedelta(seconds=1)
    )
    assert first.is_written is True and set(first.layout.tabs) == {"p2"}
    assert seen[0].tabs.keys() == {"p1", "p2"}
    assert store.read_client_layout("everything", "c1") == first.layout
    # The edit is handed what is stored when it runs, not an earlier snapshot: a browser save in between is what it sees.
    store.save_browser_layout("everything", "c1", _layout(), first.layout.updated_at, TEST_NOW + timedelta(seconds=2))
    second = store.edit_client_layout(
        "everything", "c1", DeviceKind.DESKTOP, drop_files, TEST_NOW + timedelta(seconds=3)
    )
    assert seen[-1].tabs.keys() == {"p1", "p2"} and second.is_written is True
    # An edit that changes nothing is neither written nor stamped.
    third = store.edit_client_layout(
        "everything", "c1", DeviceKind.DESKTOP, drop_files, TEST_NOW + timedelta(seconds=4)
    )
    assert third.is_written is False and third.layout == second.layout
    assert store.read_client_layout("everything", "c1") == second.layout


def test_the_shells_own_write_leaves_the_seed_alone(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    edited = strip_address_from_layout(_layout(), _FILES)
    written = store.write_client_layout("everything", "c1", edited, TEST_NOW + timedelta(seconds=1))
    assert set(written.tabs) == {"p2"}
    assert store.read_client_layout("everything", "c1") == written
    assert store.read_client_layout("everything", "c9") is None
    assert set(store.read_layout("everything", "c9", DeviceKind.DESKTOP).tabs) == {"p1", "p2"}


def test_strip_helpers_are_pure_over_the_dockview_shape() -> None:
    dockview = _dockview("p1", "p2")
    stripped = strip_panel_from_dockview(dockview, "p1")
    assert stripped is not None
    assert stripped["grid"]["root"]["data"][0]["data"] == {"views": ["p2"], "activeView": "p2"}
    assert strip_panel_from_dockview(_dockview("p1"), "p1") is None
    untouched = strip_address_from_layout(_layout(), Address("app:browser"))
    assert untouched == _layout()
    assert unreferenced_addresses([_FILES, _TERMINAL_1], {_FILES}) == [_TERMINAL_1]


def test_client_and_view_layouts_can_be_deleted(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_browser_layout("everything", "c1", _layout(), None, TEST_NOW)
    store.save_browser_layout("alpha", "c1", _layout(), None, TEST_NOW)
    store.save_browser_layout("alpha", "c2", _layout(), None, TEST_NOW)
    assert store.delete_client_layouts(ClientId("c1")) == 2
    assert [str(stored.client_id) for stored in store.all_client_layouts()] == ["c2"]
    store.delete_view_layouts("alpha")
    assert store.all_client_layouts() == []
    assert not (tmp_path / "layouts" / "alpha").exists()
    store.delete_view_layouts("never-existed")
