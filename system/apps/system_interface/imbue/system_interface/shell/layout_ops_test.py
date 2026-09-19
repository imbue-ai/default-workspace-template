from typing import Any

import pytest
from app_manifest.primitives import AppName

from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import instance_panel_params_json
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.layout_ops import is_desktop_op
from imbue.system_interface.shell.layout_ops import layout_inspect
from imbue.system_interface.shell.layout_ops import parse_op_requester
from imbue.system_interface.shell.layout_ops import requester_address
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
        "panels": {
            "p1": {"params": instance_panel_params_json(_FILES, _TAB, 0)},
            "p2": {"params": {"kind": "launcher"}},
        },
        "activeGroup": "g1",
    }


def _stored(client_id: str, view_id: str = "everything") -> StoredLayout:
    layout = LayoutRecord(dockview=_dockview(), device_kind=DeviceKind.DESKTOP, updated_at=None)
    return StoredLayout(view_id=ViewId(view_id), client_id=ClientId(client_id), layout=layout)


def test_inspect_projects_the_grid_and_the_panels() -> None:
    summary = layout_inspect(_stored("c1").layout, {"app:files": "Files"})
    assert summary["active_panel"] == "g1"
    assert summary["panels"] == [{"address": "app:files", "tab_id": str(_TAB), "title": "Files"}]
    tree = summary["tree"]
    assert tree["type"] == "branch" and tree["arrangement"] == "row"
    first_leaf, second_leaf = tree["children"]
    assert first_leaf["panels"] == [{"address": "app:files", "tab_id": str(_TAB), "title": "Files", "active": True}]
    # A panel whose params name no instance (the launcher) is listed with no address.
    assert second_leaf["panels"][0]["address"] is None
    assert layout_inspect(None, {}) == {"active_panel": None, "panels": [], "tree": None}


@pytest.mark.parametrize(
    ("op", "args", "is_desktop"),
    [
        # A name only the desktop vocabulary has is a desktop op whatever it carries.
        ("minimize", {"address": "app:files"}, True),
        ("shortcut_set", {}, True),
        ("desktops", {}, True),
        # The shared names are told apart by their arguments; ``address`` and ``view`` always win.
        ("open", {"app": "files"}, True),
        ("open", {"address": "app:files"}, False),
        ("open", {}, False),
        ("load", {"desktop": "home"}, True),
        ("load", {"view": "alpha"}, False),
        ("load", {}, False),
        ("refresh", {"window": "win-0000000000000001"}, True),
        ("refresh", {"app": "files"}, True),
        ("refresh", {"address": "app:files"}, False),
        ("refresh", {}, False),
        ("focus", {"window": "self"}, True),
        ("focus", {"window": "self", "address": "app:files"}, False),
        ("close", {"window": "files", "view": "alpha"}, False),
        ("close", {}, False),
        # An address-only verb never becomes a desktop op, even beside a ``window``.
        ("split", {"window": "self"}, False),
        ("inspect", {"desktop": "home"}, False),
    ],
)
def test_is_desktop_op_tells_the_vocabularies_apart_by_name_and_shape(
    op: str, args: dict[str, Any], is_desktop: bool
) -> None:
    assert is_desktop_op(op, args) is is_desktop


def test_a_requester_parses_from_an_address_or_an_app_and_marker_and_the_rest_is_refused() -> None:
    assert parse_op_requester(None) is None
    assert parse_op_requester("") is None
    keyed = parse_op_requester("app:files?instance=agent-1")
    assert keyed == OpRequester(app=AppName("files"), marker="agent-1")
    assert parse_op_requester("app:files") == OpRequester(app=AppName("files"), marker="")
    assert parse_op_requester({"app": "files", "marker": "agent-1"}) == keyed
    assert parse_op_requester({"app": "files"}) == OpRequester(app=AppName("files"), marker="")
    assert parse_op_requester({"app": "files", "marker": None}) == OpRequester(app=AppName("files"), marker="")
    with pytest.raises(LayoutOpError, match="marker"):
        parse_op_requester({"app": "files", "marker": 7})
    for malformed in (7, [], {"marker": "agent-1"}, {"app": 3}):
        with pytest.raises(LayoutOpError, match="requester"):
            parse_op_requester(malformed)
    assert str(requester_address(keyed)) == "app:files?instance=agent-1"
    assert str(requester_address(OpRequester(app=AppName("files"), marker=""))) == "app:files"
