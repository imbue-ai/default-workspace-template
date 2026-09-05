"""The pure editor over a client's serialized dockview document (contracts.md section 12).

A layout file holds dockview's own ``toJSON`` output plus one tab record per panel. Dockview's
grid is a tree: the root is a branch laid out along ``grid.orientation``, every nested branch
flips orientation, and a leaf is a group of tabs (``views``) with an ``activeView`` and an ``id``.
A node's ``size`` is its extent along its parent's axis and its cross extent is its parent's
``size``; dockview lays the tree out proportionally on load, so the numbers only need to be in
proportion. Placement follows the tree, never the screen: a direction finds the nearest enclosing
branch of the matching orientation and the sibling on that side.
"""

import copy
from enum import auto
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import PanelNotFoundError
from imbue.system_interface.shell.layouts import strip_panel_from_dockview
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import TabId

# A never-arranged view starts from a grid of this size; the browser rescales it to its window.
NOMINAL_ROOT_WIDTH: Final[int] = 1200
NOMINAL_ROOT_HEIGHT: Final[int] = 800

# The frontend's panel components, as its ``createComponent`` names them; a New Tab launcher's
# panel id carries the prefix and has no tab record.
INSTANCE_COMPONENT: Final[str] = "instance"
CUSTOM_TAB_COMPONENT: Final[str] = "custom"
LAUNCHER_PANEL_ID_PREFIX: Final[str] = "new-tab-"

HORIZONTAL: Final[str] = "HORIZONTAL"
VERTICAL: Final[str] = "VERTICAL"

# The share of the anchor a split gives the new panel when the op names none, and the bounds a
# requested share is held to: a group of (nearly) no extent could not be dragged open again.
DEFAULT_SPLIT_RATIO: Final[float] = 0.6
MIN_SPLIT_RATIO: Final[float] = 0.05
MAX_SPLIT_RATIO: Final[float] = 0.95

_GridPath = tuple[int, ...]


class Direction(LowerCaseStrEnum):
    """Where a split or a move puts a panel relative to its anchor (a wire value from ``layout.py``)."""

    LEFT = auto()
    RIGHT = auto()
    ABOVE = auto()
    BELOW = auto()
    WITHIN = auto()


class Placement(FrozenModel):
    """Where an op docks a panel: into an anchor's group, or beside it in a direction."""

    anchor_panel_id: str | None = Field(description="The panel to place relative to; None for the active group")
    direction: Direction | None = Field(description="None or WITHIN tabs into the anchor's group")
    ratio: float = Field(description="The share of the anchor's extent a split gives the new group")
    is_new_group: bool = Field(description="Split even when a group already lies in the direction")
    group_id: str = Field(description="The id a new group is created with")


@pure
def is_launcher_panel_id(panel_id: str) -> bool:
    return panel_id.startswith(LAUNCHER_PANEL_ID_PREFIX)


@pure
def panel_id_for_address(layout: LayoutRecord, address: Address) -> str | None:
    """The panel showing ``address``: exact, or any instance of the app for a bare app address."""
    for panel_id, tab in layout.tabs.items():
        if tab.address == address:
            return panel_id
    if address.key is not None:
        return None
    for panel_id, tab in layout.tabs.items():
        if tab.address.app == address.app:
            return panel_id
    return None


@pure
def _orthogonal(orientation: str) -> str:
    return VERTICAL if orientation == HORIZONTAL else HORIZONTAL


@pure
def _orientation_at_depth(root_orientation: str, depth: int) -> str:
    return root_orientation if depth % 2 == 0 else _orthogonal(root_orientation)


@pure
def _axis_of(direction: Direction) -> str:
    return HORIZONTAL if direction in (Direction.LEFT, Direction.RIGHT) else VERTICAL


@pure
def _is_before(direction: Direction) -> bool:
    return direction in (Direction.LEFT, Direction.ABOVE)


def _node_at(root: dict[str, Any], path: _GridPath) -> dict[str, Any]:
    node = root
    for index in path:
        node = node["data"][index]
    return node


def _leaf_paths(node: dict[str, Any], path: _GridPath) -> list[_GridPath]:
    if node.get("type") == "leaf":
        return [path]
    paths: list[_GridPath] = []
    for index, child in enumerate(node.get("data", []) or []):
        paths.extend(_leaf_paths(child, (*path, index)))
    return paths


def _leaf_path_for_panel(root: dict[str, Any], panel_id: str) -> _GridPath | None:
    for path in _leaf_paths(root, ()):
        if panel_id in (_node_at(root, path)["data"].get("views") or []):
            return path
    return None


def _edge_leaf_path(root: dict[str, Any], path: _GridPath, is_last: bool) -> _GridPath:
    """The first (or last) leaf under the node at ``path``, the one nearest an anchor across the boundary."""
    node = _node_at(root, path)
    while node.get("type") == "branch":
        children = node["data"]
        index = len(children) - 1 if is_last else 0
        path = (*path, index)
        node = children[index]
    return path


def _neighbor_leaf_path(
    root: dict[str, Any], root_orientation: str, path: _GridPath, direction: Direction
) -> _GridPath | None:
    """The group adjacent to the leaf at ``path`` in ``direction``, by the tree: the nearest enclosing branch of the
    matching orientation decides, and the sibling on that side is the neighbour."""
    axis = _axis_of(direction)
    current = path
    while current:
        parent_path = current[:-1]
        if _orientation_at_depth(root_orientation, len(parent_path)) == axis:
            neighbor_index = current[-1] - 1 if _is_before(direction) else current[-1] + 1
            siblings = _node_at(root, parent_path)["data"]
            if 0 <= neighbor_index < len(siblings):
                return _edge_leaf_path(root, (*parent_path, neighbor_index), is_last=_is_before(direction))
        current = parent_path
    return None


def _nominal_extent(orientation: str) -> int:
    return NOMINAL_ROOT_WIDTH if orientation == HORIZONTAL else NOMINAL_ROOT_HEIGHT


def _extent(node: dict[str, Any], orientation_of_parent: str) -> float:
    size = node.get("size")
    return float(size) if isinstance(size, (int, float)) else float(_nominal_extent(orientation_of_parent))


def _cross_extent(grid: dict[str, Any], parent: dict[str, Any], parent_orientation: str, is_root: bool) -> float:
    """A child's extent along the parent's cross axis: the parent's own size, or the grid's for the root."""
    size = parent.get("size")
    if isinstance(size, (int, float)):
        return float(size)
    if is_root:
        cross = grid.get("height") if parent_orientation == HORIZONTAL else grid.get("width")
        if isinstance(cross, (int, float)):
            return float(cross)
    return float(_nominal_extent(_orthogonal(parent_orientation)))


def _new_leaf(group_id: str, panel_id: str) -> dict[str, Any]:
    return {"type": "leaf", "data": {"views": [panel_id], "activeView": panel_id, "id": group_id}}


def _panel_entry(panel_id: str, address: Address, title: str) -> dict[str, Any]:
    return {
        "id": panel_id,
        "contentComponent": INSTANCE_COMPONENT,
        "tabComponent": CUSTOM_TAB_COMPONENT,
        "title": title,
        "params": {"kind": INSTANCE_COMPONENT, "address": str(address), "tabId": panel_id},
    }


def _empty_document(group_id: str, panel_id: str) -> dict[str, Any]:
    leaf = {**_new_leaf(group_id, panel_id), "size": NOMINAL_ROOT_WIDTH}
    return {
        "grid": {
            "root": {"type": "branch", "data": [leaf], "size": NOMINAL_ROOT_HEIGHT},
            "width": NOMINAL_ROOT_WIDTH,
            "height": NOMINAL_ROOT_HEIGHT,
            "orientation": HORIZONTAL,
        },
        "panels": {},
        "activeGroup": group_id,
    }


def _drop_launchers_from_leaf(document: dict[str, Any], leaf: dict[str, Any]) -> None:
    """A New Tab launcher is a question about an empty pane; docking into the pane answers it."""
    data = leaf["data"]
    launchers = [view for view in data.get("views", []) if is_launcher_panel_id(view)]
    if not launchers:
        return
    data["views"] = [view for view in data["views"] if view not in launchers]
    for launcher in launchers:
        document.get("panels", {}).pop(launcher, None)


def _flatten(node: dict[str, Any]) -> dict[str, Any]:
    """Collapse single-child branches a removal left behind. Only children are collapsed, never the node itself, so
    the root stays a branch as dockview requires."""
    if node.get("type") != "branch":
        return node
    children: list[dict[str, Any]] = []
    for child in node.get("data", []) or []:
        flattened = _flatten(child)
        grandchildren = flattened.get("data") if flattened.get("type") == "branch" else None
        if isinstance(grandchildren, list) and len(grandchildren) == 1:
            only = grandchildren[0]
            if only.get("type") == "leaf":
                # A group alone in its branch takes the branch's place and extent.
                children.append({**only, "size": flattened.get("size")})
                continue
            # A branch alone in a branch runs along this node's axis: its children join this one, sharing the
            # extent the wrapper had.
            inner = only.get("data") or []
            total = sum(_extent(grandchild, HORIZONTAL) for grandchild in inner) or 1.0
            wrapper_size = flattened.get("size")
            for grandchild in inner:
                share = _extent(grandchild, HORIZONTAL) / total
                children.append(
                    {**grandchild, "size": round(wrapper_size * share)}
                    if isinstance(wrapper_size, (int, float))
                    else grandchild
                )
            continue
        children.append(flattened)
    return {**node, "data": children}


def _repair_active_group(document: dict[str, Any]) -> None:
    root = document["grid"]["root"]
    group_ids = [
        _node_at(root, path)["data"].get("id")
        for path in _leaf_paths(root, ())
        if _node_at(root, path)["data"].get("id")
    ]
    if document.get("activeGroup") not in group_ids and group_ids:
        document["activeGroup"] = group_ids[0]


def _detach_panel_from_grid(document: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """The document without ``panel_id`` in its grid (its ``panels`` entry kept), or None when the grid empties."""
    entry = document.get("panels", {}).get(panel_id)
    stripped = strip_panel_from_dockview(document, panel_id)
    if stripped is None:
        return None
    if entry is not None:
        stripped["panels"] = {**stripped.get("panels", {}), panel_id: entry}
    stripped["grid"] = {**stripped["grid"], "root": _flatten(stripped["grid"]["root"])}
    _repair_active_group(stripped)
    return stripped


def _has_grid(document: dict[str, Any]) -> bool:
    grid = document.get("grid")
    if not isinstance(grid, dict):
        return False
    root = grid.get("root")
    return isinstance(root, dict) and root.get("type") == "branch" and isinstance(root.get("data"), list)


def _repair_grid(document: dict[str, Any], group_id: str) -> dict[str, Any]:
    """A document whose grid is missing or not dockview's shape gets one group holding every panel it names, so a
    damaged file costs an arrangement rather than every op on it."""
    panel_ids = [panel_id for panel_id in document.get("panels", {}) if not is_launcher_panel_id(panel_id)]
    repaired = _empty_document(group_id, panel_ids[0] if panel_ids else "")
    repaired["grid"]["root"]["data"][0]["data"]["views"] = panel_ids
    if not panel_ids:
        repaired["grid"]["root"]["data"][0]["data"].pop("activeView")
    repaired["panels"] = {panel_id: document["panels"][panel_id] for panel_id in panel_ids}
    return repaired


def _dock(document: dict[str, Any], panel_id: str, placement: Placement) -> dict[str, Any]:
    """Put ``panel_id`` (already in ``panels``) where ``placement`` says, and make it the active tab of its group."""
    if not _has_grid(document):
        document = _repair_grid(document, f"{placement.group_id}-repaired")
    grid = document["grid"]
    root = grid["root"]
    root_orientation = str(grid.get("orientation") or HORIZONTAL)
    if placement.anchor_panel_id is not None:
        anchor_path = _leaf_path_for_panel(root, placement.anchor_panel_id)
        if anchor_path is None:
            raise PanelNotFoundError(f"no panel {placement.anchor_panel_id!r} to place relative to")
    else:
        anchor_path = _active_leaf_path(document)
    direction = placement.direction
    if direction is None or direction is Direction.WITHIN:
        target_path = anchor_path
    elif (
        not placement.is_new_group
        and (neighbor := _neighbor_leaf_path(root, root_orientation, anchor_path, direction)) is not None
    ):
        target_path = neighbor
    else:
        target_path = _split_beside(grid, root_orientation, anchor_path, direction, placement)
    target = _node_at(root, target_path)
    _drop_launchers_from_leaf(document, target)
    if panel_id not in target["data"]["views"]:
        target["data"]["views"].append(panel_id)
    target["data"]["activeView"] = panel_id
    group_id = target["data"].get("id")
    if group_id:
        document["activeGroup"] = group_id
    return document


def _active_leaf_path(document: dict[str, Any]) -> _GridPath:
    root = document["grid"]["root"]
    paths = _leaf_paths(root, ())
    if not paths:
        raise LayoutOpError("the arrangement has no group to dock into")
    active_group = document.get("activeGroup")
    for path in paths:
        if _node_at(root, path)["data"].get("id") == active_group:
            return path
    return paths[0]


def _split_beside(
    grid: dict[str, Any], root_orientation: str, anchor_path: _GridPath, direction: Direction, placement: Placement
) -> _GridPath:
    """Insert a new group beside the anchor's, in a branch of the direction's axis (wrapping the anchor in one when its
    own branch runs the other way); answers the new leaf's path."""
    root = grid["root"]
    if not anchor_path:
        raise LayoutOpError("cannot split relative to the root")
    parent_path = anchor_path[:-1]
    parent = _node_at(root, parent_path)
    parent_orientation = _orientation_at_depth(root_orientation, len(parent_path))
    index = anchor_path[-1]
    anchor = parent["data"][index]
    new_leaf = _new_leaf(placement.group_id, "")
    new_leaf["data"]["views"] = []
    new_leaf["data"].pop("activeView")
    ratio = min(max(placement.ratio, MIN_SPLIT_RATIO), MAX_SPLIT_RATIO)
    if _axis_of(direction) == parent_orientation:
        anchor_extent = _extent(anchor, parent_orientation)
        new_extent = round(anchor_extent * ratio)
        anchor["size"] = anchor_extent - new_extent
        new_leaf["size"] = new_extent
        insert_at = index if _is_before(direction) else index + 1
        parent["data"].insert(insert_at, new_leaf)
        return (*parent_path, insert_at)
    cross = _cross_extent(grid, parent, parent_orientation, is_root=len(parent_path) == 0)
    new_extent = round(cross * ratio)
    anchor_in_wrapper = {**anchor, "size": cross - new_extent}
    new_leaf["size"] = new_extent
    children = [new_leaf, anchor_in_wrapper] if _is_before(direction) else [anchor_in_wrapper, new_leaf]
    parent["data"][index] = {
        "type": "branch",
        "size": anchor.get("size", _nominal_extent(parent_orientation)),
        "data": children,
    }
    return (*anchor_path, 0 if _is_before(direction) else 1)


@pure
def add_panel(layout: LayoutRecord, address: Address, tab_id: TabId, title: str, placement: Placement) -> LayoutRecord:
    """The layout with a new panel (its id the tab id) showing ``address``, docked per ``placement`` and focused."""
    panel_id = str(tab_id)
    document = (
        copy.deepcopy(layout.dockview)
        if layout.dockview is not None
        else _empty_document(placement.group_id, panel_id)
    )
    document.setdefault("panels", {})[panel_id] = _panel_entry(panel_id, address, title)
    if layout.dockview is None:
        docked = document
    else:
        docked = _dock(document, panel_id, placement)
    tabs = {**layout.tabs, panel_id: TabRecord(address=address, tab_id=tab_id, last_focused_ms=0)}
    return layout.model_copy_update(
        to_update(layout.field_ref().dockview, docked),
        to_update(layout.field_ref().tabs, tabs),
    )


@pure
def remove_panel(layout: LayoutRecord, panel_id: str) -> LayoutRecord:
    """The layout without ``panel_id``; a grid that empties leaves ``dockview`` None."""
    if layout.dockview is None or panel_id not in layout.tabs:
        raise PanelNotFoundError(f"no panel {panel_id!r} in the arrangement")
    detached = _detach_panel_from_grid(copy.deepcopy(layout.dockview), panel_id)
    if detached is not None:
        detached["panels"].pop(panel_id, None)
        if not detached["panels"]:
            detached = None
    tabs = {other: tab for other, tab in layout.tabs.items() if other != panel_id}
    return layout.model_copy_update(
        to_update(layout.field_ref().dockview, detached),
        to_update(layout.field_ref().tabs, tabs),
    )


@pure
def focus_panel(layout: LayoutRecord, panel_id: str) -> LayoutRecord:
    """The layout with ``panel_id`` the active tab of its group and that group the active one."""
    if layout.dockview is None:
        raise PanelNotFoundError(f"no panel {panel_id!r} in the arrangement")
    document = copy.deepcopy(layout.dockview)
    path = _leaf_path_for_panel(document["grid"]["root"], panel_id)
    if path is None:
        raise PanelNotFoundError(f"no panel {panel_id!r} in the arrangement")
    leaf = _node_at(document["grid"]["root"], path)
    leaf["data"]["activeView"] = panel_id
    if leaf["data"].get("id"):
        document["activeGroup"] = leaf["data"]["id"]
    return layout.model_copy_update(to_update(layout.field_ref().dockview, document))


@pure
def move_panel(layout: LayoutRecord, panel_id: str, placement: Placement) -> LayoutRecord:
    """The layout with ``panel_id`` taken out of its group and docked per ``placement`` (its page and tab record kept)."""
    if layout.dockview is None or panel_id not in layout.tabs:
        raise PanelNotFoundError(f"no panel {panel_id!r} in the arrangement")
    if placement.anchor_panel_id == panel_id:
        raise LayoutOpError(f"cannot move {panel_id!r} relative to itself")
    root = layout.dockview["grid"]["root"]
    if (
        placement.anchor_panel_id is not None
        and (placement.direction is None or placement.direction is Direction.WITHIN)
        and _leaf_path_for_panel(root, panel_id) == _leaf_path_for_panel(root, placement.anchor_panel_id)
    ):
        return layout
    detached = _detach_panel_from_grid(copy.deepcopy(layout.dockview), panel_id)
    if detached is None:
        # The moved panel was the only one: nothing is left to place it relative to.
        return layout
    return layout.model_copy_update(to_update(layout.field_ref().dockview, _dock(detached, panel_id, placement)))
