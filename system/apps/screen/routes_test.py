"""The owner gate over the service's routes: a share admits a visitor to this origin, so every route that acts on
the Screen outside the lease refuses an identity whose ``owner`` is false, while the viewer's routes keep answering
one and a header-less (loopback) request keeps reading as the owner."""

import json
from typing import Final

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from mock_neko_test import FakeNeko
from screen.browser_routes import LAUNCH_BROWSER_PATH
from screen.proxy import NEKO_PROXY_PREFIX
from screen.service import ScreenService
from screen.watch import STOPPED_NOTIFICATION_PATH
from testing import start_service_stack

_OWNER: Final[dict[str, str]] = {
    "X-Imbue-Identity": json.dumps({"owner": True, "user_id": "u1", "email": "josh@imbue.com"})
}
_VISITOR: Final[dict[str, str]] = {
    "X-Imbue-Identity": json.dumps({"owner": False, "user_id": "u2", "email": "mia@example.com"})
}
_AGENT: Final[dict[str, str]] = {"X-Mngr-Agent-Id": "chat-a", "X-Mngr-Agent-Name": "Alice"}

# Every route a visitor may not call, as (method, the rule, a path under it). The gate answers before the view
# runs, so no request needs a body or a running Screen.
_OWNER_ONLY_REQUESTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("POST", "/api/run", "/api/run"),
    ("POST", "/api/control/acquire", "/api/control/acquire"),
    ("POST", "/api/control/release", "/api/control/release"),
    ("POST", "/api/control/handoff", "/api/control/handoff"),
    ("GET", "/api/screenshot", "/api/screenshot"),
    ("GET", "/api/windows", "/api/windows"),
    ("POST", "/api/windows/<window_id>/activate", "/api/windows/0x03600001/activate"),
    ("POST", "/api/clipboard/image", "/api/clipboard/image"),
    ("POST", LAUNCH_BROWSER_PATH, LAUNCH_BROWSER_PATH),
    ("POST", "/api/open-url", "/api/open-url"),
    ("GET", "/browsers", "/browsers"),
    ("POST", "/browsers", "/browsers"),
    ("DELETE", "/browsers/<raw_name>", "/browsers/browser-1"),
    ("POST", "/browsers/<raw_name>/stop", "/browsers/browser-1/stop"),
    ("POST", "/browsers/<raw_name>/start", "/browsers/browser-1/start"),
    ("POST", "/browsers/<raw_name>/close-tab", "/browsers/browser-1/close-tab"),
    ("POST", "/browsers/<raw_name>/pending-url/cancel", "/browsers/browser-1/pending-url/cancel"),
    ("GET", "/browsers/<raw_name>/attach", "/browsers/browser-1/attach"),
    ("POST", "/browsers/<raw_name>/acquire", "/browsers/browser-1/acquire"),
    ("POST", "/browsers/<raw_name>/release", "/browsers/browser-1/release"),
    ("POST", "/browsers/<raw_name>/handoff", "/browsers/browser-1/handoff"),
)

# The rules a visitor may call (the watcher's notification gates itself on loopback).
_OPEN_RULES: Final[frozenset[str]] = frozenset(
    {
        "/",
        "/assets/<path:filename>",
        APP_CONTRACT_ROUTE,
        CONTEXT_MENU_ROUTE,
        "/health",
        "/api/state",
        "/api/start",
        "/api/connect-info",
        "/api/ws",
        "/api/session",
        "/api/control/take",
        "/api/control/return",
        "/api/screen-size",
        "/api/input-seen",
        STOPPED_NOTIFICATION_PATH,
        f"{NEKO_PROXY_PREFIX}/<path:upstream_path>",
        f"{NEKO_PROXY_PREFIX}/api/ws",
    }
)


def _session(client: FlaskClient, headers: dict[str, str]) -> dict:
    answer = client.post("/api/session", headers=headers)
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()


def test_every_route_is_either_the_owners_or_open_to_a_visitor(app: Flask) -> None:
    rules = frozenset(rule.rule for rule in app.url_map.iter_rules())
    owner_only_rules = frozenset(rule for _, rule, _ in _OWNER_ONLY_REQUESTS)
    assert rules == _OPEN_RULES | owner_only_rules


@pytest.mark.parametrize(("method", "path"), [(method, path) for method, _, path in _OWNER_ONLY_REQUESTS])
def test_a_visitor_is_refused_on_an_owner_route(client: FlaskClient, method: str, path: str) -> None:
    answer = client.open(path, method=method, headers={**_VISITOR, **_AGENT})
    assert answer.status_code == 403
    body = answer.get_json()
    assert body["detail"] == body["error"] == "this route is the workspace owner's"


@pytest.mark.parametrize(("method", "path"), [(method, path) for method, _, path in _OWNER_ONLY_REQUESTS])
def test_the_owner_and_a_header_less_caller_pass_the_gate(client: FlaskClient, method: str, path: str) -> None:
    assert client.open(path, method=method, headers=_OWNER).status_code != 403
    assert client.open(path, method=method).status_code != 403


def test_a_visitor_uses_the_viewer_and_takes_control_through_the_lease(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    assert client.get("/", headers=_VISITOR).status_code == 200
    assert client.get("/health", headers=_VISITOR).status_code == 200
    assert client.get("/api/state", headers=_VISITOR).status_code == 200
    assert client.get("/api/connect-info", headers=_VISITOR).status_code == 200
    assert client.post("/api/start", headers=_VISITOR).status_code == 200
    start_service_stack(service)
    session = _session(client, _VISITOR)
    assert session["is_owner"] is False
    taken = client.post("/api/control/take", json={"session_id": session["session_id"]}, headers=_VISITOR)
    assert taken.status_code == 200
    assert taken.get_json()["controller"] == "human"
    assert (
        client.post("/api/input-seen", json={"session_id": session["session_id"]}, headers=_VISITOR).status_code == 204
    )
    sized = client.post(
        "/api/screen-size", json={"width": 1024, "height": 768, "session_id": session["session_id"]}, headers=_VISITOR
    )
    assert sized.get_json()["applied"] is True
    assert "size:1024x768" in fake_neko.calls
    returned = client.post("/api/control/return", json={"session_id": session["session_id"]}, headers=_VISITOR)
    assert returned.status_code == 200
    assert returned.get_json()["controller"] == "nobody"


def test_a_visitor_may_not_act_as_the_owners_session(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    owner = _session(client, _OWNER)
    visitor = _session(client, _VISITOR)
    assert (
        client.post("/api/control/take", json={"session_id": owner["session_id"]}, headers=_OWNER).status_code == 200
    )
    # The owner's id is in the state document every viewer reads.
    assert client.get("/api/state", headers=_VISITOR).get_json()["control"]["human_session_id"] == owner["session_id"]
    for path, body in (
        ("/api/control/return", {"session_id": owner["session_id"]}),
        ("/api/control/take", {"session_id": owner["session_id"]}),
        ("/api/input-seen", {"session_id": owner["session_id"]}),
        ("/api/screen-size", {"width": 1024, "height": 768, "session_id": owner["session_id"]}),
    ):
        refused = client.post(path, json=body, headers=_VISITOR)
        assert refused.status_code == 403, path
        assert refused.get_json()["detail"] == "this route is the workspace owner's"
    control = client.get("/api/state", headers=_VISITOR).get_json()["control"]
    assert control["controller"] == "human"
    assert control["human_session_id"] == owner["session_id"]
    assert not any(call.startswith("size:") for call in fake_neko.calls)
    # A visitor's own session and an unknown one are unaffected.
    assert (
        client.post("/api/input-seen", json={"session_id": visitor["session_id"]}, headers=_VISITOR).status_code == 204
    )
    assert client.post("/api/input-seen", json={"session_id": "no-such"}, headers=_VISITOR).status_code == 404


def test_a_size_report_with_no_session_is_the_owners_alone(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    refused = client.post("/api/screen-size", json={"width": 1024, "height": 768}, headers=_VISITOR)
    assert refused.status_code == 403
    assert not any(call.startswith("size:") for call in fake_neko.calls)
    applied = client.post("/api/screen-size", json={"width": 1024, "height": 768}, headers=_OWNER)
    assert applied.get_json()["applied"] is True
    assert client.post("/api/screen-size", json={"width": 1280, "height": 720}).get_json()["applied"] is True
    assert "size:1280x720" in fake_neko.calls
