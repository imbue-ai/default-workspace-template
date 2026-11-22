"""The real transport against a stand-in push service on loopback: the message arrives encrypted to the device's keys
(RFC 8291), so only the device can read it, and signed with the shell's VAPID key (RFC 8292), so the push service
takes it only from this shell."""

import base64
import json
import os
from pathlib import Path
from typing import Any

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask
from flask import request
from py_vapid import Vapid

from imbue.system_interface.shell.web_push import BrowserPushSubscription
from imbue.system_interface.shell.web_push import PUSH_TTL_SECONDS
from imbue.system_interface.shell.web_push import PushSubscriptionKeys
from imbue.system_interface.shell.web_push import PywebpushTransport
from imbue.system_interface.shell.web_push import VAPID_SUBJECT
from imbue.system_interface.shell.web_push import WebPushNotifier
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import serve_app


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64url_decoded(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _push_service(received: list[dict[str, Any]], status: int) -> Flask:
    """A push service that keeps every message it is posted and answers ``status``."""
    service = Flask(__name__)

    @service.post("/push/<token>")
    def take(token: str) -> tuple[str, int]:
        received.append({"token": token, "headers": dict(request.headers), "body": request.get_data()})
        return "", status

    return service


def _device_subscription(
    endpoint: str, device_key: ec.EllipticCurvePrivateKey, auth_secret: bytes
) -> BrowserPushSubscription:
    """The subscription a browser would hand over, at a loopback endpoint the https rule would refuse."""
    public_point = device_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return BrowserPushSubscription.model_construct(
        endpoint=endpoint, keys=PushSubscriptionKeys(p256dh=_base64url(public_point), auth=_base64url(auth_secret))
    )


def test_a_message_reaches_the_push_service_encrypted_to_the_device_and_signed_by_the_shell(tmp_path: Path) -> None:
    notifier = WebPushNotifier(directory=tmp_path / "web_push", transport=PywebpushTransport())
    device_key = ec.generate_private_key(ec.SECP256R1())
    auth_secret = os.urandom(16)
    received: list[dict[str, Any]] = []
    payload = json.dumps({"title": "Report ready", "body": "Tap to open", "path": "/", "tag": None})

    with serve_app(_push_service(received, 201)) as push_provider:
        subscription = _device_subscription(f"{push_provider.http_url}/push/abc", device_key, auth_secret)
        answer = notifier.transport.post(subscription, payload, notifier.signing_key())

    assert answer.status_code == 201
    ((message,),) = [received]
    headers = message["headers"]
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Ttl"] == str(PUSH_TTL_SECONDS)
    assert headers["Urgency"] == "high"
    plaintext = http_ece.decrypt(message["body"], private_key=device_key, auth_secret=auth_secret, version="aes128gcm")
    assert plaintext.decode("utf-8") == payload
    authorization = headers["Authorization"]
    assert Vapid.verify(authorization) is True
    token_part, key_part = authorization.removeprefix("vapid ").split(",")
    assert key_part == f"k={notifier.public_key()}"
    claims = json.loads(_base64url_decoded(token_part.removeprefix("t=").split(".")[1]))
    assert claims["aud"] == push_provider.http_url
    assert claims["sub"] == VAPID_SUBJECT


def test_a_refusal_and_no_answer_at_all_come_back_as_answers_not_errors(tmp_path: Path) -> None:
    notifier = WebPushNotifier(directory=tmp_path / "web_push", transport=PywebpushTransport())
    device_key = ec.generate_private_key(ec.SECP256R1())
    received: list[dict[str, Any]] = []

    with serve_app(_push_service(received, 410)) as push_provider:
        gone = notifier.transport.post(
            _device_subscription(f"{push_provider.http_url}/push/gone", device_key, os.urandom(16)),
            "{}",
            notifier.signing_key(),
        )
    unreachable = notifier.transport.post(
        _device_subscription(f"http://127.0.0.1:{find_free_port()}/push/x", device_key, os.urandom(16)),
        "{}",
        notifier.signing_key(),
    )

    assert gone.status_code == 410
    assert unreachable.status_code is None
    assert unreachable.detail
