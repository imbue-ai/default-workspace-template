"""Integration tests for the /api/inspiration/* endpoints.

Each test builds an `InspirationService` with a recording `broadcast`
callable and a per-test `response_dir` (a `tmp_path`), then passes it to
`create_application`, which stores it on the app's `SystemInterfaceState`
for the handlers to read. This exercises the publish-request / confirm /
abort / status handshake end-to-end through the Flask test client without
touching the real `/code/runtime/inspiration/` path -- and without
`unittest.mock` or runtime attribute patching.

The publish handshake writes a response file that the `/publish-inspiration`
skill polls. The tests below assert the file's contents (status, edited
fields, sanitized SVG), the WebSocket broadcaster events the modal keys on,
the loopback (403) guard, and the argument-injection validation on both the
request and confirm bodies.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import simple_websocket
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.serving import BaseWSGIServer

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.inspiration import InspirationService
from imbue.system_interface.inspiration import _RESPONSE_FILENAME
from imbue.system_interface.server import create_application
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

# A non-loopback client address; the loopback-guarded handlers must reject it.
_NON_LOOPBACK_ENVIRON = {"REMOTE_ADDR": "10.0.0.5"}

# A slug shared across the request/confirm handshake tests.
_SLUG = "slack-inbox"

# An SVG carrying every dangerous construct the backend must strip on confirm:
# a <script> element, an on* event-handler attribute, and a <foreignObject>.
_DIRTY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" onload="steal()">'
    '<script>fetch("//evil")</script>'
    "<foreignObject><div>x</div></foreignObject>"
    '<rect width="10" height="10" />'
    "</svg>"
)


class _RecordingBroadcaster:
    """Captures the dicts the service hands to `broadcast` for assertion.

    Mirrors the shape of `WebSocketBroadcaster.broadcast` (a
    `Callable[[dict[str, Any]], None]`) so it drops straight into the
    `InspirationService(broadcast=...)` seam without patching.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, message: dict[str, Any]) -> None:
        self.events.append(message)


@contextmanager
def _client(service: InspirationService) -> Iterator[FlaskClient]:
    """Build a Flask test client over an app wired with the given service."""
    app = create_application(inspiration_service=service)
    yield app.test_client()


# How long a WebSocket client waits for the next frame in the real-server
# tests. Frames arrive over loopback within milliseconds; the margin only
# matters on a heavily-loaded CI box.
_WS_RECEIVE_TIMEOUT_SECONDS = 5.0

# How long the no-replay assertions wait to prove no further frame arrives.
# Kept short: this bounds the runtime of every negative check.
_WS_QUIET_TIMEOUT_SECONDS = 0.5


def _build_production_wired_app(tmp_path: Path) -> Flask:
    """Build an app whose InspirationService broadcasts over the app's real broadcaster.

    The unit-style tests above inject a recording `broadcast` to assert on
    events without sockets; the real-server tests below instead need the
    production wiring (`InspirationService.broadcast` -> the same
    `WebSocketBroadcaster` the `/api/ws` connections drain), because they
    assert on frames arriving at a real WebSocket client. Only `response_dir`
    is redirected, into the test's `tmp_path`.
    """
    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    service = InspirationService(response_dir=tmp_path, broadcast=broadcaster.broadcast)
    return create_application(agent_manager=agent_manager, inspiration_service=service)


@contextmanager
def _real_server(app: Flask) -> Iterator[str]:
    """Serve ``app`` on a real loopback port; yield the ``http://...`` base URL.

    The threaded server matches production (`make_threaded_server` is what
    `main` runs), which is what makes real WebSocket connections -- and
    therefore the `/api/ws` connection counter and connect-time replay --
    testable end-to-end.
    """
    server: BaseWSGIServer = make_threaded_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def _connect_ws(base_url: str) -> simple_websocket.Client:
    """Open a real WebSocket connection to the app's ``/api/ws`` endpoint."""
    ws_url = base_url.replace("http://", "ws://") + "/api/ws"
    return simple_websocket.Client.connect(ws_url)


def _receive_event(ws: simple_websocket.Client) -> dict[str, Any] | None:
    """Receive and decode one JSON frame, or None if nothing arrives in time."""
    data = ws.receive(timeout=_WS_RECEIVE_TIMEOUT_SECONDS)
    if data is None:
        return None
    return json.loads(data)


def _receive_until_type(ws: simple_websocket.Client, event_type: str, max_frames: int = 10) -> dict[str, Any] | None:
    """Read frames until one of ``event_type`` arrives; None if it never does.

    Skips over the connect-time snapshot frames (``agents_updated``,
    ``applications_updated``, proto-agent replays) that precede the frame
    under test.
    """
    for _ in range(max_frames):
        event = _receive_event(ws)
        if event is None:
            return None
        if event["type"] == event_type:
            return event
    return None


def _build_service(tmp_path: Path) -> tuple[InspirationService, _RecordingBroadcaster]:
    """Construct an `InspirationService` writing into `tmp_path`, plus its recorder."""
    recorder = _RecordingBroadcaster()
    service = InspirationService(response_dir=tmp_path, broadcast=recorder)
    return service, recorder


def _request_body(**overrides: Any) -> dict[str, Any]:
    """A valid publish-request body, with per-test field overrides."""
    body: dict[str, Any] = {
        "slug": _SLUG,
        "title": "Slack Inbox",
        "description": "Checks your Slack inbox.",
        "repo_name": "slack-inbox",
        "visibility": "private",
        "thumbnail_svg": "<svg></svg>",
    }
    body.update(overrides)
    return body


def _confirm_body(**overrides: Any) -> dict[str, Any]:
    """A valid publish-confirm body, with per-test field overrides."""
    body: dict[str, Any] = {
        "slug": _SLUG,
        "title": "Slack Inbox Checker",
        "description": "Edited description.",
        "repo_name": "slack-inbox-checker",
        "visibility": "public",
        "thumbnail_svg": "<svg></svg>",
    }
    body.update(overrides)
    return body


def _read_response_file(tmp_path: Path) -> dict[str, Any]:
    """Read and parse the handshake response file the skill polls."""
    response_path = tmp_path / _RESPONSE_FILENAME
    return json.loads(response_path.read_text())


def test_publish_request_records_pending_and_broadcasts(tmp_path: Path) -> None:
    """publish-request stores the pending slug and broadcasts publish_requested.

    The broadcast is what opens the publish modal on the frontend, so the
    event type and every proposal field the modal prefills from must be
    present. `status` then reports the proposal as pending.
    """
    service, recorder = _build_service(tmp_path)
    with _client(service) as client:
        response = client.post("/api/inspiration/publish-request", json=_request_body())
        assert response.status_code == 200
        # The broadcast is fire-and-forget, so the reply reports how many
        # live /api/ws clients it could have reached. The Flask test client
        # carries no WebSocket connections, so the count is 0 here.
        assert response.get_json() == {"status": "ok", "ws_client_count": 0}

        status = client.get("/api/inspiration/status")
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload["pending_slug"] == _SLUG
    assert status_payload["has_pending"] is True

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event["type"] == "inspiration_publish_requested"
    assert event["slug"] == _SLUG
    assert event["title"] == "Slack Inbox"
    assert event["description"] == "Checks your Slack inbox."
    assert event["repo_name"] == "slack-inbox"
    assert event["visibility"] == "private"
    assert event["thumbnail_svg"] == "<svg></svg>"


def test_publish_confirm_writes_response_file_with_edited_values(tmp_path: Path) -> None:
    """publish-confirm writes the user's edited fields to the polled response file.

    The skill reads this file to learn the final repo name / visibility /
    title before it runs `gh repo create` + push, so the confirm reply and
    the on-disk file must both carry status "confirmed" and the edited
    values (not the originally-proposed ones).
    """
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        response = client.post("/api/inspiration/publish-confirm", json=_confirm_body())

    assert response.status_code == 200
    reply = response.get_json()
    assert reply["status"] == "confirmed"
    assert reply["slug"] == _SLUG
    assert reply["title"] == "Slack Inbox Checker"
    assert reply["description"] == "Edited description."
    assert reply["repo_name"] == "slack-inbox-checker"
    assert reply["visibility"] == "public"

    on_disk = _read_response_file(tmp_path)
    assert on_disk["status"] == "confirmed"
    assert on_disk["slug"] == _SLUG
    assert on_disk["title"] == "Slack Inbox Checker"
    assert on_disk["repo_name"] == "slack-inbox-checker"
    assert on_disk["visibility"] == "public"


def test_publish_confirm_clears_pending(tmp_path: Path) -> None:
    """After a confirm, status reports no pending proposal."""
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        client.post("/api/inspiration/publish-confirm", json=_confirm_body())
        status = client.get("/api/inspiration/status")
    status_payload = status.get_json()
    assert status_payload["pending_slug"] is None
    assert status_payload["has_pending"] is False


def test_publish_confirm_sanitizes_svg_before_writing(tmp_path: Path) -> None:
    """The untrusted SVG is stripped server-side before it reaches the skill.

    The value the skill commits comes from the response file, so the strip
    of <script>, on* handlers, and <foreignObject> must happen on confirm --
    before the file is written -- not only in the frontend preview. The
    benign <rect> is retained to prove sanitization is a strip, not a
    wholesale drop.
    """
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        response = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(thumbnail_svg=_DIRTY_SVG),
        )

    sanitized_reply = response.get_json()["thumbnail_svg"]
    sanitized_on_disk = _read_response_file(tmp_path)["thumbnail_svg"]
    for sanitized in (sanitized_reply, sanitized_on_disk):
        assert "<script" not in sanitized.lower()
        assert "onload" not in sanitized.lower()
        assert "foreignobject" not in sanitized.lower()
        assert "<rect" in sanitized.lower()


def test_abort_writes_aborted_response_and_broadcasts(tmp_path: Path) -> None:
    """abort unblocks the skill's poll with status "aborted" and closes the modal.

    The skill polls the same response file; an abort must write it (so the
    poll unblocks and the skill stops without pushing) and broadcast
    publish_aborted carrying the slug (so the frontend can slug-guard the
    modal close).
    """
    service, recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        response = client.post("/api/inspiration/abort", json={})
        assert response.status_code == 200
        status = client.get("/api/inspiration/status")

    on_disk = _read_response_file(tmp_path)
    assert on_disk["status"] == "aborted"
    assert on_disk["slug"] == _SLUG

    assert status.get_json()["has_pending"] is False

    abort_events = [e for e in recorder.events if e["type"] == "inspiration_publish_aborted"]
    assert len(abort_events) == 1
    assert abort_events[0]["slug"] == _SLUG


def test_status_reports_no_pending_before_any_request(tmp_path: Path) -> None:
    """With no proposal in flight, status reports nothing pending."""
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        status = client.get("/api/inspiration/status")
    status_payload = status.get_json()
    assert status_payload["pending_slug"] is None
    assert status_payload["has_pending"] is False


def test_publish_request_rejects_bad_repo_name(tmp_path: Path) -> None:
    """A repo_name that could inject into `gh repo create` is rejected 400.

    Both a leading '-' (argument injection) and an embedded space (outside
    ^[A-Za-z0-9._-]+$) must fail on the request body, and no proposal is
    recorded.
    """
    service, recorder = _build_service(tmp_path)
    with _client(service) as client:
        leading_dash = client.post(
            "/api/inspiration/publish-request",
            json=_request_body(repo_name="-foo"),
        )
        with_space = client.post(
            "/api/inspiration/publish-request",
            json=_request_body(repo_name="a b"),
        )
        status = client.get("/api/inspiration/status")

    assert leading_dash.status_code == 400
    assert with_space.status_code == 400
    assert status.get_json()["has_pending"] is False
    assert recorder.events == []


def test_publish_request_rejects_bad_visibility(tmp_path: Path) -> None:
    """A visibility outside {private, public} is rejected 400 on the request body."""
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        response = client.post(
            "/api/inspiration/publish-request",
            json=_request_body(visibility="secret"),
        )
    assert response.status_code == 400


def test_publish_confirm_rejects_bad_repo_name(tmp_path: Path) -> None:
    """The argument-injection guard also applies to the confirm body.

    A malicious repo_name must be rejected on confirm even though a valid
    request preceded it, and the response file must not be written.
    """
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        leading_dash = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(repo_name="-foo"),
        )
        with_space = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(repo_name="a b"),
        )
    assert leading_dash.status_code == 400
    assert with_space.status_code == 400
    assert not (tmp_path / _RESPONSE_FILENAME).exists()


def test_publish_confirm_rejects_bad_visibility(tmp_path: Path) -> None:
    """A visibility outside {private, public} is rejected 400 on the confirm body."""
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        response = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(visibility="secret"),
        )
    assert response.status_code == 400


def test_publish_confirm_rejects_slug_mismatch(tmp_path: Path) -> None:
    """Confirming a slug that isn't the pending one is an error.

    This guards against a stale modal confirming after a newer proposal
    superseded it; the mismatch must not write a confirmed response file
    for the wrong slug.
    """
    service, _recorder = _build_service(tmp_path)
    with _client(service) as client:
        client.post("/api/inspiration/publish-request", json=_request_body())
        response = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(slug="other-slug"),
        )
    assert response.status_code == 400
    assert not (tmp_path / _RESPONSE_FILENAME).exists()


def test_publish_request_reaches_connected_ws_client_and_counts_it(tmp_path: Path) -> None:
    """A live /api/ws client receives the broadcast, and the reply counts it.

    End-to-end over a real socket (threaded server + real WebSocket client),
    because this is exactly the path that silently failed in production: a
    200 OK publish-request whose broadcast never rendered a modal. The reply's
    `ws_client_count` must be 1 (one live client) and the client must receive
    the `inspiration_publish_requested` frame with the full proposal.
    """
    app = _build_production_wired_app(tmp_path)
    with _real_server(app) as base_url:
        ws = _connect_ws(base_url)
        try:
            # Wait for the connect-time snapshot: once it arrives, the server-side
            # handler is registered and counted, so the POST below cannot race it.
            first = _receive_event(ws)
            assert first is not None
            assert first["type"] == "agents_updated"

            response = httpx.post(f"{base_url}/api/inspiration/publish-request", json=_request_body())
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "ws_client_count": 1}

            event = _receive_until_type(ws, "inspiration_publish_requested")
            assert event is not None
            assert event["slug"] == _SLUG
            assert event["title"] == "Slack Inbox"
        finally:
            ws.close()


def test_publish_request_racing_ws_connect_still_reaches_client(tmp_path: Path) -> None:
    """A publish-request fired the instant a client connects still reaches it.

    Regression test for a race between `WsConnectionCounter.increment()` and
    `WebSocketBroadcaster.register()`: opening the socket alone is enough for
    the server to count and register the client, so a broadcast that lands
    before the connect-time snapshot frames are even read must still be
    delivered -- unlike `test_publish_request_reaches_connected_ws_client_and_counts_it`,
    this does not wait for the first `agents_updated` frame first, so it would
    have caught the client being counted (`ws_client_count: 1`) before it was
    actually registered with the broadcaster.
    """
    app = _build_production_wired_app(tmp_path)
    with _real_server(app) as base_url:
        ws = _connect_ws(base_url)
        try:
            response = httpx.post(f"{base_url}/api/inspiration/publish-request", json=_request_body())
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "ws_client_count": 1}

            event = _receive_until_type(ws, "inspiration_publish_requested")
            assert event is not None
            assert event["slug"] == _SLUG
            assert event["title"] == "Slack Inbox"
        finally:
            ws.close()


def test_pending_publish_request_is_replayed_to_late_connecting_ws_client(tmp_path: Path) -> None:
    """A proposal recorded with nobody listening reaches the next client to connect.

    The original broadcast is fire-and-forget; if no live client was connected
    (reply reports `ws_client_count` 0), the pending proposal must be replayed
    as part of the connect-time snapshot so the publish modal still opens as
    soon as a UI connects.
    """
    app = _build_production_wired_app(tmp_path)
    with _real_server(app) as base_url:
        response = httpx.post(f"{base_url}/api/inspiration/publish-request", json=_request_body())
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "ws_client_count": 0}

        ws = _connect_ws(base_url)
        try:
            event = _receive_until_type(ws, "inspiration_publish_requested")
            assert event is not None
            assert event["slug"] == _SLUG
            assert event["thumbnail_svg"] == "<svg></svg>"
        finally:
            ws.close()


def test_resolved_publish_request_is_not_replayed_on_connect(tmp_path: Path) -> None:
    """After an abort resolves the proposal, new connections get no replay."""
    app = _build_production_wired_app(tmp_path)
    with _real_server(app) as base_url:
        httpx.post(f"{base_url}/api/inspiration/publish-request", json=_request_body())
        httpx.post(f"{base_url}/api/inspiration/abort", json={})

        ws = _connect_ws(base_url)
        try:
            # Connect snapshot: agents_updated then applications_updated, then
            # nothing -- the resolved proposal must not be replayed.
            first = _receive_event(ws)
            second = _receive_event(ws)
            assert first is not None and first["type"] == "agents_updated"
            assert second is not None and second["type"] == "applications_updated"
            assert ws.receive(timeout=_WS_QUIET_TIMEOUT_SECONDS) is None
        finally:
            ws.close()


def test_all_inspiration_routes_reject_non_loopback(tmp_path: Path) -> None:
    """Every /api/inspiration/* route refuses non-loopback callers with 403.

    These routes handle publish proposals and trigger pushes, so they carry
    the same loopback guard as the layout-broadcast endpoint. Driving a
    non-loopback REMOTE_ADDR must 403 before any handler logic runs.
    """
    service, recorder = _build_service(tmp_path)
    with _client(service) as client:
        request_response = client.post(
            "/api/inspiration/publish-request",
            json=_request_body(),
            environ_base=_NON_LOOPBACK_ENVIRON,
        )
        confirm_response = client.post(
            "/api/inspiration/publish-confirm",
            json=_confirm_body(),
            environ_base=_NON_LOOPBACK_ENVIRON,
        )
        abort_response = client.post(
            "/api/inspiration/abort",
            json={},
            environ_base=_NON_LOOPBACK_ENVIRON,
        )
        status_response = client.get(
            "/api/inspiration/status",
            environ_base=_NON_LOOPBACK_ENVIRON,
        )

    assert request_response.status_code == 403
    assert confirm_response.status_code == 403
    assert abort_response.status_code == 403
    assert status_response.status_code == 403
    # A rejected non-loopback request must not have recorded a proposal.
    assert recorder.events == []
    assert not (tmp_path / _RESPONSE_FILENAME).exists()
