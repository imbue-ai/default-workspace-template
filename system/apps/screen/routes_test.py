"""The owner gate over the service's routes: a share admits a visitor to this origin, so every route that acts on
the Screen outside the lease refuses an identity whose ``owner`` is false, while the viewer's routes, where the
lease arbitrates (a visitor in control pastes, drops files and cancels a held page), keep answering one and a
header-less (loopback) request keeps reading as the owner."""

import io
import json
from pathlib import Path
from typing import Final

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from mock_chromium_test import FakeCdpConnector
from mock_neko_test import FakeNeko
from screen.browser_routes import CANCEL_PENDING_URL_RULE
from screen.browser_routes import LAUNCH_BROWSER_PATH
from screen.desktop_files import MAX_DROP_BYTES
from screen.proxy import NEKO_PROXY_PREFIX
from screen.service import ScreenService
from screen.watch import STOPPED_NOTIFICATION_PATH
from testing import start_browser
from testing import start_service_stack
from workspace_layout.testing import LoopbackShell

_OWNER: Final[dict[str, str]] = {
    "X-Imbue-Identity": json.dumps({"owner": True, "user_id": "u1", "email": "josh@imbue.com"})
}
_VISITOR: Final[dict[str, str]] = {
    "X-Imbue-Identity": json.dumps({"owner": False, "user_id": "u2", "email": "mia@example.com"})
}
_AGENT: Final[dict[str, str]] = {"X-Mngr-Agent-Id": "chat-a", "X-Mngr-Agent-Name": "Alice"}
_PNG_HEADER: Final[bytes] = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

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
    ("POST", LAUNCH_BROWSER_PATH, LAUNCH_BROWSER_PATH),
    ("POST", "/api/open-url", "/api/open-url"),
    ("GET", "/browsers", "/browsers"),
    ("POST", "/browsers", "/browsers"),
    ("DELETE", "/browsers/<raw_name>", "/browsers/browser-1"),
    ("POST", "/browsers/<raw_name>/stop", "/browsers/browser-1/stop"),
    ("POST", "/browsers/<raw_name>/start", "/browsers/browser-1/start"),
    ("POST", "/browsers/<raw_name>/close-tab", "/browsers/browser-1/close-tab"),
    ("GET", "/browsers/<raw_name>/attach", "/browsers/browser-1/attach"),
    ("POST", "/browsers/<raw_name>/acquire", "/browsers/browser-1/acquire"),
    ("POST", "/browsers/<raw_name>/release", "/browsers/browser-1/release"),
    ("POST", "/browsers/<raw_name>/handoff", "/browsers/browser-1/handoff"),
)

# The rules a visitor may call: the viewer's, where the lease arbitrates (the watcher's notification gates itself
# on loopback).
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
        "/api/clipboard/image",
        "/api/drop",
        CANCEL_PENDING_URL_RULE,
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


def _drop(client: FlaskClient, headers: dict[str, str], session_id: str, *files: tuple[str, bytes]):
    return client.post(
        "/api/drop",
        data={"session_id": session_id, "files": [(io.BytesIO(content), name) for name, content in files]},
        content_type="multipart/form-data",
        headers=headers,
    )


def test_a_visitor_in_control_drops_files_into_the_desktop_folder(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    visitor = _session(client, _VISITOR)
    assert (
        client.post("/api/control/take", json={"session_id": visitor["session_id"]}, headers=_VISITOR).status_code
        == 200
    )
    answer = _drop(client, _VISITOR, visitor["session_id"], ("notes.txt", b"hello"), ("notes.txt", b"again"))
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json() == {"directory": str(service.desktop_dir), "saved": ["notes.txt", "notes-1.txt"]}
    assert (service.desktop_dir / "notes.txt").read_bytes() == b"hello"
    assert (service.desktop_dir / "notes-1.txt").read_bytes() == b"again"


def test_a_drop_needs_the_controlling_session_and_refuses_the_owners_session_to_a_visitor(
    client: FlaskClient, service: ScreenService
) -> None:
    start_service_stack(service)
    owner = _session(client, _OWNER)
    visitor = _session(client, _VISITOR)
    assert (
        client.post("/api/control/take", json={"session_id": owner["session_id"]}, headers=_OWNER).status_code == 200
    )
    as_owner = _drop(client, _VISITOR, owner["session_id"], ("notes.txt", b"hello"))
    assert as_owner.status_code == 403
    assert as_owner.get_json()["detail"] == "this route is the workspace owner's"
    not_controlling = _drop(client, _VISITOR, visitor["session_id"], ("notes.txt", b"hello"))
    assert not_controlling.status_code == 403
    assert not_controlling.get_json()["detail"] == "only the session in control of the Screen may do this"
    no_session = client.post(
        "/api/drop", data={"files": [(io.BytesIO(b"x"), "a.txt")]}, content_type="multipart/form-data"
    )
    assert no_session.status_code == 400
    assert not service.desktop_dir.exists()


def test_a_drop_refuses_bad_names_empty_drops_and_oversize_bodies(client: FlaskClient, service: ScreenService) -> None:
    assert _drop(client, _OWNER, "s1", ("a.txt", b"x")).status_code == 409
    start_service_stack(service)
    owner = _session(client, _OWNER)
    assert (
        client.post("/api/control/take", json={"session_id": owner["session_id"]}, headers=_OWNER).status_code == 200
    )
    for bad_name in ("../escape.txt", "dir/file.txt", ".hidden"):
        refused = _drop(client, _OWNER, owner["session_id"], (bad_name, b"x"))
        assert refused.status_code == 400, bad_name
        assert "invalid file name" in refused.get_json()["detail"]
    empty = client.post(
        "/api/drop", data={"session_id": owner["session_id"]}, content_type="multipart/form-data", headers=_OWNER
    )
    assert empty.status_code == 400
    too_large = client.post(
        "/api/drop",
        data={"session_id": owner["session_id"], "files": [(io.BytesIO(b"x"), "a.txt")]},
        content_type="multipart/form-data",
        headers=_OWNER,
        environ_overrides={"CONTENT_LENGTH": str(MAX_DROP_BYTES + 1)},
    )
    assert too_large.status_code == 413
    assert not service.desktop_dir.exists()


def _paste(client: FlaskClient, headers: dict[str, str], session_id: str):
    return client.post(
        "/api/clipboard/image",
        data={"session_id": session_id, "image": (io.BytesIO(_PNG_HEADER), "clip", "image/png")},
        content_type="multipart/form-data",
        headers=headers,
    )


def _hold_a_page(client: FlaskClient, service: ScreenService) -> str:
    """A browser with a page held for the human while an agent drives; answers the browser's name."""
    name = start_browser(client, service)
    assert client.post(f"/browsers/{name}/acquire", json={}, headers=_AGENT).get_json()["ok"] is True
    held = client.post("/api/open-url", json={"client_id": "c-link", "url": "http://localhost:3000/"})
    assert held.status_code == 200, held.get_json()
    assert client.get("/api/state").get_json()["chromium"]["pending_url"] == "http://localhost:3000/"
    return name


def test_a_visitor_in_control_pastes_an_image_and_may_cancel_a_held_page(
    client: FlaskClient, service: ScreenService, fake_xclip: Path, loopback_shell: LoopbackShell
) -> None:
    name = start_browser(client, service)
    visitor = _session(client, _VISITOR)
    assert (
        client.post("/api/control/take", json={"session_id": visitor["session_id"]}, headers=_VISITOR).status_code
        == 200
    )
    pasted = _paste(client, _VISITOR, visitor["session_id"])
    assert pasted.status_code == 204, pasted.get_json()
    assert (fake_xclip.parent / "xclip-record" / "set.bytes").read_bytes() == _PNG_HEADER
    # A page is held only while an agent drives, so a human in control finds none to drop; the verb is theirs anyway.
    cancelled = client.post(
        f"/browsers/{name}/pending-url/cancel", json={"session_id": visitor["session_id"]}, headers=_VISITOR
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    assert cancelled.get_json() == {"cancelled": False}


def test_a_visitor_who_does_not_control_gets_the_lease_refusal(
    client: FlaskClient,
    service: ScreenService,
    fake_xclip: Path,
    fake_cdp_connector: FakeCdpConnector,
    loopback_shell: LoopbackShell,
) -> None:
    name = _hold_a_page(client, service)
    visitor = _session(client, _VISITOR)
    owner = _session(client, _OWNER)
    refused_paste = _paste(client, _VISITOR, visitor["session_id"])
    assert refused_paste.status_code == 403
    assert refused_paste.get_json()["detail"] == "only the session in control of the Screen may do this"
    assert not (fake_xclip.parent / "xclip-record" / "set.bytes").exists()
    for body, query in (
        ({"session_id": visitor["session_id"]}, ""),
        ({}, f"?session_id={visitor['session_id']}"),
        ({}, ""),
    ):
        refused_cancel = client.post(f"/browsers/{name}/pending-url/cancel{query}", json=body, headers=_VISITOR)
        assert refused_cancel.status_code == 403, (body, query)
        assert refused_cancel.get_json()["detail"] == "only the session in control of the Screen may do this"
        assert refused_cancel.get_json()["error"] == refused_cancel.get_json()["detail"]
    # Naming the owner's session is the owner-gate refusal, as on every viewer route.
    as_owner = client.post(
        f"/browsers/{name}/pending-url/cancel", json={"session_id": owner["session_id"]}, headers=_VISITOR
    )
    assert as_owner.status_code == 403
    assert as_owner.get_json()["detail"] == "this route is the workspace owner's"
    assert _paste(client, _VISITOR, owner["session_id"]).get_json()["detail"] == "this route is the workspace owner's"
    assert client.get("/api/state").get_json()["chromium"]["pending_url"] == "http://localhost:3000/"
    assert fake_cdp_connector.clients[0].created == []


def test_the_owner_cancels_a_held_page_with_or_without_a_session(
    client: FlaskClient, service: ScreenService, loopback_shell: LoopbackShell
) -> None:
    name = _hold_a_page(client, service)
    # A header-less caller (an agent or the shell over loopback) carries no session and still may.
    assert client.post(f"/browsers/{name}/pending-url/cancel").get_json() == {"cancelled": True}
    assert (
        client.post("/api/open-url", json={"client_id": "c-link", "url": "http://localhost:3000/"}).status_code == 200
    )
    # The owner's own viewer names its session, which does not control while the agent drives.
    owner = _session(client, _OWNER)
    with_session = client.post(
        f"/browsers/{name}/pending-url/cancel", json={"session_id": owner["session_id"]}, headers=_OWNER
    )
    assert with_session.get_json() == {"cancelled": True}
    assert (
        client.post("/api/open-url", json={"client_id": "c-link", "url": "http://localhost:3000/"}).status_code == 200
    )
    by_query = client.post(f"/browsers/{name}/pending-url/cancel?session_id={owner['session_id']}", headers=_OWNER)
    assert by_query.get_json() == {"cancelled": True}
    # A paste, unlike the cancel, is the controller's alone: the owner's session is not in control.
    refused_paste = _paste(client, _OWNER, owner["session_id"])
    assert refused_paste.status_code == 403
    assert refused_paste.get_json()["detail"] == "only the session in control of the Screen may do this"


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
