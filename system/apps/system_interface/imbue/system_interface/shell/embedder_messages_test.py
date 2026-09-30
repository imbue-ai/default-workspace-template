"""The embedder-message relay (desktop contracts.md section 5.6): the shell posts a message from the Imbue Studio chrome to
every app whose row registered its type, over real loopback servers standing in for the apps."""

from pathlib import Path
from typing import Any

from flask import Flask
from flask import jsonify
from flask.testing import FlaskClient

from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import message_handling_app
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_FOCUS_CHAT = "minds:focus-chat"
_HANDLER_PATH = "/api/focus-chat"


def _relay_client(tmp_path: Path, broadcaster: WebSocketBroadcaster, *rows: str) -> FlaskClient:
    inventory = build_inventory(write_two_app_registry(tmp_path, *rows), broadcaster)
    return shell_application(tmp_path, inventory, broadcaster).test_client()


def _relay(client: FlaskClient, body: dict[str, Any]) -> Any:
    return client.post("/api/embedder-messages", json=body)


def test_a_message_is_posted_to_every_app_that_registered_its_type_with_the_client_and_the_payload(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    focus_received: list[dict[str, Any]] = []
    other_received: list[dict[str, Any]] = []
    with (
        serve_app(message_handling_app(focus_received, _HANDLER_PATH, 200)) as focus_app,
        serve_app(message_handling_app(other_received, "/api/closed", 200)) as other_app,
    ):
        client = _relay_client(
            tmp_path,
            broadcaster,
            registry_row_toml("buddy", focus_app.http_url, message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]),
            registry_row_toml("pal", other_app.http_url, message_handlers=[("minds:close-active-tab", "/api/closed")]),
        )

        relayed = _relay(client, {"type": _FOCUS_CHAT, "client_id": "c1", "payload": {"chatId": "agent-1"}})

    assert relayed.status_code == 200
    assert relayed.get_json() == {"type": _FOCUS_CHAT, "deliveries": [{"app": "buddy", "status": 200, "detail": ""}]}
    assert focus_received == [{"type": _FOCUS_CHAT, "client_id": "c1", "chatId": "agent-1"}]
    assert other_received == []


def test_a_message_no_app_registered_is_refused_and_posted_nowhere(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    with serve_app(message_handling_app(received, _HANDLER_PATH, 200)) as focus_app:
        client = _relay_client(
            tmp_path,
            broadcaster,
            registry_row_toml("buddy", focus_app.http_url, message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]),
        )

        refused = _relay(client, {"type": "minds:close-active-tab", "client_id": "c1", "payload": {}})

    assert refused.status_code == 404
    assert "minds:close-active-tab" in refused.get_json()["detail"]
    assert received == []


def test_an_app_that_refuses_or_cannot_be_reached_is_reported_and_the_others_still_get_the_message(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    taken: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    with (
        serve_app(message_handling_app(taken, _HANDLER_PATH, 204)) as taking_app,
        serve_app(message_handling_app(refused, _HANDLER_PATH, 500)) as refusing_app,
    ):
        client = _relay_client(
            tmp_path,
            broadcaster,
            registry_row_toml(
                "down", f"http://127.0.0.1:{find_free_port()}", message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]
            ),
            registry_row_toml("refusing", refusing_app.http_url, message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]),
            registry_row_toml("taking", taking_app.http_url, message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]),
        )

        relayed = _relay(client, {"type": _FOCUS_CHAT, "client_id": "c1", "payload": {"chatId": "agent-1"}})

    assert relayed.status_code == 502
    assert "down did not take it" in relayed.get_json()["detail"]
    assert "refusing did not take it" in relayed.get_json()["detail"]
    assert "taking" not in relayed.get_json()["detail"]
    deliveries = {delivery["app"]: delivery for delivery in relayed.get_json()["deliveries"]}
    assert set(deliveries) == {"down", "refusing", "taking"}
    assert deliveries["down"]["status"] is None and deliveries["down"]["detail"] != ""
    assert deliveries["refusing"]["status"] == 500
    assert (deliveries["taking"]["status"], deliveries["taking"]["detail"]) == (204, "")
    assert len(taken) == 1 and len(refused) == 1


def test_a_relay_body_the_shell_cannot_forward_is_a_400(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    client = _relay_client(
        tmp_path,
        broadcaster,
        registry_row_toml("buddy", "http://127.0.0.1:1", message_handlers=[(_FOCUS_CHAT, _HANDLER_PATH)]),
    )
    for body, fragment in (
        ({"type": "focus-chat", "client_id": "c1", "payload": {}}, "message type"),
        ({"type": _FOCUS_CHAT, "client_id": "not a client", "payload": {}}, "client id"),
        ({"type": _FOCUS_CHAT, "client_id": "c1", "payload": {"client_id": "c2"}}, "client_id"),
        ({"type": _FOCUS_CHAT, "client_id": "c1", "payload": {"type": "minds:other"}}, "type"),
    ):
        answer = _relay(client, body)
        assert answer.status_code == 400, body
        assert fragment in answer.get_json()["detail"], body


_OPEN_FILE = "open:file"
_VIEWER_ROW_NAME = "viewer"
_VIEWER_SHOW = "{path}?view"
_VIEWER_SHOWING = ("{path}", "{path}/", "{path}/?view")
_CLIENT_ID = "c-show-5f2a"


def _viewer_row(*extra_handlers: tuple[str, str]) -> str:
    return registry_row_toml(
        _VIEWER_ROW_NAME,
        "http://127.0.0.1:1",
        message_handlers=extra_handlers,
        shown_message_handlers=[(_OPEN_FILE, _VIEWER_SHOW, _VIEWER_SHOWING)],
    )


def _arrived_client(tmp_path: Path, broadcaster: WebSocketBroadcaster, *rows: str) -> FlaskClient:
    """A relay client over ``rows`` whose client ``_CLIENT_ID`` has arrived, so the shell knows its desktop."""
    client = _relay_client(tmp_path, broadcaster, *rows)
    assert client.post(f"/api/clients/{_CLIENT_ID}/arrive").status_code == 200
    return client


def _viewer_windows(client: FlaskClient) -> list[dict[str, Any]]:
    (desktop,) = client.get("/api/desktops").get_json()["desktops"]
    return [window for window in desktop["windows"] if window["app"] == _VIEWER_ROW_NAME]


def test_a_show_handler_opens_the_page_it_builds_from_the_message_and_raises_it_the_second_time(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client = _arrived_client(tmp_path, broadcaster, _viewer_row())
    body = {"type": _OPEN_FILE, "client_id": _CLIENT_ID, "payload": {"path": "/home/user/my notes/a?b#c é.md"}}

    first = _relay(client, {**body, "sender": "chat"})
    second = _relay(client, body)

    page = "/home/user/my%20notes/a%3Fb%23c%20%C3%A9.md?view"
    (window,) = _viewer_windows(client)
    assert window["path"] == page
    assert first.status_code == 200
    assert first.get_json() == {
        "type": _OPEN_FILE,
        "deliveries": [
            {"app": _VIEWER_ROW_NAME, "status": 200, "detail": "", "shown": "opened", "window_id": window["id"]}
        ],
    }
    assert second.get_json()["deliveries"][0]["shown"] == "raised"
    assert second.get_json()["deliveries"][0]["window_id"] == window["id"]


def test_a_window_on_a_showing_page_counts_as_already_showing_the_message(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client = _arrived_client(tmp_path, broadcaster, _viewer_row())
    opened = client.post(
        "/api/desktops/home/windows",
        json={"app": _VIEWER_ROW_NAME, "path": "/data/q4/", "client_id": _CLIENT_ID, "if_present": "new"},
    )
    assert opened.status_code == 201

    relayed = _relay(client, {"type": _OPEN_FILE, "client_id": _CLIENT_ID, "payload": {"path": "/data/q4"}})

    assert relayed.get_json()["deliveries"][0]["shown"] == "raised"
    assert [window["path"] for window in _viewer_windows(client)] == ["/data/q4/"]


def test_a_payload_that_cannot_fill_a_show_handler_is_that_handlers_refusal_and_a_path_handler_still_gets_it(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    received: list[dict[str, Any]] = []
    with serve_app(message_handling_app(received, "/api/files-opened", 200)) as logging_app:
        client = _arrived_client(
            tmp_path,
            broadcaster,
            _viewer_row(),
            registry_row_toml("logger", logging_app.http_url, message_handlers=[(_OPEN_FILE, "/api/files-opened")]),
        )

        relayed = _relay(client, {"type": _OPEN_FILE, "client_id": _CLIENT_ID, "payload": {"file": "/x.md"}})

    assert relayed.status_code == 502
    deliveries = {delivery["app"]: delivery for delivery in relayed.get_json()["deliveries"]}
    assert deliveries["logger"] == {"app": "logger", "status": 200, "detail": ""}
    assert deliveries[_VIEWER_ROW_NAME]["status"] is None
    assert "'path' field is missing" in deliveries[_VIEWER_ROW_NAME]["detail"]
    assert f"{_VIEWER_ROW_NAME} did not take it" in relayed.get_json()["detail"]
    assert received == [{"type": _OPEN_FILE, "client_id": _CLIENT_ID, "file": "/x.md"}]
    assert _viewer_windows(client) == []


def test_a_show_for_a_client_the_shell_does_not_know_is_reported_as_the_apps_failed_delivery(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    client = _relay_client(tmp_path, broadcaster, _viewer_row())

    relayed = _relay(client, {"type": _OPEN_FILE, "client_id": "c-never-arrived", "payload": {"path": "/x.md"}})

    assert relayed.status_code == 502
    (delivery,) = relayed.get_json()["deliveries"]
    assert delivery["status"] is None and "c-never-arrived" in delivery["detail"]


def test_a_sender_that_is_not_an_app_name_is_a_400(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    client = _relay_client(tmp_path, broadcaster, _viewer_row())

    answer = _relay(client, {"type": _OPEN_FILE, "client_id": "c1", "payload": {"path": "/x"}, "sender": "Not An App"})

    assert answer.status_code == 400



def test_an_app_that_refuses_with_a_json_detail_is_reported_in_its_own_words(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    refusing = Flask("refusing")
    refusing.add_url_rule(
        "/api/open-url",
        view_func=lambda: (jsonify({"detail": "Chromium is not installed yet"}), 503),
        methods=["POST"],
    )
    with serve_app(refusing) as refusing_app:
        client = _relay_client(
            tmp_path,
            broadcaster,
            registry_row_toml("browser", refusing_app.http_url, message_handlers=[("open:url", "/api/open-url")]),
        )

        relayed = _relay(client, {"type": "open:url", "client_id": "c1", "payload": {"url": "http://localhost:3000/"}})

    assert relayed.status_code == 502
    assert relayed.get_json()["deliveries"] == [
        {"app": "browser", "status": 503, "detail": "Chromium is not installed yet"}
    ]
    assert relayed.get_json()["detail"] == "browser did not take it: Chromium is not installed yet"
