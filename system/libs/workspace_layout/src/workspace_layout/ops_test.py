from typing import Any

import pytest
from app_manifest.primitives import AppName

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.ops import ContextBody
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import NavigateBody
from workspace_layout.ops import OpBody
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import OpenBody
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import PlaceBody
from workspace_layout.ops import RefreshAppArgs
from workspace_layout.ops import RefreshAppBody
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import ShowBody
from workspace_layout.ops import WindowArgs
from workspace_layout.ops import WindowOpBody
from workspace_layout.ops import op_request_body
from workspace_layout.ops import parse_layout_op
from workspace_layout.ops import parse_op_body
from workspace_layout.ops import parse_op_requester
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import IfPresent
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.records import Frame
from workspace_layout.testing import describe_op_body_problem

_CLIENT = ClientId("client-1")


def test_an_op_parses_from_its_wire_spelling_and_a_retired_or_malformed_one_is_refused() -> None:
    assert parse_layout_op("shortcut_set") is LayoutOp.SHORTCUT_SET
    assert parse_layout_op("reload_system_interface") is LayoutOp.RELOAD_SYSTEM_INTERFACE
    retired = ("inspect", "where", "views", "split", "move", "rename", "delete", "stop", "start", "replace-url")
    for malformed in (*retired, "SHOW", "", None, 7, ["show"]):
        assert parse_layout_op(malformed) is None, malformed


def test_a_requester_parses_from_an_app_and_marker_and_the_rest_is_refused() -> None:
    assert parse_op_requester(None) is None
    assert parse_op_requester("") is None
    keyed = OpRequester(app=AppName("files"), marker="agent-1")
    assert parse_op_requester({"app": "files", "marker": "agent-1"}) == keyed
    assert parse_op_requester({"app": "files"}) == OpRequester(app=AppName("files"), marker="")
    assert parse_op_requester({"app": "files", "marker": None}) == OpRequester(app=AppName("files"), marker="")
    with pytest.raises(InvalidLayoutValueError, match="marker"):
        parse_op_requester({"app": "files", "marker": 7})
    # The retired address spelling is refused like any other string.
    for malformed in (7, [], {"marker": "agent-1"}, {"app": 3}, {"app": "Bad Name"}, "app:files?instance=agent-1"):
        with pytest.raises(InvalidLayoutValueError, match="requester"):
            parse_op_requester(malformed)


_REQUESTER = OpRequester(app=AppName("chat"), marker="")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            ShowBody(
                args=ShowArgs(
                    app=AppName("chat"),
                    path=WindowPath("/?chat=agent-1"),
                    showing=(WindowPath("/agent-1"),),
                    repoint=(WindowPage("/"),),
                    client=_CLIENT,
                ),
                requester=_REQUESTER,
            ),
            {"app": "chat", "path": "/?chat=agent-1", "showing": ["/agent-1"], "repoint": ["/"], "client": "client-1"},
        ),
        (
            ShowBody(args=ShowArgs(app=AppName("files"), path=WindowPath("/a")), requester=_REQUESTER),
            {"app": "files", "path": "/a"},
        ),
        (
            OpenBody(
                args=OpenArgs(
                    app=AppName("getting-started"),
                    path=WindowPath("/"),
                    if_present=IfPresent.FOCUS,
                    client=_CLIENT,
                    desktop="home",
                ),
                requester=_REQUESTER,
            ),
            {"app": "getting-started", "path": "/", "if_present": "focus", "client": "client-1", "desktop": "home"},
        ),
        (
            WindowOpBody(op=LayoutOp.FOCUS, args=WindowArgs(window="self", desktop="Research"), requester=_REQUESTER),
            {"window": "self", "desktop": "Research"},
        ),
        (
            NavigateBody(
                args=NavigateArgs(window="files", path=WindowPath("/b/"), client=_CLIENT), requester=_REQUESTER
            ),
            {"window": "files", "path": "/b/", "client": "client-1"},
        ),
        (
            PlaceBody(
                args=PlaceArgs(
                    window="win-0123456789abcdef",
                    frame=Frame(x=0.0, y=0.0, width=0.5, height=1.0),
                    client=_CLIENT,
                    desktop="home",
                ),
                requester=_REQUESTER,
            ),
            {
                "window": "win-0123456789abcdef",
                "frame": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
                "client": "client-1",
                "desktop": "home",
            },
        ),
        (RefreshAppBody(args=RefreshAppArgs(app=AppName("files")), requester=_REQUESTER), {"app": "files"}),
    ],
    ids=["show", "show-for-the-shells-choice-of-client", "open", "focus", "navigate", "place", "refresh-an-app"],
)
def test_each_body_is_spelled_as_the_op_route_reads_it(body: OpBody, expected: dict[str, Any]) -> None:
    """Only the arguments the caller set go on the wire, and the shell's own reading of the body gives it back."""
    wire = op_request_body(body)
    assert wire == {"op": body.op.value, "args": expected, "requester": {"app": "chat", "marker": ""}}
    assert parse_op_body(wire) == body


def test_an_op_that_reads_no_arguments_ignores_whatever_it_is_sent() -> None:
    assert parse_op_body({"op": "context", "args": {"window": "self"}}) == ContextBody()


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"op": "split", "args": {}}, "Unknown layout op"),
        ({"op": "focus", "args": {"window": "self", "relative_to": "x"}}, "relative_to"),
        ({"op": "focus", "args": {"window": "self"}, "requester": "chat:agent-1"}, "requester"),
        ({"op": "focus", "args": {"window": "self", "client": 3}}, "client"),
        ({"op": "open", "args": {"app": "files", "if_present": "sometimes"}}, "if_present"),
        ({"op": "place", "args": {"window": "self", "zone": "left"}}, "zone"),
        ({"op": "place", "args": {"window": "self", "state": "NORMAL"}}, "a frame or a restore"),
        ({"op": "place", "args": {"window": "self", "frame": "0,0,0.5,1"}}, "frame"),
        ({"op": "place", "args": {"window": "self", "frame": {"x": 0.6, "y": 0, "width": 0.5, "height": 1}}}, "unit"),
        ({"op": "show", "args": {"app": "files", "path": "//elsewhere"}}, "single '/'"),
        ({"op": "show", "args": {"app": "files", "path": "/a", "repoint": ["/?x"]}}, "no query string"),
        ({"op": "shortcut_move", "args": {"app": "files", "launch": "new", "cell": "0,1"}}, "cell"),
        ({"op": "load", "args": {}}, "desktop"),
        ({"op": "load", "args": {"desktop": ""}}, "desktop"),
        ({"op": "focus", "args": {"window": "self", "desktop": ""}}, "desktop"),
        ({"op": "focus", "args": {"window": "", "desktop": "work"}}, "needs a window"),
        ({"op": "navigate", "args": {"window": "Bad Name", "path": "/a"}}, "is not a window"),
        ({"op": "open", "args": {"app": "files", "beside": ""}}, "beside"),
        ({"op": "open", "args": {"app": "files", "path": "/a", "launch": "new"}}, "not both"),
        ({"op": "open", "args": {"app": "files", "minimized": True, "beside": "self"}}, "not both"),
        ({"op": "refresh", "args": {"app": "files", "window": "self"}}, "window"),
        ({"op": "refresh", "args": {"app": "files", "client": "client-1"}}, "client"),
        ({"op": "refresh", "args": {}}, "window"),
        ({"op": "reload_system_interface", "args": {"window": "self"}}, "window"),
        ({"op": "focus", "args": "self"}, "args"),
    ],
)
def test_a_body_the_shell_would_refuse_is_named_with_the_reason(body: dict[str, Any], fragment: str) -> None:
    problem = describe_op_body_problem(body)
    assert problem is not None and fragment in problem
