from typing import Any

import pytest

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import instance_panel_params_json
from imbue.system_interface.shell.dockview_document import Direction
from imbue.system_interface.shell.dockview_document import HORIZONTAL
from imbue.system_interface.shell.dockview_document import NOMINAL_ROOT_HEIGHT
from imbue.system_interface.shell.dockview_document import NOMINAL_ROOT_WIDTH
from imbue.system_interface.shell.dockview_document import Placement
from imbue.system_interface.shell.dockview_document import add_panel
from imbue.system_interface.shell.dockview_document import focus_panel
from imbue.system_interface.shell.dockview_document import is_launcher_panel_id
from imbue.system_interface.shell.dockview_document import move_panel
from imbue.system_interface.shell.dockview_document import panel_id_for_address
from imbue.system_interface.shell.dockview_document import remove_panel
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import PanelNotFoundError
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.testing import addresses_by_panel_id

_FILES = Address("app:files")
_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_TERMINAL_2 = Address("app:terminal?instance=terminal-2")
_TAB_A = TabId("tab-000000000000000a")
_TAB_B = TabId("tab-000000000000000b")
_TAB_C = TabId("tab-000000000000000c")
_TAB_D = TabId("tab-000000000000000d")


def _panel(panel_id: str, address: Address, tab_id: TabId) -> dict[str, Any]:
    return {"id": panel_id, "params": instance_panel_params_json(address, tab_id, 0)}


def _leaf(group_id: str, *views: str, size: int) -> dict[str, Any]:
    return {"type": "leaf", "data": {"views": list(views), "activeView": views[0], "id": group_id}, "size": size}


def _two_groups_side_by_side() -> LayoutRecord:
    """What dockview saves for two groups left and right: a horizontal root of two leaves."""
    dockview = {
        "grid": {
            "root": {
                "type": "branch",
                "data": [_leaf("g1", "pa", size=600), _leaf("g2", "pb", size=600)],
                "size": 800,
            },
            "width": 1200,
            "height": 800,
            "orientation": HORIZONTAL,
        },
        "panels": {"pa": _panel("pa", _FILES, _TAB_A), "pb": _panel("pb", _TERMINAL_1, _TAB_B)},
        "activeGroup": "g1",
    }
    return LayoutRecord(dockview=dockview, device_kind=DeviceKind.DESKTOP, updated_at=None)


def _placement(
    anchor: str | None,
    direction: Direction | None = None,
    ratio: float = 0.5,
    is_new_group: bool = False,
    group_id: str = "g-new",
) -> Placement:
    return Placement(
        anchor_panel_id=anchor, direction=direction, ratio=ratio, is_new_group=is_new_group, group_id=group_id
    )


def _leaves(dockview: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        if node["type"] == "leaf":
            found.append(node)
            return
        for child in node["data"]:
            walk(child)

    walk(dockview["grid"]["root"])
    return found


def test_panel_lookup_widens_a_bare_app_to_any_of_its_instances() -> None:
    layout = _two_groups_side_by_side()
    assert panel_id_for_address(layout, _TERMINAL_1) == "pb"
    assert panel_id_for_address(layout, Address("app:terminal")) == "pb"
    assert panel_id_for_address(layout, _TERMINAL_2) is None
    assert panel_id_for_address(layout, Address("app:browser")) is None
    assert is_launcher_panel_id("new-tab-000000000000000a") and not is_launcher_panel_id("tab-000000000000000a")


def test_adding_to_a_never_arranged_view_builds_a_root_branch_dockview_accepts() -> None:
    empty = LayoutRecord(dockview=None, device_kind=DeviceKind.DESKTOP, updated_at=None)
    added = add_panel(empty, _FILES, _TAB_A, "Files", _placement(None))
    dockview = added.dockview
    assert dockview is not None
    assert dockview["grid"]["root"]["type"] == "branch"
    assert dockview["grid"]["width"] == NOMINAL_ROOT_WIDTH and dockview["grid"]["height"] == NOMINAL_ROOT_HEIGHT
    assert _leaves(dockview)[0]["data"] == {"views": [str(_TAB_A)], "activeView": str(_TAB_A), "id": "g-new"}
    assert dockview["activeGroup"] == "g-new"
    panel = dockview["panels"][str(_TAB_A)]
    assert panel["contentComponent"] == "instance" and panel["tabComponent"] == "custom" and panel["title"] == "Files"
    assert panel["params"] == {"kind": "instance", "address": str(_FILES), "tabId": str(_TAB_A), "lastFocusedMs": 0}
    assert addresses_by_panel_id(added.dockview) == {str(_TAB_A): _FILES}


def test_adding_with_no_anchor_tabs_into_the_active_group_and_keeps_its_launcher() -> None:
    """A New Tab beside real tabs is an ordinary tab: docking into its group leaves it open.

    Only the stand-in for an empty dock is consumed, and this document holds two other panels,
    so the launcher here cannot be one.
    """
    layout = _two_groups_side_by_side()
    launcher_id = "new-tab-000000000000000f"
    dockview = layout.dockview
    assert dockview is not None
    dockview["grid"]["root"]["data"][1]["data"]["views"].append(launcher_id)
    dockview["panels"][launcher_id] = {"id": launcher_id}
    dockview["activeGroup"] = "g2"

    added = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement(None))

    assert added.dockview is not None
    right = _leaves(added.dockview)[1]["data"]
    assert right["views"] == ["pb", launcher_id, str(_TAB_C)] and right["activeView"] == str(_TAB_C)
    assert launcher_id in added.dockview["panels"]
    assert added.dockview["activeGroup"] == "g2"


def test_adding_into_a_dock_holding_only_a_launcher_consumes_that_launcher() -> None:
    """The browser mints a launcher so an emptied dock shows something; filling the pane spends it.

    It is the document's only other panel, which is what tells it apart from a New Tab the user
    opened -- one can only be minted at zero panels, when there is no "+" to press.
    """
    launcher_id = "new-tab-000000000000000f"
    placeholder_only = LayoutRecord(
        dockview={
            "grid": {
                "root": {"type": "branch", "data": [_leaf("g1", launcher_id, size=1200)], "size": 800},
                "width": 1200,
                "height": 800,
                "orientation": HORIZONTAL,
            },
            "panels": {launcher_id: {"id": launcher_id, "params": {"kind": "launcher"}}},
            "activeGroup": "g1",
        },
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )

    added = add_panel(placeholder_only, _TERMINAL_1, _TAB_A, "Terminal 1", _placement(None))

    assert added.dockview is not None
    assert _leaves(added.dockview)[0]["data"]["views"] == [str(_TAB_A)]
    assert set(added.dockview["panels"]) == {str(_TAB_A)}


def test_a_split_along_the_branchs_axis_inserts_a_sibling_sharing_the_anchors_extent() -> None:
    layout = _two_groups_side_by_side()
    added = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pa", Direction.LEFT, ratio=0.25))
    assert added.dockview is not None
    root = added.dockview["grid"]["root"]
    assert [leaf["data"]["id"] for leaf in root["data"]] == ["g-new", "g1", "g2"]
    assert [leaf["size"] for leaf in root["data"]] == [150, 450, 600]
    assert root["data"][0]["data"]["views"] == [str(_TAB_C)]


def test_a_split_across_the_branchs_axis_wraps_the_anchor_in_a_new_branch() -> None:
    layout = _two_groups_side_by_side()
    added = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pb", Direction.BELOW, ratio=0.5))
    assert added.dockview is not None
    root = added.dockview["grid"]["root"]
    wrapper = root["data"][1]
    # The wrapper takes the anchor's width; its children split the root's height.
    assert wrapper["type"] == "branch" and wrapper["size"] == 600
    assert [child["data"]["id"] for child in wrapper["data"]] == ["g2", "g-new"]
    assert [child["size"] for child in wrapper["data"]] == [400, 400]
    assert added.dockview["activeGroup"] == "g-new"


def test_a_direction_with_a_neighbour_tabs_into_it_unless_a_new_group_is_asked_for() -> None:
    layout = _two_groups_side_by_side()
    tabbed = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pa", Direction.RIGHT))
    assert tabbed.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(tabbed.dockview)] == [["pa"], ["pb", str(_TAB_C)]]

    split = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pa", Direction.RIGHT, is_new_group=True))
    assert split.dockview is not None
    assert [leaf["data"]["id"] for leaf in _leaves(split.dockview)] == ["g1", "g-new", "g2"]


def test_a_neighbour_is_found_across_a_nested_branch_by_the_tree() -> None:
    layout = _two_groups_side_by_side()
    stacked = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pb", Direction.BELOW))
    # From the left group, "right" lands in the nearest group across the boundary: the top of the stack.
    landed = add_panel(stacked, Address("app:browser?instance=b"), _TAB_D, "B", _placement("pa", Direction.RIGHT))
    assert landed.dockview is not None
    leaves = _leaves(landed.dockview)
    assert leaves[1]["data"]["views"] == ["pb", str(_TAB_D)]
    # From the bottom of the stack, "above" is the group right above it; "left" is the left column.
    from_bottom = add_panel(
        stacked, Address("app:browser?instance=c"), _TAB_D, "C", _placement(str(_TAB_C), Direction.ABOVE)
    )
    assert from_bottom.dockview is not None
    assert _leaves(from_bottom.dockview)[1]["data"]["views"] == ["pb", str(_TAB_D)]
    leftward = add_panel(
        stacked, Address("app:browser?instance=d"), _TAB_D, "D", _placement(str(_TAB_C), Direction.LEFT)
    )
    assert leftward.dockview is not None
    assert _leaves(leftward.dockview)[0]["data"]["views"] == ["pa", str(_TAB_D)]


def test_removing_a_panel_collapses_its_group_and_the_wrapper_it_leaves_behind() -> None:
    stacked = add_panel(
        _two_groups_side_by_side(), _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pb", Direction.BELOW)
    )
    removed = remove_panel(stacked, str(_TAB_C))
    assert removed.dockview is not None
    root = removed.dockview["grid"]["root"]
    # The stack of one is flattened back into a leaf that keeps the wrapper's width.
    assert [child["type"] for child in root["data"]] == ["leaf", "leaf"]
    assert root["data"][1]["data"]["id"] == "g2" and root["data"][1]["size"] == 600
    assert removed.dockview["activeGroup"] == "g1"
    assert (
        set(addresses_by_panel_id(removed.dockview)) == {"pa", "pb"} and str(_TAB_C) not in removed.dockview["panels"]
    )

    last_two = remove_panel(removed, "pb")
    assert last_two.dockview is not None and [leaf["data"]["id"] for leaf in _leaves(last_two.dockview)] == ["g1"]
    gone = remove_panel(last_two, "pa")
    assert gone.dockview is None
    with pytest.raises(PanelNotFoundError):
        remove_panel(gone, "pa")


def test_a_document_without_a_grid_is_repaired_into_one_group_by_every_op() -> None:
    gridless = LayoutRecord(
        dockview={"panels": {"pa": _panel("pa", _FILES, _TAB_A), "pb": _panel("pb", _TERMINAL_1, _TAB_B)}},
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )
    focused = focus_panel(gridless, "pb")
    assert focused.dockview is not None
    assert [leaf["data"] for leaf in _leaves(focused.dockview)] == [
        {"views": ["pa", "pb"], "activeView": "pb", "id": "pb-repaired"}
    ]
    moved = move_panel(gridless, "pa", _placement("pb", Direction.BELOW))
    assert moved.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(moved.dockview)] == [["pb"], ["pa"]]
    removed = remove_panel(gridless, "pa")
    assert removed.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(removed.dockview)] == [["pb"]]
    assert set(addresses_by_panel_id(removed.dockview)) == {"pb"}
    # The panel an add docks lands beside the repaired group only, not in it as well.
    added = add_panel(gridless, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pb", Direction.BELOW))
    assert added.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(added.dockview)] == [["pa", "pb"], [str(_TAB_C)]]


def test_focus_marks_the_tab_and_its_group_active() -> None:
    layout = _two_groups_side_by_side()
    focused = focus_panel(layout, "pb")
    assert focused.dockview is not None
    assert focused.dockview["activeGroup"] == "g2"
    assert _leaves(focused.dockview)[1]["data"]["activeView"] == "pb"
    with pytest.raises(PanelNotFoundError):
        focus_panel(layout, "nope")


def test_move_relocates_a_panel_keeping_its_record_and_is_a_noop_within_its_own_group() -> None:
    layout = _two_groups_side_by_side()
    moved = move_panel(layout, "pa", _placement("pb", Direction.WITHIN))
    assert moved.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(moved.dockview)] == [["pb", "pa"]]
    assert addresses_by_panel_id(moved.dockview) == addresses_by_panel_id(layout.dockview)
    assert set(moved.dockview["panels"]) == {"pa", "pb"}
    assert move_panel(moved, "pa", _placement("pb", Direction.WITHIN)) == moved

    beside = move_panel(layout, "pa", _placement("pb", Direction.BELOW))
    assert beside.dockview is not None
    assert [leaf["data"]["id"] for leaf in _leaves(beside.dockview)] == ["g2", "g-new"]
    # A panel already in the anchor's neighbour group in that direction stays put, rather than being detached (which
    # removes its group) and docked into the group beyond it or into a fresh split.
    assert move_panel(layout, "pb", _placement("pa", Direction.RIGHT)) == layout
    three = add_panel(layout, _TERMINAL_2, _TAB_C, "Terminal 2", _placement("pb", Direction.RIGHT, is_new_group=True))
    assert move_panel(three, "pb", _placement("pa", Direction.RIGHT)) == three
    # The panel beyond that neighbour is moved into it.
    moved_in = move_panel(three, str(_TAB_C), _placement("pa", Direction.RIGHT))
    assert moved_in.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(moved_in.dockview)] == [["pa"], ["pb", str(_TAB_C)]]
    with pytest.raises(LayoutOpError):
        move_panel(layout, "pa", _placement("pa", Direction.WITHIN))
    with pytest.raises(PanelNotFoundError):
        move_panel(layout, "nope", _placement("pb", Direction.WITHIN))


def test_an_open_into_a_launcher_only_view_fills_the_pane_rather_than_splitting_beside_it() -> None:
    """What the browser saves after the last tab is closed: one group holding a New Tab launcher."""
    launcher_id = "new-tab-000000000000000f"
    launcher_only = LayoutRecord(
        dockview={
            "grid": {
                "root": {"type": "branch", "data": [_leaf("g1", launcher_id, size=1200)], "size": 800},
                "width": 1200,
                "height": 800,
                "orientation": HORIZONTAL,
            },
            "panels": {launcher_id: {"id": launcher_id, "params": {"kind": "launcher"}}},
            "activeGroup": "g1",
        },
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )
    # An agent's ``open`` with no requester docked: no anchor, to the right of the active group.
    opened = add_panel(launcher_only, _TERMINAL_1, _TAB_A, "Terminal 1", _placement(None, Direction.RIGHT))
    assert opened.dockview is not None
    assert [leaf["data"]["views"] for leaf in _leaves(opened.dockview)] == [[str(_TAB_A)]]
    assert set(opened.dockview["panels"]) == {str(_TAB_A)}
    assert opened.dockview["activeGroup"] == "g1"
