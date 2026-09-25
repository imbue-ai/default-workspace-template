"""The embedder-message relay (desktop contracts.md section 5.6): the shell posts a message from the minds chrome to
every app whose row registered its type, over real loopback servers standing in for the apps."""

from pathlib import Path
from typing import Any

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
