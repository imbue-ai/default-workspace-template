from typing import Any

import pytest
from app_manifest.primitives import AppName

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.ops import NavigateRequest
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceRequest
from workspace_layout.ops import ShowRequest
from workspace_layout.ops import WindowRequest
from workspace_layout.ops import navigate_op_arguments
from workspace_layout.ops import op_request_body
from workspace_layout.ops import open_op_arguments
from workspace_layout.ops import parse_layout_op
from workspace_layout.ops import parse_op_requester
from workspace_layout.ops import place_op_arguments
from workspace_layout.ops import show_op_arguments
from workspace_layout.ops import window_op_arguments
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


@pytest.mark.parametrize(
    ("op", "arguments", "expected"),
    [
        (
            LayoutOp.SHOW,
            show_op_arguments(
                ShowRequest(
                    app=AppName("chat"),
                    path=WindowPath("/?chat=agent-1"),
                    showing=(WindowPath("/agent-1"),),
                    repoint=(WindowPage("/"),),
                    client_id=_CLIENT,
                )
            ),
            {"app": "chat", "path": "/?chat=agent-1", "showing": ["/agent-1"], "repoint": ["/"], "client": "client-1"},
        ),
        (
            LayoutOp.SHOW,
            show_op_arguments(
                ShowRequest(app=AppName("files"), path=WindowPath("/a"), showing=(), repoint=(), client_id=None)
            ),
            {"app": "files", "path": "/a", "showing": [], "repoint": []},
        ),
        (
            LayoutOp.OPEN,
            open_op_arguments(
                OpenRequest(
                    app=AppName("getting-started"),
                    path=WindowPath("/"),
                    if_present=IfPresent.FOCUS,
                    is_minimized=False,
                    client_id=_CLIENT,
                    desktop="home",
                )
            ),
            {
                "app": "getting-started",
                "path": "/",
                "if_present": "focus",
                "minimized": False,
                "client": "client-1",
                "desktop": "home",
            },
        ),
        (
            LayoutOp.FOCUS,
            window_op_arguments(WindowRequest(window="self", client_id=None, desktop="Research")),
            {"window": "self", "desktop": "Research"},
        ),
        (
            LayoutOp.NAVIGATE,
            navigate_op_arguments(
                NavigateRequest(window="files", path=WindowPath("/b/"), client_id=_CLIENT, desktop=None)
            ),
            {"window": "files", "path": "/b/", "client": "client-1"},
        ),
        (
            LayoutOp.PLACE,
            place_op_arguments(
                PlaceRequest(
                    window="win-0123456789abcdef",
                    frame=Frame(x=0.0, y=0.0, width=0.5, height=1.0),
                    client_id=_CLIENT,
                    desktop="home",
                )
            ),
            {
                "window": "win-0123456789abcdef",
                "frame": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0},
                "client": "client-1",
                "desktop": "home",
            },
        ),
    ],
    ids=["show", "show-for-the-shells-choice-of-client", "open", "focus", "navigate", "place"],
)
def test_each_request_is_spelled_as_the_op_route_reads_it(
    op: LayoutOp, arguments: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Only what the request names goes on the wire, the target keys among it, and the shell's own reading of the
    body takes it."""
    assert arguments == expected
    body = op_request_body(op, arguments, OpRequester(app=AppName("chat"), marker=""))
    assert body == {"op": op, "args": expected, "requester": {"app": "chat", "marker": ""}}
    assert describe_op_body_problem(body) is None


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
        ({"op": "shortcut_move", "args": {"app": "files", "cell": "0,1"}}, "cell"),
    ],
)
def test_a_body_the_shell_would_refuse_is_named_with_the_reason(body: dict[str, Any], fragment: str) -> None:
    problem = describe_op_body_problem(body)
    assert problem is not None and fragment in problem
