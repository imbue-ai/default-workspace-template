import base64
import json
import re

import pytest
from starlette.testclient import TestClient

from imbue.oauth_redirector.forwarding import StateParseError
from imbue.oauth_redirector.forwarding import is_allowed_forward_target
from imbue.oauth_redirector.forwarding import read_callback_url_from_state
from imbue.oauth_redirector.web import web_app

_DEV_HOST_PATTERN = re.compile(r"minds-dev(-[a-z0-9-]+)?--rsc-dev-api\.modal\.run")

_ALLOWED_CALLBACK = "https://minds-dev-josh-1--rsc-dev-api.modal.run/share/oauth/google/callback"


def _fake_state(callback_url: str | None) -> str:
    """Build an unsigned JWT-shaped state carrying ``cb`` (signature is never checked here)."""
    payload: dict[str, object] = {"purpose": "accounts_oauth", "nonce": "n"}
    if callback_url is not None:
        payload["cb"] = callback_url
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{payload_b64}.signature"


def test_read_callback_url_from_state_extracts_cb_claim() -> None:
    assert read_callback_url_from_state(_fake_state(_ALLOWED_CALLBACK)) == _ALLOWED_CALLBACK


def test_read_callback_url_from_state_rejects_non_jwt_and_missing_claim() -> None:
    with pytest.raises(StateParseError):
        read_callback_url_from_state("not-a-jwt")
    with pytest.raises(StateParseError):
        read_callback_url_from_state(_fake_state(None))


def test_read_callback_url_from_state_rejects_non_object_json_payloads() -> None:
    """A JWT-shaped state whose payload is valid JSON but not an object must 400, not crash."""
    for payload_json in ("[1, 2]", '"just-a-string"', "3", "null"):
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()
        state = f"eyJhbGciOiJSUzI1NiJ9.{payload_b64}.signature"
        with pytest.raises(StateParseError):
            read_callback_url_from_state(state)


def test_is_allowed_forward_target_accepts_only_tier_connector_callbacks() -> None:
    assert is_allowed_forward_target(_ALLOWED_CALLBACK, _DEV_HOST_PATTERN)
    # Wrong scheme, host, path, or extra query -- all refused.
    assert not is_allowed_forward_target(_ALLOWED_CALLBACK.replace("https://", "http://"), _DEV_HOST_PATTERN)
    assert not is_allowed_forward_target("https://evil.example.com/share/oauth/google/callback", _DEV_HOST_PATTERN)
    assert not is_allowed_forward_target(
        "https://minds-dev-josh-1--rsc-dev-api.modal.run/other/path", _DEV_HOST_PATTERN
    )
    assert not is_allowed_forward_target(_ALLOWED_CALLBACK + "?x=1", _DEV_HOST_PATTERN)
    # The pattern must match the FULL host: a crafted superstring host that
    # merely contains the pattern is refused.
    assert not is_allowed_forward_target(
        "https://minds-dev-a--rsc-dev-api.modal.run.evil.example/share/oauth/google/callback",
        _DEV_HOST_PATTERN,
    )


def test_forward_redirects_the_full_query_to_the_state_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAUTH_REDIRECTOR_ALLOWED_HOST_REGEX", r"minds-dev(-[a-z0-9-]+)?--rsc-dev-api\.modal\.run")
    client = TestClient(web_app)
    state = _fake_state(_ALLOWED_CALLBACK)

    resp = client.get(f"/forward?code=abc123&state={state}", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{_ALLOWED_CALLBACK}?code=abc123&state={state}"


def test_forward_refuses_foreign_and_unreadable_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAUTH_REDIRECTOR_ALLOWED_HOST_REGEX", r"minds-dev(-[a-z0-9-]+)?--rsc-dev-api\.modal\.run")
    client = TestClient(web_app)

    foreign = _fake_state("https://evil.example.com/share/oauth/google/callback")
    assert client.get(f"/forward?code=x&state={foreign}", follow_redirects=False).status_code == 400
    assert client.get("/forward?code=x&state=garbage", follow_redirects=False).status_code == 400
    assert client.get("/forward?code=x", follow_redirects=False).status_code == 400


def test_forward_requires_the_allowlist_to_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OAUTH_REDIRECTOR_ALLOWED_HOST_REGEX", raising=False)
    client = TestClient(web_app)
    state = _fake_state(_ALLOWED_CALLBACK)

    resp = client.get(f"/forward?code=abc&state={state}", follow_redirects=False)

    assert resp.status_code == 503
