from typing import Any

import pytest
from app_manifest.primitives import AppName

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.ops import KNOWN_OPS
from workspace_layout.ops import NavigateRequest
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceRequest
from workspace_layout.ops import ShowRequest
from workspace_layout.ops import WindowRequest
from workspace_layout.ops import is_known_op
from workspace_layout.ops import navigate_op_arguments
from workspace_layout.ops import op_request_body
from workspace_layout.ops import open_op_arguments
from workspace_layout.ops import parse_op_requester
from workspace_layout.ops import place_op_arguments
from workspace_layout.ops import show_op_arguments
from workspace_layout.ops import window_op_arguments
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import IfPresent
from workspace_layout.testing import describe_op_body_problem

_CLIENT = ClientId("client-1")


def test_the_op_route_knows_the_desktop_verbs_and_nothing_else() -> None:
    assert {
        "context",
        "desktops",
        "list",
        "load",
        "open",
        "show",
        "focus",
        "place",
        "navigate",
        "refresh",
    } <= KNOWN_OPS
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
            "show",
            show_op_arguments(
                ShowRequest(
                    app=AppName("chat"),
                    path="/?chat=agent-1",
                    showing=("/agent-1",),
                    repoint=("/",),
                    client_id=_CLIENT,
                )
            ),
            {"app": "chat", "path": "/?chat=agent-1", "showing": ["/agent-1"], "repoint": ["/"], "client": "client-1"},
        ),
        (
            "show",
            show_op_arguments(ShowRequest(app=AppName("files"), path="/a", showing=(), repoint=(), client_id=None)),
            {"app": "files", "path": "/a", "showing": [], "repoint": []},
        ),
        (
            "open",
            open_op_arguments(
                OpenRequest(
                    app=AppName("getting-started"),
                    path="/",
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
            "focus",
            window_op_arguments(WindowRequest(window="self", client_id=None, desktop="Research")),
            {"window": "self", "desktop": "Research"},
        ),
        (
            "navigate",
            navigate_op_arguments(NavigateRequest(window="files", path="/b/", client_id=_CLIENT, desktop=None)),
            {"window": "files", "path": "/b/", "client": "client-1"},
        ),
        (
            "place",
            place_op_arguments(
                PlaceRequest(window="win-0123456789abcdef", frame="0,0,0.5,1", client_id=_CLIENT, desktop="home")
            ),
            {"window": "win-0123456789abcdef", "frame": "0,0,0.5,1", "client": "client-1", "desktop": "home"},
        ),
    ],
    ids=["show", "show-for-the-shells-choice-of-client", "open", "focus", "navigate", "place"],
)
def test_each_request_is_spelled_as_the_op_route_reads_it(
    op: str, arguments: dict[str, Any], expected: dict[str, Any]
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
        ({"op": "split", "args": {}}, "unknown op"),
        ({"op": "focus", "args": {"window": "self", "relative_to": "x"}}, "relative_to"),
        ({"op": "focus", "args": {"window": "self"}, "requester": "chat:agent-1"}, "requester"),
        ({"op": "focus", "args": {"window": "self", "client": 3}}, "client"),
        ({"op": "open", "args": {"app": "files", "if_present": "sometimes"}}, "if_present"),
    ],
)
def test_a_body_the_shell_would_refuse_is_named_with_the_reason(body: dict[str, Any], fragment: str) -> None:
    problem = describe_op_body_problem(body)
    assert problem is not None and fragment in problem
