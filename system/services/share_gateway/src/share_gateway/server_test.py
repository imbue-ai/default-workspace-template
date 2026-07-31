from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import jwt
from flask import Flask
from flask.testing import FlaskClient

from share_gateway.handoff import JwksCache
from share_gateway.handoff import SingleUseJtiRegistry
from share_gateway.materials import ShareMaterials
from share_gateway.server import PendingLoginRegistry
from share_gateway.server import build_gateway_app
from share_gateway.session_cookie import SESSION_COOKIE_NAME
from share_gateway.session_cookie import mint_session_cookie_value

from cryptography.hazmat.primitives.asymmetric import rsa

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"
_BROKER_URL = "https://accounts.example.com"
_SIGNING_SECRET = "gateway-secret-4471"
_TEST_KID = "test-kid-1"
_BROKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Every origin is an unguessable ``<name>-<rand>`` label; grants stay keyed by
# name, and the gateway maps label -> name via this registry.
_AUTH_LABEL = "auth-x7k9q2w1"
_LABELS = {
    "system_interface-shell111": "system_interface",
    "web-web111111": "web",
    "terminal-term1111": "terminal",
}
_SHELL_HOST = f"system_interface-shell111.{_DOMAIN}"
_WEB_HOST = f"web-web111111.{_DOMAIN}"
_TERMINAL_HOST = f"terminal-term1111.{_DOMAIN}"
_AUTH_ORIGIN = f"https://{_AUTH_LABEL}.{_DOMAIN}"

_GRANTS = """
[workspace]
emails = ["bob@example.com"]
email_domains = []

[services.web]
emails = ["carol@example.com"]
email_domains = []
"""


class _GatewayHarness:
    def __init__(self, app: Flask, client: FlaskClient, grants_path: Path, pending_logins: PendingLoginRegistry) -> None:
        self.app = app
        self.client = client
        self.grants_path = grants_path
        self.pending_logins = pending_logins


def _make_harness(tmp_path: Path, grants_text: str = _GRANTS) -> _GatewayHarness:
    grants_path = tmp_path / "share_grants.toml"
    grants_path.write_text(grants_text)
    materials = ShareMaterials(
        workspace_domain=_DOMAIN,
        relay_host="relay.example.com",
        relay_port=7000,
        relay_token="tok",
        connector_url="https://connector.example.com",
        broker_url=_BROKER_URL,
    )
    pending_logins = PendingLoginRegistry()
    app = build_gateway_app(
        materials=materials,
        grants_path=grants_path,
        signing_secret=_SIGNING_SECRET,
        jwks_cache=JwksCache(
            f"{_BROKER_URL}/share/jwks.json",
            preloaded_keys_by_kid={_TEST_KID: _BROKER_KEY.public_key()},
        ),
        jti_registry=SingleUseJtiRegistry(),
        pending_logins=pending_logins,
        auth_label=_AUTH_LABEL,
        get_label_to_name=lambda: _LABELS,
    )
    return _GatewayHarness(app, app.test_client(), grants_path, pending_logins)


def _session_cookie_for(email: str) -> str:
    return mint_session_cookie_value(_SIGNING_SECRET, email, _DOMAIN)


def _install_session(client: FlaskClient, email: str, extra_cookies: dict[str, str] | None = None) -> None:
    # The werkzeug test client ignores a hand-written Cookie header; go
    # through its cookie jar instead.
    client.set_cookie(SESSION_COOKIE_NAME, _session_cookie_for(email))
    for name, value in (extra_cookies or {}).items():
        client.set_cookie(name, value)


def _verify_headers(
    host: str = _SHELL_HOST,
    method: str = "GET",
    uri: str = "/some/page",
    accept: str = "text/html,application/xhtml+xml",
    origin: str | None = None,
    is_websocket: bool = False,
) -> dict[str, str]:
    headers = {
        "X-Forwarded-Host": host,
        "X-Forwarded-Method": method,
        "X-Forwarded-Uri": uri,
        "Accept": accept,
    }
    if origin is not None:
        headers["Origin"] = origin
    if is_websocket:
        headers["X-Forwarded-Upgrade"] = "websocket"
    return headers


def _mint_handoff(nonce: str, email: str = "bob@example.com", audience: str = _DOMAIN, jti: str = "jti-1") -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-1",
        "email": email,
        "aud": audience,
        "jti": jti,
        "nonce": nonce,
        "iat": now,
        "exp": now + timedelta(seconds=60),
    }
    return jwt.encode(payload, _BROKER_KEY, algorithm="RS256", headers={"kid": _TEST_KID})


def test_unauthenticated_html_navigation_redirects_to_broker_with_callback_origin(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.client.get("/_auth/verify", headers=_verify_headers(host=_WEB_HOST, uri="/x?y=1"))

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith(f"{_BROKER_URL}/share/authorize?")
    query = parse_qs(urlsplit(location).query)
    assert query["machine_domain"] == [_DOMAIN]
    assert query["next"] == [f"https://{_WEB_HOST}/x?y=1"]
    # The callback must land on the dedicated auth origin, not the service origin.
    assert query["callback_origin"] == [_AUTH_ORIGIN]
    assert query["state"][0]


def test_unauthenticated_non_html_request_gets_401(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    resp = harness.client.get("/_auth/verify", headers=_verify_headers(accept="application/json"))

    assert resp.status_code == 401


def test_unknown_label_is_forbidden(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "bob@example.com")

    resp = harness.client.get("/_auth/verify", headers=_verify_headers(host=f"unknown-00000000.{_DOMAIN}"))

    assert resp.status_code == 403


def test_workspace_grant_allows_shell_and_services_and_strips_cookie(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "bob@example.com", extra_cookies={"other": "1"})

    shell = harness.client.get("/_auth/verify", headers=_verify_headers())
    service = harness.client.get("/_auth/verify", headers=_verify_headers(host=_TERMINAL_HOST))

    assert shell.status_code == 200
    assert shell.headers["X-Share-Filtered-Cookie"] == "other=1"
    assert service.status_code == 200


def test_per_service_grant_scopes_to_that_service_only(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "carol@example.com")

    allowed = harness.client.get("/_auth/verify", headers=_verify_headers(host=_WEB_HOST))
    shell = harness.client.get("/_auth/verify", headers=_verify_headers())
    sibling = harness.client.get("/_auth/verify", headers=_verify_headers(host=_TERMINAL_HOST))

    assert allowed.status_code == 200
    assert shell.status_code == 403
    assert sibling.status_code == 403


def test_revocation_is_instant_via_grants_file(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "bob@example.com")
    assert harness.client.get("/_auth/verify", headers=_verify_headers()).status_code == 200

    harness.grants_path.write_text("[workspace]\nemails = []\nemail_domains = []\n")

    assert harness.client.get("/_auth/verify", headers=_verify_headers()).status_code == 403


def test_malformed_grants_fail_closed(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, grants_text="not toml [[")
    _install_session(harness.client, "bob@example.com")

    resp = harness.client.get("/_auth/verify", headers=_verify_headers())

    assert resp.status_code == 403


def test_websocket_upgrade_requires_workspace_origin_even_with_session(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "bob@example.com")

    no_origin = harness.client.get("/_auth/verify", headers=_verify_headers(is_websocket=True))
    foreign = harness.client.get(
        "/_auth/verify",
        headers=_verify_headers(is_websocket=True, origin="https://evil.example.com"),
    )
    ours = harness.client.get(
        "/_auth/verify",
        headers=_verify_headers(host=_WEB_HOST, is_websocket=True, origin=f"https://{_WEB_HOST}"),
    )

    assert no_origin.status_code == 403
    assert foreign.status_code == 403
    assert ours.status_code == 200


def test_post_with_foreign_origin_is_rejected(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    _install_session(harness.client, "bob@example.com")

    resp = harness.client.get(
        "/_auth/verify",
        headers=_verify_headers(method="POST", origin="https://evil.example.com"),
    )

    assert resp.status_code == 403


def test_callback_sets_domain_cookie_and_redirects_to_next(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    nonce = harness.pending_logins.mint()
    token = _mint_handoff(nonce)

    resp = harness.client.get(f"/_auth/callback?token={token}&state={nonce}&next=https://{_WEB_HOST}/panel")

    assert resp.status_code == 302
    assert resp.headers["Location"] == f"https://{_WEB_HOST}/panel"
    set_cookie = resp.headers["Set-Cookie"]
    assert SESSION_COOKIE_NAME in set_cookie
    assert f"Domain={_DOMAIN}" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie

    cookie_value = set_cookie.split(f"{SESSION_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    harness.client.set_cookie(SESSION_COOKIE_NAME, cookie_value)
    verified = harness.client.get("/_auth/verify", headers=_verify_headers())
    assert verified.status_code == 200


def test_callback_clamps_foreign_next_to_the_shell_label(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    nonce = harness.pending_logins.mint()
    token = _mint_handoff(nonce)

    resp = harness.client.get(f"/_auth/callback?token={token}&state={nonce}&next=https://evil.example.com/")

    assert resp.status_code == 302
    # The bare domain no longer routes, so a foreign next falls back to the
    # shell (system_interface) label origin.
    assert resp.headers["Location"] == f"https://{_SHELL_HOST}/"


def test_callback_rejects_unknown_state_and_replayed_tokens(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    unknown_state = harness.client.get(f"/_auth/callback?token=whatever&state=never-minted&next=/")
    assert unknown_state.status_code == 403

    nonce = harness.pending_logins.mint()
    token = _mint_handoff(nonce)
    first = harness.client.get(f"/_auth/callback?token={token}&state={nonce}")
    assert first.status_code == 302
    replay = harness.client.get(f"/_auth/callback?token={token}&state={nonce}")
    assert replay.status_code == 403


def test_callback_rejects_wrong_audience_nonce_mismatch_and_ungranted_email(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    nonce_one = harness.pending_logins.mint()
    wrong_audience = _mint_handoff(nonce_one, audience="other.example.com", jti="jti-aud")
    assert harness.client.get(f"/_auth/callback?token={wrong_audience}&state={nonce_one}").status_code == 403

    nonce_two = harness.pending_logins.mint()
    mismatched = _mint_handoff("some-other-nonce", jti="jti-nonce")
    assert harness.client.get(f"/_auth/callback?token={mismatched}&state={nonce_two}").status_code == 403

    nonce_three = harness.pending_logins.mint()
    stranger = _mint_handoff(nonce_three, email="stranger@nowhere.dev", jti="jti-stranger")
    assert harness.client.get(f"/_auth/callback?token={stranger}&state={nonce_three}").status_code == 403


def test_loading_and_healthz_endpoints(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    assert harness.client.get("/_auth/healthz").status_code == 200
    loading = harness.client.get("/_auth/loading")
    assert loading.status_code == 503
    assert "refresh" in loading.get_data(as_text=True)


def test_expired_session_cookie_is_rejected(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {"email": "bob@example.com", "aud": _DOMAIN, "iat": now - timedelta(days=2), "exp": now - timedelta(days=1)},
        _SIGNING_SECRET,
        algorithm="HS256",
    )

    harness.client.set_cookie(SESSION_COOKIE_NAME, expired)

    resp = harness.client.get("/_auth/verify", headers=_verify_headers(accept="application/json"))

    assert resp.status_code == 401
