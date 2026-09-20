import pytest
from app_manifest.primitives import AppName

from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.layout_ops import KNOWN_OPS
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.layout_ops import is_known_op
from imbue.system_interface.shell.layout_ops import parse_op_requester


def test_the_op_route_knows_the_desktop_verbs_and_nothing_else() -> None:
    assert {"context", "desktops", "list", "load", "open", "focus", "place", "navigate", "refresh"} <= KNOWN_OPS
    assert {"shortcuts", "shortcut_set", "shortcut_move", "shortcut_remove", "wallpaper"} <= KNOWN_OPS
    for retired in ("inspect", "where", "views", "split", "move", "rename", "delete", "stop", "start", "replace-url"):
        assert not is_known_op(retired), retired


def test_a_requester_parses_from_an_app_and_marker_and_the_rest_is_refused() -> None:
    assert parse_op_requester(None) is None
    assert parse_op_requester("") is None
    keyed = OpRequester(app=AppName("files"), marker="agent-1")
    assert parse_op_requester({"app": "files", "marker": "agent-1"}) == keyed
    assert parse_op_requester({"app": "files"}) == OpRequester(app=AppName("files"), marker="")
    assert parse_op_requester({"app": "files", "marker": None}) == OpRequester(app=AppName("files"), marker="")
    with pytest.raises(LayoutOpError, match="marker"):
        parse_op_requester({"app": "files", "marker": 7})
    # The address spelling of the tabbed shell is refused like any other string.
    for malformed in (7, [], {"marker": "agent-1"}, {"app": 3}, {"app": "Bad Name"}, "app:files?instance=agent-1"):
        with pytest.raises(LayoutOpError, match="requester"):
            parse_op_requester(malformed)
