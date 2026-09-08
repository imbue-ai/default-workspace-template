from typing import Any

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import TabRecord
from imbue.system_interface.shell.layout_ops import layout_inspect
from imbue.system_interface.shell.layouts import StoredLayout
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId

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
