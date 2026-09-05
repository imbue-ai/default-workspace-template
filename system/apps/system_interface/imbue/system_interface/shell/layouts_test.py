from pathlib import Path
from typing import Any

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.layouts import LayoutStore
from imbue.system_interface.shell.layouts import empty_layout
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

    saved = store.save_layout("everything", "c1", _layout(), TEST_NOW)
    assert saved.updated_at == TEST_NOW
    assert store.read_layout("everything", "c1", DeviceKind.MOBILE) == saved
    # Another desktop client inherits the seed; a mobile one has no seed yet.
    assert store.read_layout("everything", "c2", DeviceKind.DESKTOP) == saved
    assert store.read_layout("everything", "c2", DeviceKind.MOBILE) == empty_layout(DeviceKind.MOBILE)
    assert (tmp_path / "layouts" / "everything" / "c1.json").is_file()
    assert (tmp_path / "layouts" / "everything" / "seed.desktop.json").is_file()


def test_all_client_layouts_skips_seeds_and_unreadable_files(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_layout("everything", "c1", _layout(), TEST_NOW)
    store.save_layout("alpha", "c2", _layout(DeviceKind.MOBILE), TEST_NOW)
    (tmp_path / "layouts" / "alpha" / "broken.json").write_text("{")
    stored = store.all_client_layouts()
    assert [(str(item.view_id), str(item.client_id)) for item in stored] == [("alpha", "c2"), ("everything", "c1")]
    assert store.referenced_addresses() == {_FILES, _TERMINAL_1}


def test_tabs_are_found_and_rebound_by_id(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_layout("everything", "c1", _layout(), TEST_NOW)
    store.save_layout("alpha", "c1", _layout(), TEST_NOW)
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


def test_removed_addresses_leave_every_layout_and_its_grid(tmp_path: Path) -> None:
    store = LayoutStore(state_directory=tmp_path)
    store.save_layout("everything", "c1", _layout(), TEST_NOW)
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
    store.save_layout("everything", "c1", _layout(), TEST_NOW)
    store.save_layout("alpha", "c1", _layout(), TEST_NOW)
    store.save_layout("alpha", "c2", _layout(), TEST_NOW)
    assert store.delete_client_layouts(ClientId("c1")) == 2
    assert [str(stored.client_id) for stored in store.all_client_layouts()] == ["c2"]
    store.delete_view_layouts("alpha")
    assert store.all_client_layouts() == []
    assert not (tmp_path / "layouts" / "alpha").exists()
    store.delete_view_layouts("never-existed")
