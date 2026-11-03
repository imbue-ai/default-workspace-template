import json
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from workspace_layout.identity import RequestIdentity

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.web_push import device_id_of
from imbue.system_interface.testing import RecordingPushTransport
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-one"
_OTHER_ENDPOINT = "https://web.push.apple.com/device-two"
_VISITOR = RequestIdentity(owner=False, user_id="user-visitor", email="visitor@example.com")
_SHARED_OWNER = RequestIdentity(owner=True, user_id="user-owner", email="owner@example.com")
# A caller from outside the container.
_REMOTE = {"REMOTE_ADDR": "203.0.113.7"}


def _registration(endpoint: str = _ENDPOINT, label: str = "iPhone, Safari (Home Screen app)") -> dict[str, object]:
    return {
        "subscription": {
            "endpoint": endpoint,
            "expirationTime": None,
            "keys": {"p256dh": "BPublic", "auth": "secret"},
        },
        "label": label,
    }


def _transport(app: Flask) -> RecordingPushTransport:
    transport = state_of(app).shell.web_push.transport
    assert type(transport) is RecordingPushTransport
    return transport


def _register(client: FlaskClient, endpoint: str = _ENDPOINT) -> str:
    response = client.post(
        "/api/web-push/devices",
        json=_registration(endpoint),
        headers=identity_headers(_SHARED_OWNER),
        environ_base=_REMOTE,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["device_id"]


def test_the_page_its_script_and_the_worker_are_served_from_the_shells_root_uncached(client: FlaskClient) -> None:
    page = client.get("/notifications")
    script = client.get("/notifications.js")
    worker = client.get("/sw.js")

    assert page.status_code == script.status_code == worker.status_code == 200
    assert page.mimetype == "text/html"
    assert script.mimetype == worker.mimetype == "text/javascript"
    assert {page.headers["Cache-Control"], script.headers["Cache-Control"], worker.headers["Cache-Control"]} == {
        "no-cache"
    }
    # The page loads its script from its own origin, which the shell's page policy admits; it carries no inline one.
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert '<script type="module" src="notifications.js"></script>' in page.get_data(as_text=True)
    assert 'addEventListener("push"' in worker.get_data(as_text=True)


def test_the_public_key_is_the_raw_p256_point_and_stays_the_same(client: FlaskClient, app: Flask) -> None:
    first = client.get("/api/web-push/key").get_json()["public_key"]
    second = client.get("/api/web-push/key").get_json()["public_key"]

    assert first == second
    # 65 bytes (0x04, then X and Y) in unpadded base64url.
    assert len(first) == 87 and first.startswith("B") and "=" not in first
    key_path = state_of(app).shell.web_push.directory / "vapid_private_key.pem"
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_the_owner_registers_a_device_lists_it_and_forgets_it(client: FlaskClient) -> None:
    device_id = _register(client)

    listing = client.get("/api/web-push/devices", headers=identity_headers(_SHARED_OWNER)).get_json()
    assert [(device["device_id"], device["label"], device["push_provider"]) for device in listing["devices"]] == [
        (device_id, "iPhone, Safari (Home Screen app)", "Google")
    ]
    # The listing never hands out the subscription itself.
    assert "subscription" not in listing["devices"][0]
    assert device_id == device_id_of(_ENDPOINT)

    assert (
        client.delete(f"/api/web-push/devices/{device_id}", headers=identity_headers(_SHARED_OWNER)).status_code == 204
    )
    assert client.get("/api/web-push/devices").get_json()["devices"] == []
    assert client.delete(f"/api/web-push/devices/{device_id}").status_code == 404


def test_registering_the_same_subscription_again_keeps_one_entry_with_the_new_label(client: FlaskClient) -> None:
    _register(client)
    client.post("/api/web-push/devices", json=_registration(label="Android, Firefox"))

    devices = client.get("/api/web-push/devices").get_json()["devices"]

    assert [device["label"] for device in devices] == ["Android, Firefox"]


def test_a_visitor_can_neither_register_nor_list_nor_remove_devices(client: FlaskClient) -> None:
    device_id = _register(client)
    visitor = identity_headers(_VISITOR)

    refusals = [
        client.post("/api/web-push/devices", json=_registration(_OTHER_ENDPOINT), headers=visitor),
        client.get("/api/web-push/devices", headers=visitor),
        client.delete(f"/api/web-push/devices/{device_id}", headers=visitor),
        client.post(f"/api/web-push/devices/{device_id}/test", headers=visitor),
    ]

    assert [refusal.status_code for refusal in refusals] == [403, 403, 403, 403]
    assert [device["device_id"] for device in client.get("/api/web-push/devices").get_json()["devices"]] == [device_id]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://push.example.com/device",
        # Origins the VAPID claim cannot be signed for, which would fail the send to every device.
        "https://user:secret@push.example.com/device",
        "https://[2001:db8::1]/device",
        "https://push.example.com:port/device",
    ],
)
def test_a_registration_with_an_endpoint_off_an_https_host_is_refused(client: FlaskClient, endpoint: str) -> None:
    response = client.post("/api/web-push/devices", json=_registration(endpoint))

    assert response.status_code == 400
    assert "https" in response.get_json()["detail"]
    assert client.get("/api/web-push/devices").get_json()["devices"] == []


def test_a_registration_whose_keys_are_not_base64url_is_refused(client: FlaskClient) -> None:
    registration = _registration()
    registration["subscription"] = {"endpoint": _ENDPOINT, "keys": {"p256dh": "!!!", "auth": "secret"}}

    response = client.post("/api/web-push/devices", json=registration)

    assert response.status_code == 400
    assert client.get("/api/web-push/devices").get_json()["devices"] == []


def test_a_test_send_reaches_only_that_device(client: FlaskClient, app: Flask) -> None:
    device_id = _register(client)
    _register(client, _OTHER_ENDPOINT)

    outcome = client.post(f"/api/web-push/devices/{device_id}/test", headers=identity_headers(_SHARED_OWNER))

    assert outcome.status_code == 200
    assert outcome.get_json()["accepted_count"] == 1
    ((posted),) = _transport(app).posted
    assert posted.endpoint == _ENDPOINT
    assert json.loads(posted.payload)["title"] == "Notifications are on"
    assert client.post("/api/web-push/devices/unknown/test").status_code == 404


def test_the_loopback_send_reaches_every_device_and_forgets_a_gone_one(client: FlaskClient, app: Flask) -> None:
    _register(client)
    gone_id = _register(client, _OTHER_ENDPOINT)
    _transport(app).status_code_by_endpoint[_OTHER_ENDPOINT] = 410

    response = client.post(
        "/api/web-push/send",
        json={"title": "Report ready", "body": "The Q4 numbers are in.", "path": "/?open=chat:/?chat=c1", "tag": "t"},
    )

    assert response.status_code == 200
    outcome = response.get_json()
    assert outcome["accepted_count"] == 1
    assert {(delivery["status_code"], delivery["is_forgotten"]) for delivery in outcome["deliveries"]} == {
        (201, False),
        (410, True),
    }
    assert [json.loads(posted.payload) for posted in _transport(app).posted] == [
        {"title": "Report ready", "body": "The Q4 numbers are in.", "path": "/?open=chat:/?chat=c1", "tag": "t"}
    ] * 2
    remaining = [device["device_id"] for device in client.get("/api/web-push/devices").get_json()["devices"]]
    assert gone_id not in remaining and len(remaining) == 1


def test_a_send_with_no_device_registered_posts_nothing(client: FlaskClient, app: Flask) -> None:
    response = client.post("/api/web-push/send", json={"title": "Done"})

    assert response.get_json() == {"deliveries": [], "accepted_count": 0}
    assert _transport(app).posted == []


def test_the_send_route_refuses_a_remote_caller_and_anything_that_came_through_a_share(
    client: FlaskClient, app: Flask
) -> None:
    _register(client)

    remote = client.post("/api/web-push/send", json={"title": "Phish"}, environ_base=_REMOTE)
    proxied = client.post("/api/web-push/send", json={"title": "Phish"}, headers=identity_headers(_SHARED_OWNER))

    assert (remote.status_code, proxied.status_code) == (403, 403)
    assert _transport(app).posted == []


def test_a_send_path_must_stay_under_the_shells_root(client: FlaskClient) -> None:
    for path in ("https://evil.example/", "//evil.example/", "relative", "/\\evil.example"):
        assert client.post("/api/web-push/send", json={"title": "Done", "path": path}).status_code == 400


def test_a_send_too_large_for_a_push_service_is_refused_before_any_post(client: FlaskClient, app: Flask) -> None:
    _register(client)

    response = client.post("/api/web-push/send", json={"title": "Done", "body": "\U0001f600" * 1000})

    assert response.status_code == 400
    assert "bytes as sent" in response.get_json()["detail"]
    assert _transport(app).posted == []


def test_a_preview_shell_reaches_no_device(tmp_path: Path, broadcaster: WebSocketBroadcaster) -> None:
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster)
    application = shell_application(tmp_path, inventory, broadcaster, is_preview=True)
    preview = application.test_client()

    refusals = [
        preview.post("/api/web-push/devices", json=_registration()),
        preview.post("/api/web-push/devices/any/test"),
        preview.delete("/api/web-push/devices/any"),
        preview.post("/api/web-push/send", json={"title": "Done"}),
    ]

    assert [refusal.status_code for refusal in refusals] == [403, 403, 403, 403]
    assert all("preview" in refusal.get_json()["detail"] for refusal in refusals)
    assert _transport(application).posted == []
