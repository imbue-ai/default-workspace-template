"""Tests for the accounts broker (share authorization handoff + JWKS)."""

import json
import logging
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import algorithms as jwt_algorithms_rsa
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.share_broker as share_broker_module
from imbue.modal_app_kit.request_logging import RequestLoggingMiddleware
from imbue.remote_service_connector.share_broker import build_broker_jwks
from imbue.remote_service_connector.share_broker import mint_share_handoff_token
from imbue.remote_service_connector.shares import derive_share_user_label
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _SHARE_STUB_EMAIL
from imbue.remote_service_connector.testing import _SHARE_STUB_HOST_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_ID
from imbue.remote_service_connector.testing import _SHARE_STUB_USER_LABEL
from imbue.remote_service_connector.testing import _make_share_test_client_with_fakes
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.web import web_app

_TEST_BROKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_BROKER_KEY_PEM = _TEST_BROKER_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")


def _make_broker_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakePoolBackend, FakeSuperTokensBackend]:
    supertokens_backend = make_fake_supertokens_backend()
    _plain_client, backend = _make_share_test_client_with_fakes(monkeypatch, {})
    # The accounts surface's cookies are Secure, so the client must speak
    # https or its cookie jar will store them and then refuse to send them.
    client = TestClient(web_app, base_url="https://testserver")
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", _TEST_BROKER_KEY_PEM)
    supertokens_backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, supertokens_backend


def _sign_in_verified_visitor(
    client: TestClient,
    st_backend: FakeSuperTokensBackend,
    email: str = "visitor@example.com",
) -> str:
    """Create a verified account with a live browser session on ``client``; returns its user id."""
    signup = st_backend.sign_up(tenant_id="public", email=email, password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.mark_email_verified(signup.user.id)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    return signup.user.id


def _seed_active_share(backend: FakePoolBackend) -> str:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    backend.add_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL, "us1", domain)
    return domain


def _authorize_url(
    domain: str, callback_origin: str, state: str = "abc", next_url: str = "", confirmed: bool = True
) -> str:
    query = {
        "machine_domain": domain,
        "next": next_url,
        "callback_origin": callback_origin,
        "state": state,
    }
    if confirmed:
        query["confirmed"] = "1"
    return f"/share/authorize?{urlencode(query)}"


def test_build_broker_jwks_matches_signing_key() -> None:
    jwks = build_broker_jwks(_TEST_BROKER_KEY.public_key())

    assert len(jwks["keys"]) == 1
    key_entry = jwks["keys"][0]
    assert key_entry["kty"] == "RSA"
    assert key_entry["alg"] == "RS256"
    assert key_entry["kid"]
    assert "=" not in key_entry["n"]
    reconstructed = jwt_algorithms_rsa.RSAAlgorithm.from_jwk(json.dumps(key_entry))
    assert isinstance(reconstructed, rsa.RSAPublicKey)
    assert reconstructed.public_numbers() == _TEST_BROKER_KEY.public_key().public_numbers()


def test_mint_share_handoff_token_roundtrips_with_jwks() -> None:
    domain = f"{_SHARE_STUB_HOST_ID}.{_SHARE_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"

    token = mint_share_handoff_token(
        signing_key=_TEST_BROKER_KEY,
        user_id=_SHARE_STUB_USER_ID,
        email=_SHARE_STUB_EMAIL,
        machine_domain=domain,
        nonce="nonce-123",
        is_owner=True,
    )

    claims = pyjwt.decode(token, _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["sub"] == _SHARE_STUB_USER_ID
    assert claims["email"] == _SHARE_STUB_EMAIL
    assert claims["nonce"] == "nonce-123"
    assert claims["owner"] is True
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == 60
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == build_broker_jwks(_TEST_BROKER_KEY.public_key())["keys"][0]["kid"]


def test_broker_jwks_endpoint_serves_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/jwks.json")

    assert resp.status_code == 200
    assert resp.json() == build_broker_jwks(_TEST_BROKER_KEY.public_key())


def test_broker_legacy_login_path_redirects_to_the_merged_login_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/login?next=/share/authorize%3Fa%3Db", follow_redirects=False)

    assert resp.status_code == 308
    assert resp.headers["location"] == "/login?next=%2Fshare%2Fauthorize%3Fa%3Db"


def test_broker_authorize_redirects_to_login_without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next=https://web-1a2b3c4d.{domain}/panel"
        f"&callback_origin={callback_origin}&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?next=")
    # The callback origin (and machine domain) must survive the login round-trip.
    assert "machine_domain" in resp.headers["location"]
    assert "callback_origin" in resp.headers["location"]


def test_broker_authorize_shows_interstitial_for_an_unconfirmed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing session is never used silently: without confirmed=1 the visitor goes to the login page.

    The login page renders the "Continue as ..." confirmation and returns them
    here with confirmed=1.
    """
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    _sign_in_verified_visitor(client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(_authorize_url(domain, callback_origin, confirmed=False), follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?next=")


def test_broker_authorize_requires_machine_domain_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    assert client.get("/share/authorize?state=abc", follow_redirects=False).status_code == 400
    assert client.get("/share/authorize?machine_domain=x.example", follow_redirects=False).status_code == 400


def test_broker_authorize_rejects_missing_or_foreign_callback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    _sign_in_verified_visitor(client, st_backend)

    # No callback_origin at all.
    missing = client.get(f"/share/authorize?machine_domain={domain}&state=abc", follow_redirects=False)
    assert missing.status_code == 400
    # A callback_origin on a foreign host would leak a signed token off-domain.
    foreign = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://auth-x.evil.example.com&state=abc",
        follow_redirects=False,
    )
    assert foreign.status_code == 400
    # The bare domain does not route and is not a valid callback origin.
    bare = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://{domain}&state=abc",
        follow_redirects=False,
    )
    assert bare.status_code == 400
    # A deeper host is not a single label under the domain: the relay refuses
    # to route it and the wildcard cert does not cover it (mirrors NewProxy).
    deeper = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://a.auth-x7k9q2w1.{domain}&state=abc",
        follow_redirects=False,
    )
    assert deeper.status_code == 400


def test_broker_authorize_404s_without_active_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    _sign_in_verified_visitor(client, st_backend)

    resp = client.get(
        _authorize_url("unknown.example.com", "https://auth-x7k9q2w1.unknown.example.com"),
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_authorize_hands_off_signed_token_to_the_auth_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    visitor_user_id = _sign_in_verified_visitor(client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    next_url = f"https://web-1a2b3c4d.{domain}/panel?x=1"

    resp = client.get(
        _authorize_url(domain, callback_origin, state="nonce-9", next_url=next_url),
        follow_redirects=False,
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    # Delivered to the dedicated auth origin, not the bare domain.
    assert location.startswith(f"{callback_origin}/_auth/callback?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["nonce-9"]
    assert query["next"] == [next_url]
    claims = pyjwt.decode(query["token"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["sub"] == visitor_user_id
    assert claims["email"] == "visitor@example.com"
    assert claims["nonce"] == "nonce-9"


def test_broker_authorize_drops_a_foreign_next(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    _sign_in_verified_visitor(client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        _authorize_url(domain, callback_origin, state="nonce-9", next_url="https://evil.example.com/"),
        follow_redirects=False,
    )

    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    # A foreign next is dropped (the gateway falls back to a safe landing spot).
    assert query.get("next", [""]) == [""]


def test_broker_authorize_rejects_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    share = backend.find_share(_SHARE_STUB_HOST_ID, _SHARE_STUB_USER_LABEL)
    assert share is not None
    share["state"] = "inactive"
    _sign_in_verified_visitor(client, st_backend)

    resp = client.get(
        _authorize_url(domain, f"https://auth-x7k9q2w1.{domain}"),
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_authorize_requires_verified_email_and_sends_the_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unverified visitor is bounced to the check-inbox page after the contextual verification send.

    Satisfying a share grant is one of the two verification-gated actions: the
    visitor's email IS the authorization identity, so an unverified session
    must never receive a handoff token.
    """
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    signup = st_backend.sign_up(tenant_id="public", email="unverified@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)

    resp = client.get(
        _authorize_url(domain, f"https://auth-x7k9q2w1.{domain}"),
        follow_redirects=False,
    )

    # The check-your-inbox page lives in the hosted accounts bundle; it now
    # carries the way back -- the /share/authorize path that re-enters this
    # authorization once the email is verified.
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/check-inbox?next=")
    continue_path = parse_qs(urlsplit(location).query)["next"][0]
    assert continue_path.startswith("/share/authorize?")
    continue_query = parse_qs(urlsplit(continue_path).query)
    assert continue_query["machine_domain"] == [domain]
    assert continue_query["state"] == ["abc"]
    # The return path skips the "Continue as ..." interstitial: the visitor
    # already passed it on the way in.
    assert continue_query["confirmed"] == ["1"]
    assert len(st_backend.sent_verification_emails) == 1
    # Definitely no handoff token was minted for the unverified visitor.
    assert "_auth/callback" not in location


def _seed_share_owned_by(backend: FakePoolBackend, owner_user_id: str) -> str:
    """Seed an active share whose owner label is derived from ``owner_user_id``."""
    owner_label = derive_share_user_label(owner_user_id)
    domain = f"{_SHARE_STUB_HOST_ID}.{owner_label}.us1.{_CONTENT_DOMAIN}"
    backend.add_share(_SHARE_STUB_HOST_ID, owner_label, "us1", domain)
    return domain


def test_broker_owner_skips_the_interstitial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace owner is handed off with no confirmed=1 interstitial round-trip."""
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    owner_user_id = _sign_in_verified_visitor(client, st_backend, email="owner@example.com")
    domain = _seed_share_owned_by(backend, owner_user_id)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        _authorize_url(domain, callback_origin, state="n-owner", confirmed=False), follow_redirects=False
    )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{callback_origin}/_auth/callback?")
    query = parse_qs(urlsplit(location).query)
    claims = pyjwt.decode(query["token"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["owner"] is True


def test_broker_owner_does_not_require_a_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner with an unverified email is still handed off (ownership, not email, is the identity)."""
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="unverified-owner@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    domain = _seed_share_owned_by(backend, signup.user.id)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(_authorize_url(domain, callback_origin, state="n-owner2"), follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{callback_origin}/_auth/callback?")
    # No verification email was needed for the owner.
    assert len(st_backend.sent_verification_emails) == 0


def test_broker_non_owner_token_carries_owner_false(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    _sign_in_verified_visitor(client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(_authorize_url(domain, callback_origin, state="n-visitor"), follow_redirects=False)

    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    claims = pyjwt.decode(query["token"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["owner"] is False


def test_broker_authorize_login_roundtrip_url_is_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The login redirect's next parameter decodes back to the original authorize request."""
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    original = f"/share/authorize?{urlencode({'machine_domain': domain, 'next': '', 'callback_origin': callback_origin, 'state': 'n-1'})}"

    resp = client.get(original, follow_redirects=False)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location == f"/login?next={quote(original, safe='')}"


class _RecordingLogHandler(logging.Handler):
    """Captures formatted messages from a propagate=False logger (caplog cannot)."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_broker_authorize_emits_a_structured_share_visit_line(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    visitor_user_id = _sign_in_verified_visitor(client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    recording_handler = _RecordingLogHandler()
    share_broker_module._share_visit_logger.addHandler(recording_handler)
    try:
        resp = client.get(_authorize_url(domain, callback_origin, state="nonce-3"), follow_redirects=False)
    finally:
        share_broker_module._share_visit_logger.removeHandler(recording_handler)

    assert resp.status_code == 302
    assert len(recording_handler.messages) == 1
    visit_record = json.loads(recording_handler.messages[0])
    assert visit_record == {
        "type": "share_visit_authorized",
        "visitor_user_id": visitor_user_id,
        "host_id": _SHARE_STUB_HOST_ID,
        "owner_share_label": _SHARE_STUB_USER_LABEL,
        "workspace_domain": domain,
        "is_owner": False,
    }


def test_broker_authorize_stashes_the_visitor_identity_for_the_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrap the app the way app.py does (RequestLoggingMiddleware outermost) and
    # confirm an authenticated request's JSON access-log line carries the full
    # user id stashed during routing.
    _client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    lines: list[str] = []
    logging_client = TestClient(
        RequestLoggingMiddleware(web_app, line_sink=lines.append), base_url="https://testserver"
    )
    visitor_user_id = _sign_in_verified_visitor(logging_client, st_backend)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = logging_client.get(_authorize_url(domain, callback_origin), follow_redirects=False)

    assert resp.status_code == 302
    access_records = [json.loads(line) for line in lines]
    authorize_records = [r for r in access_records if r["path"] == "/share/authorize"]
    assert len(authorize_records) == 1
    assert authorize_records[0]["user"] == visitor_user_id
    assert authorize_records[0]["type"] == "http_request"
