from typing import Any

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import LayoutSaveRequest
from imbue.system_interface.shell.data_types import fold_legacy_tabs_into_dockview
from imbue.system_interface.shell.data_types import instance_panel_params_by_id
from imbue.system_interface.shell.data_types import instance_panel_params_json
from imbue.system_interface.shell.data_types import with_panel_params_address
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId

_FILES = Address("app:files")
_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_TAB_A = TabId("tab-000000000000000a")
_TAB_B = TabId("tab-000000000000000b")


def _dockview(panels: dict[str, Any]) -> dict[str, Any]:
    return {"grid": {"root": {"type": "branch", "data": []}}, "panels": panels, "activeGroup": "g1"}


def test_instance_panel_params_are_read_off_the_dockview_document_skipping_launchers_and_damage() -> None:
    dockview = _dockview(
        {
            "pa": {"id": "pa", "params": instance_panel_params_json(_FILES, _TAB_A, 7)},
            # A key the browser added that the shell does not know is tolerated.
            "pb": {"id": "pb", "params": {**instance_panel_params_json(_TERMINAL_1, _TAB_B, 0), "future": True}},
            "new-tab-1": {"id": "new-tab-1", "params": {"kind": "launcher"}},
            "damaged": {"id": "damaged", "params": {"kind": "instance", "address": "not-an-address"}},
            "bare": {"id": "bare"},
            "junk": "not a dict",
        }
    )
    parsed = instance_panel_params_by_id(dockview)
    assert set(parsed) == {"pa", "pb"}
    assert parsed["pa"].address == _FILES and parsed["pa"].tab_id == _TAB_A and parsed["pa"].last_focused_ms == 7
    assert parsed["pb"].address == _TERMINAL_1 and parsed["pb"].last_focused_ms == 0
    assert instance_panel_params_by_id(None) == {}
    assert instance_panel_params_by_id({"panels": []}) == {}


def test_repointing_a_panel_keeps_every_other_key_of_its_params() -> None:
    dockview = _dockview({"pa": {"id": "pa", "params": {**instance_panel_params_json(_FILES, _TAB_A, 7), "extra": 1}}})
    repointed = with_panel_params_address(dockview, "pa", _TERMINAL_1)
    assert repointed["panels"]["pa"]["params"] == {
        "kind": "instance",
        "address": str(_TERMINAL_1),
        "tabId": str(_TAB_A),
        "lastFocusedMs": 7,
        "extra": 1,
    }
    # Pure: the input is left as it was.
    assert dockview["panels"]["pa"]["params"]["address"] == str(_FILES)


def test_a_layout_in_the_older_shape_reads_as_params_only() -> None:
    """A file written before params-only layouts carried a ``tabs`` block, which was the truth of each panel's identity."""
    legacy = {
        "dockview": _dockview(
            {
                # The older browser wrote params too, but its ``tabs`` block was what it read back: the block wins.
                "pa": {"id": "pa", "params": {"kind": "instance", "address": "app:stale", "tabId": "tab-00000000000000ff"}},
                "new-tab-1": {"id": "new-tab-1", "params": {"kind": "launcher"}},
                "orphan": {"id": "orphan"},
            }
        ),
        "tabs": {
            "pa": {"address": str(_FILES), "tab_id": str(_TAB_A), "last_focused_ms": 7},
            "gone": {"address": str(_TERMINAL_1), "tab_id": str(_TAB_B), "last_focused_ms": 0},
        },
        "device_kind": "desktop",
        "updated_at": None,
    }
    layout = LayoutRecord.model_validate(legacy)
    assert layout.dockview is not None
    assert layout.dockview["panels"]["pa"]["params"] == {
        "kind": "instance",
        "address": str(_FILES),
        "tabId": str(_TAB_A),
        "lastFocusedMs": 7,
    }
    # A record for a panel the grid no longer names is dropped; the launcher and the orphan are left alone.
    assert set(layout.dockview["panels"]) == {"pa", "new-tab-1", "orphan"}
    assert "params" not in layout.dockview["panels"]["orphan"]
    assert set(instance_panel_params_by_id(layout.dockview)) == {"pa"}
    assert "tabs" not in layout.model_dump(mode="json")

    # The empty legacy layout, and a body with no ``tabs`` at all, read unchanged.
    assert LayoutRecord.model_validate(
        {"dockview": None, "tabs": {}, "device_kind": "desktop", "updated_at": None}
    ) == LayoutRecord(dockview=None, device_kind=DeviceKind.DESKTOP, updated_at=None)
    current = {"dockview": None, "device_kind": "desktop", "updated_at": None}
    assert fold_legacy_tabs_into_dockview(current) is current
    assert fold_legacy_tabs_into_dockview("not a mapping") == "not a mapping"

    # The save body takes the same fold.
    request = LayoutSaveRequest.model_validate(
        {
            "client_id": "c1",
            "save_id": "save-0000000000000001",
            "device_kind": "desktop",
            "dockview": legacy["dockview"],
            "tabs": legacy["tabs"],
        }
    )
    assert instance_panel_params_by_id(request.dockview)["pa"].address == _FILES
