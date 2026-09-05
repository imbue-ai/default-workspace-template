import time
from pathlib import Path
from typing import Any

from app_manifest.registry import read_registry

from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import Project
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.data_types import synthesized_single_instance
from imbue.system_interface.shell.layout_ops import LayoutMutex
from imbue.system_interface.shell.layout_ops import layout_inspect
from imbue.system_interface.shell.layout_ops import layout_list
from imbue.system_interface.shell.layout_ops import layout_views
from imbue.system_interface.shell.layout_ops import view_display_name
from imbue.system_interface.shell.layouts import StoredLayout
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry

_FILES = Address("app:files")
_TAB = TabId("tab-000000000000000a")


def _dockview() -> dict[str, Any]:
    return {
        "grid": {
            "root": {
                "type": "branch",
                "data": [
                    {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 50}},
                    {"type": "leaf", "data": {"views": ["p2"], "activeView": "p2", "size": 50}},
                ],
            },
            "orientation": "HORIZONTAL",
        },
        "panels": {"p1": {}, "p2": {}},
        "activeGroup": "g1",
    }


def _stored(client_id: str, view_id: str = "everything") -> StoredLayout:
    layout = LayoutRecord(
        dockview=_dockview(),
        tabs={"p1": TabRecord(address=_FILES, tab_id=_TAB, last_focused_ms=0)},
        device_kind=DeviceKind.DESKTOP,
        updated_at=None,
    )
    return StoredLayout(view_id=ViewId(view_id), client_id=ClientId(client_id), layout=layout)


def test_inspect_projects_the_grid_and_the_panels() -> None:
    summary = layout_inspect(_stored("c1").layout, {"app:files": "Files"})
    assert summary["active_panel"] == "g1"
    assert summary["panels"] == [{"address": "app:files", "tab_id": str(_TAB), "title": "Files"}]
    tree = summary["tree"]
    assert tree["type"] == "branch" and tree["arrangement"] == "row"
    first_leaf, second_leaf = tree["children"]
    assert first_leaf["panels"] == [{"address": "app:files", "tab_id": str(_TAB), "title": "Files", "active": True}]
    # A panel with no tab record (the launcher) is listed with no address.
    assert second_leaf["panels"][0]["address"] is None
    assert layout_inspect(None, {}) == {"active_panel": None, "panels": [], "tree": None}


def test_list_names_every_app_with_where_its_instances_are_docked(tmp_path: Path) -> None:
    rows = read_registry(
        write_registry(
            tmp_path / "apps.toml",
            registry_row_toml("files", "http://localhost:1"),
            registry_row_toml("hidden", "http://localhost:2", is_internal=True),
        )
    )
    entries = [
        AppInventoryEntry(
            row=row, is_running=True, instances=(synthesized_single_instance(row, True),), first_seen_at_by_key={}
        )
        for row in rows
    ]
    listing = layout_list(entries, [_stored("c1"), _stored("c2", "alpha"), _stored("c1", "alpha")])
    assert [app["name"] for app in listing] == ["files"]
    assert listing[0]["actions"] == [{"id": "open", "label": "Open Files"}]
    assert listing[0]["instances"] == [
        {"key": "", "address": "app:files", "title": "Files", "status": "idle", "docked_in": ["c1", "c2"]}
    ]


def test_views_and_display_names() -> None:
    project = Project(id=ProjectId("alpha"), name="Alpha", color="#111111", glyph=0, tabs=(_FILES,), shortcuts=())
    views = layout_views([project], [_FILES], {"alpha": [{"id": "c1", "device_kind": "desktop"}]})
    assert views == [
        {
            "id": "alpha",
            "name": "Alpha",
            "is_everything": False,
            "tabs": ["app:files"],
            "clients": [{"id": "c1", "device_kind": "desktop"}],
        },
        {"id": "everything", "name": "Everything", "is_everything": True, "tabs": ["app:files"], "clients": []},
    ]
    assert view_display_name("alpha", [project]) == "Alpha"
    assert view_display_name("everything", [project]) == "Everything"
    assert view_display_name("gone", [project]) == "gone"


def test_the_mutex_is_exclusive_until_released_or_expired() -> None:
    mutex = LayoutMutex(ttl_seconds=0.05)
    assert mutex.try_acquire("a", "open", {"address": "app:files"}) is None
    holder = mutex.try_acquire("b", "close", {})
    assert holder is not None and holder["agent_id"] == "a" and holder["operation"] == "open"
    mutex.release("b", "close")
    assert mutex.try_acquire("b", "close", {}) is not None
    mutex.release("a", "open")
    assert mutex.try_acquire("b", "close", {}) is None
    time.sleep(0.06)
    assert mutex.try_acquire("c", "focus", {}) is None
    assert mutex.retry_after_ms() == 50
