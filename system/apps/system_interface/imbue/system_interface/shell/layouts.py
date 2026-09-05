"""Client layouts: one arrangement per view per client, plus a seed per device kind (contracts.md sections 6 and 7)."""

from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

LAYOUTS_DIRNAME: Final[str] = "layouts"
SEED_FILENAME_PREFIX: Final[str] = "seed."
LAYOUT_FILE_SUFFIX: Final[str] = ".json"


class StoredLayout(FrozenModel):
    """One layout file, with where it lives."""

    view_id: ViewId = Field(description="The view the layout arranges")
    client_id: ClientId = Field(description="The client that owns it")
    layout: LayoutRecord = Field(description="The arrangement")


@pure
def empty_layout(device_kind: DeviceKind) -> LayoutRecord:
    return LayoutRecord(dockview=None, tabs={}, device_kind=device_kind, updated_at=None)


@pure
def layout_wire_json(layout: LayoutRecord) -> dict[str, Any]:
    return layout.model_dump(mode="json")


@pure
def _pruned_grid_node(node: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """Drop ``panel_id`` from one grid node, or None when the node empties out."""
    node_type = node.get("type")
    if node_type == "leaf":
        data = node.get("data")
        if not isinstance(data, dict):
            return node
        views = [view for view in data.get("views", []) if view != panel_id]
        if not views:
            return None
        pruned_data = {**data, "views": views}
        if pruned_data.get("activeView") == panel_id:
            pruned_data["activeView"] = views[0]
        return {**node, "data": pruned_data}
    if node_type == "branch":
        children = node.get("data")
        if not isinstance(children, list):
            return node
        pruned_children = [
            pruned for pruned in (_pruned_grid_node(child, panel_id) for child in children) if pruned is not None
        ]
        if not pruned_children:
            return None
        return {**node, "data": pruned_children}
    return node


@pure
def strip_panel_from_dockview(dockview: dict[str, Any], panel_id: str) -> dict[str, Any] | None:
    """Remove one panel from a serialized dockview grid, or None when nothing is left.

    The panel leaves ``panels`` and whichever group holds it; a group that empties collapses
    away, and a grid that empties answers None so the caller can drop the arrangement outright.
    """
    panels = dockview.get("panels")
    pruned_panels = (
        {key: value for key, value in panels.items() if key != panel_id} if isinstance(panels, dict) else panels
    )
    if isinstance(pruned_panels, dict) and not pruned_panels:
        return None
    pruned: dict[str, Any] = {**dockview, "panels": pruned_panels}
    grid = dockview.get("grid")
    if isinstance(grid, dict):
        root = grid.get("root")
        pruned_root = _pruned_grid_node(root, panel_id) if isinstance(root, dict) else root
        if pruned_root is None:
            return None
        pruned["grid"] = {**grid, "root": pruned_root}
    return pruned


@pure
def strip_address_from_layout(layout: LayoutRecord, address: Address) -> LayoutRecord:
    """The layout without every panel showing ``address``; a grid that empties out leaves ``dockview`` None."""
    doomed_panel_ids = [panel_id for panel_id, tab in layout.tabs.items() if tab.address == address]
    if not doomed_panel_ids:
        return layout
    dockview = layout.dockview
    for panel_id in doomed_panel_ids:
        if dockview is not None:
            dockview = strip_panel_from_dockview(dockview, panel_id)
    tabs = {panel_id: tab for panel_id, tab in layout.tabs.items() if panel_id not in doomed_panel_ids}
    return layout.model_copy_update(
        to_update(layout.field_ref().dockview, dockview),
        to_update(layout.field_ref().tabs, tabs),
    )


@pure
def unreferenced_addresses(candidates: Sequence[Address], referenced: set[Address]) -> list[Address]:
    """The candidates no project tab set and no client layout references (the referenced-lifetime rule)."""
    return [address for address in candidates if address not in referenced]


class LayoutStore(MutableModel):
    """Reads and writes ``layouts/<view>/<client>.json`` and the seeds under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _layouts_dir(self) -> Path:
        return self.state_directory / LAYOUTS_DIRNAME

    def _client_path(self, view_id: str, client_id: str) -> Path:
        return self._layouts_dir() / view_id / f"{client_id}{LAYOUT_FILE_SUFFIX}"

    def _seed_path(self, view_id: str, device_kind: DeviceKind) -> Path:
        return self._layouts_dir() / view_id / f"{SEED_FILENAME_PREFIX}{device_kind.value}{LAYOUT_FILE_SUFFIX}"

    def _read_file(self, path: Path) -> LayoutRecord | None:
        raw = read_json_object(path)
        if raw is None:
            return None
        try:
            return LayoutRecord.model_validate(raw)
        except ValidationError as e:
            logger.warning("Ignored an unreadable layout at {}: {}", path, e.errors()[0]["msg"])
            return None

    def read_layout(self, view_id: str, client_id: str, device_kind: DeviceKind) -> LayoutRecord:
        """The client's own arrangement, else the seed for its device kind, else the empty layout."""
        with STATE_FILES_LOCK:
            own = self._read_file(self._client_path(view_id, client_id))
            if own is not None:
                return own
            seed = self._read_file(self._seed_path(view_id, device_kind))
            if seed is not None:
                return seed
        return empty_layout(device_kind)

    def save_layout(self, view_id: str, client_id: str, layout: LayoutRecord, now: datetime) -> LayoutRecord:
        """Write the client's arrangement and rewrite the seed for its device kind."""
        stamped = layout.model_copy_update(to_update(layout.field_ref().updated_at, now.astimezone(timezone.utc)))
        document = layout_wire_json(stamped)
        with STATE_FILES_LOCK:
            write_json_atomic(self._client_path(view_id, client_id), document)
            write_json_atomic(self._seed_path(view_id, layout.device_kind), document)
        return stamped

    def all_client_layouts(self) -> list[StoredLayout]:
        """Every client's arrangement of every view (seeds excluded)."""
        stored: list[StoredLayout] = []
        with STATE_FILES_LOCK:
            layouts_dir = self._layouts_dir()
            if not layouts_dir.is_dir():
                return []
            for view_dir in sorted(layouts_dir.iterdir()):
                if not view_dir.is_dir():
                    continue
                for path in sorted(view_dir.iterdir()):
                    if not path.name.endswith(LAYOUT_FILE_SUFFIX) or path.name.startswith(SEED_FILENAME_PREFIX):
                        continue
                    layout = self._read_file(path)
                    if layout is None:
                        continue
                    try:
                        stored.append(
                            StoredLayout(
                                view_id=ViewId(view_dir.name),
                                client_id=ClientId(path.name[: -len(LAYOUT_FILE_SUFFIX)]),
                                layout=layout,
                            )
                        )
                    except ValueError as e:
                        logger.warning("Skipped a layout file with an unusable name at {}: {}", path, e)
        return stored

    def referenced_addresses(self) -> set[Address]:
        return {tab.address for stored in self.all_client_layouts() for tab in stored.layout.tabs.values()}

    def find_tab(self, tab_id: TabId) -> list[tuple[StoredLayout, str]]:
        """Every (layout, panel id) whose tab record carries ``tab_id``."""
        found: list[tuple[StoredLayout, str]] = []
        for stored in self.all_client_layouts():
            for panel_id, tab in stored.layout.tabs.items():
                if tab.tab_id == tab_id:
                    found.append((stored, panel_id))
        return found

    def rebind_tab(self, tab_id: TabId, address: Address, now: datetime) -> list[StoredLayout]:
        """Point every tab record carrying ``tab_id`` at ``address``; returns the layouts rewritten."""
        rewritten: list[StoredLayout] = []
        with STATE_FILES_LOCK:
            for stored, panel_id in self.find_tab(tab_id):
                tab = stored.layout.tabs[panel_id]
                tabs = {
                    **stored.layout.tabs,
                    panel_id: tab.model_copy_update(to_update(tab.field_ref().address, address)),
                }
                layout = stored.layout.model_copy_update(to_update(stored.layout.field_ref().tabs, tabs))
                saved = self.save_layout(stored.view_id, stored.client_id, layout, now)
                rewritten.append(stored.model_copy_update(to_update(stored.field_ref().layout, saved)))
        return rewritten

    def remove_addresses_everywhere(self, addresses: Sequence[Address], now: datetime) -> list[StoredLayout]:
        """Strip the panels showing addresses no app lists any more from every client layout."""
        rewritten: list[StoredLayout] = []
        with STATE_FILES_LOCK:
            for stored in self.all_client_layouts():
                layout = stored.layout
                for address in addresses:
                    layout = strip_address_from_layout(layout, address)
                if layout is stored.layout:
                    continue
                saved = self.save_layout(stored.view_id, stored.client_id, layout, now)
                rewritten.append(stored.model_copy_update(to_update(stored.field_ref().layout, saved)))
        return rewritten

    def delete_client_layouts(self, client_id: ClientId) -> int:
        """Remove every layout file a client owns; returns how many went."""
        removed = 0
        with STATE_FILES_LOCK:
            layouts_dir = self._layouts_dir()
            if not layouts_dir.is_dir():
                return 0
            for view_dir in layouts_dir.iterdir():
                path = view_dir / f"{client_id}{LAYOUT_FILE_SUFFIX}"
                if path.is_file():
                    path.unlink()
                    removed += 1
        return removed

    def delete_view_layouts(self, view_id: str) -> None:
        """Remove every layout and seed of a deleted view."""
        with STATE_FILES_LOCK:
            view_dir = self._layouts_dir() / view_id
            if not view_dir.is_dir():
                return
            for path in view_dir.iterdir():
                if path.is_file():
                    path.unlink()
            view_dir.rmdir()
