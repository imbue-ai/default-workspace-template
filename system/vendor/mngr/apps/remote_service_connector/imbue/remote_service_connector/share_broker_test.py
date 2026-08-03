"""Tests for the accounts broker (share login handoff + browser OAuth)."""

import json
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt import algorithms as jwt_algorithms_rsa
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.share_broker import build_broker_jwks
from imbue.remote_service_connector.share_broker import mint_share_handoff_token
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _STUB_EMAIL
from imbue.remote_service_connector.testing import _STUB_HOST_ID
from imbue.remote_service_connector.testing import _STUB_TOKEN
from imbue.remote_service_connector.testing import _STUB_USER_ID
from imbue.remote_service_connector.testing import _STUB_USER_LABEL
from imbue.remote_service_connector.testing import _make_share_test_client_with_fakes
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.web import web_app

# ---------------------------------------------------------------------------
# Accounts broker
# ---------------------------------------------------------------------------

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

    # Resolve the SSO cookie against the fake backend's sessions (so a session
    # minted by the broker's own login/OAuth flows works end to end), with the
    # legacy _STUB_TOKEN accepted for tests that seed the cookie directly.
    def _resolve_token_user_id(token: str) -> str:
        if token == _STUB_TOKEN:
            return _STUB_USER_ID
        session = supertokens_backend.sessions_by_access_token.get(token)
        if session is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return session.user_id

    def _resolve_verified_email(user_id: str) -> str | None:
        account = supertokens_backend.accounts_by_id.get(user_id)
        if account is None:
            return _STUB_EMAIL
        return account.email if account.is_verified else None

    _plain_client, backend = _make_share_test_client_with_fakes(
        monkeypatch,
        {
            "get_user_id_from_access_token": _resolve_token_user_id,
            "default_email_getter": _resolve_verified_email,
        },
    )
    # The broker's cookies are Secure, so the client must speak https or its
    # cookie jar will store them and then silently refuse to send them back.
    client = TestClient(web_app, base_url="https://testserver")
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", _TEST_BROKER_KEY_PEM)
    supertokens_backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, supertokens_backend


def _seed_active_share(backend: FakePoolBackend) -> str:
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"
    backend.add_share(_STUB_HOST_ID, _STUB_USER_LABEL, "us1", domain)
    return domain


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
    domain = f"{_STUB_HOST_ID}.{_STUB_USER_LABEL}.us1.{_CONTENT_DOMAIN}"

    token = mint_share_handoff_token(
        signing_key=_TEST_BROKER_KEY,
        user_id=_STUB_USER_ID,
        email=_STUB_EMAIL,
        machine_domain=domain,
        nonce="nonce-123",
    )

    claims = pyjwt.decode(token, _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["sub"] == _STUB_USER_ID
    assert claims["email"] == _STUB_EMAIL
    assert claims["nonce"] == "nonce-123"
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == 60
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == build_broker_jwks(_TEST_BROKER_KEY.public_key())["keys"][0]["kid"]


def test_broker_jwks_endpoint_serves_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/jwks.json")

    assert resp.status_code == 200
    assert resp.json() == build_broker_jwks(_TEST_BROKER_KEY.public_key())


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
    assert resp.headers["location"].startswith("/share/login?next=")
    # The callback origin (and machine domain) must survive the login round-trip.
    assert "machine_domain" in resp.headers["location"]
    assert "callback_origin" in resp.headers["location"]


def test_broker_authorize_requires_machine_domain_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    assert client.get("/share/authorize?state=abc", follow_redirects=False).status_code == 400
    assert client.get("/share/authorize?machine_domain=x.example", follow_redirects=False).status_code == 400


def test_broker_authorize_rejects_missing_or_foreign_callback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

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
    client, _backend, _st = _make_broker_test_client(monkeypatch)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

    resp = client.get(
        "/share/authorize?machine_domain=unknown.example.com"
        "&callback_origin=https://auth-x7k9q2w1.unknown.example.com&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_authorize_hands_off_signed_token_to_the_auth_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    next_url = f"https://web-1a2b3c4d.{domain}/panel?x=1"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next={next_url}&callback_origin={callback_origin}&state=nonce-9",
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
    assert claims["sub"] == _STUB_USER_ID
    assert claims["email"] == _STUB_EMAIL
    assert claims["nonce"] == "nonce-9"


def test_broker_authorize_drops_a_foreign_next(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&next=https://evil.example.com/"
        f"&callback_origin={callback_origin}&state=nonce-9",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    # A foreign next is dropped (the gateway falls back to a safe landing spot).
    assert query.get("next", [""]) == [""]


def test_broker_authorize_rejects_inactive_share(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _st = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    share = backend.find_share(_STUB_HOST_ID, _STUB_USER_LABEL)
    assert share is not None
    share["state"] = "inactive"
    client.cookies.set("imbue_sso_session", _STUB_TOKEN)

    resp = client.get(
        f"/share/authorize?machine_domain={domain}&callback_origin=https://auth-x7k9q2w1.{domain}&state=abc",
        follow_redirects=False,
    )

    assert resp.status_code == 404


def test_broker_login_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/login?next=/share/authorize%3Fmachine_domain%3Dx")

    assert resp.status_code == 200
    assert "<form method='post' action='/share/session'>" in resp.text
    # The shared CSS must be wrapped in a <style> element inside <head>;
    # unwrapped it gets hoisted into <body> and renders as page text.
    assert "<style>body{" in resp.text
    assert "</style></head>" in resp.text


def test_broker_session_rejects_cross_site_form_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A login POST whose Origin names another site is refused (login CSRF needs no cookie)."""
    client, _backend, _st_backend = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "alice@example.com", "password": "pw-123456", "mode": "signin"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert resp.status_code == 403


def test_broker_session_accepts_a_same_origin_form_post(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="carol@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.mark_email_verified(signup.user.id)

    resp = client.post(
        "/share/session",
        data={"email": "carol@example.com", "password": "pw-123456", "mode": "signin", "next": "/"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resp.status_code == 303


def test_broker_session_sets_cookie_and_redirects_for_verified_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="alice@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.mark_email_verified(signup.user.id)

    resp = client.post(
        "/share/session",
        data={
            "email": "alice@example.com",
            "password": "pw-123456",
            "mode": "signin",
            "next": "/share/authorize%3Fa%3Db",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/share/authorize?a=b"
    set_cookie = resp.headers["set-cookie"]
    assert "imbue_sso_session=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_broker_session_shows_verify_page_for_unverified_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "bob@example.com", "password": "pw-123456", "mode": "signup", "next": "/"},
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "Check your inbox" in resp.text
    assert len(st_backend.sent_verification_emails) == 1


def test_broker_session_rerenders_login_on_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.post(
        "/share/session",
        data={"email": "nobody@example.com", "password": "wrong", "mode": "signin", "next": "/"},
        follow_redirects=False,
    )

    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


# ---------------------------------------------------------------------------
# Broker browser OAuth (Continue with Google)
# ---------------------------------------------------------------------------


def _start_broker_oauth(client: TestClient, next_path: str) -> str:
    """Drive /share/oauth/google/start and return the signed state from the provider redirect."""
    resp = client.get(f"/share/oauth/google/start?next={quote(next_path, safe='')}", follow_redirects=False)
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]


def test_broker_login_page_offers_google_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)

    without_provider = client.get("/share/login?next=/")
    assert "Continue with Google" not in without_provider.text

    st_backend.register_provider("google")
    with_provider = client.get("/share/login?next=/share/authorize%3Fa%3Db")
    assert "Continue with Google" in with_provider.text
    assert "/share/oauth/google/start?next=%2Fshare%2Fauthorize%3Fa%3Db" in with_provider.text


def test_broker_oauth_start_redirects_with_signed_state_and_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")

    resp = client.get("/share/oauth/google/start?next=%2Fshare%2Fauthorize%3Fa%3Db", follow_redirects=False)

    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert location.netloc == "google.example.com"
    query = parse_qs(location.query)
    # The redirect URI is this broker's own web callback, derived from the request.
    assert query["redirect_uri"] == ["https://testserver/share/oauth/google/callback"]
    # The state is self-contained and signed: nonce + where to resume.
    claims = pyjwt.decode(query["state"][0], _TEST_BROKER_KEY.public_key(), algorithms=["RS256"])
    assert claims["purpose"] == "broker_oauth"
    assert claims["next"] == "/share/authorize?a=b"
    set_cookie = resp.headers["set-cookie"]
    assert "imbue_oauth_nonce=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert claims["nonce"] == client.cookies.get("imbue_oauth_nonce")


def test_broker_oauth_start_honors_accounts_base_url_for_the_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com/")

    resp = client.get("/share/oauth/google/start?next=%2F", follow_redirects=False)

    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["redirect_uri"] == ["https://accounts.example.com/share/oauth/google/callback"]


def test_broker_oauth_start_404s_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _st = _make_broker_test_client(monkeypatch)

    resp = client.get("/share/oauth/google/start?next=%2F", follow_redirects=False)

    assert resp.status_code == 404


def test_broker_oauth_callback_signs_in_and_resumes_the_share_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full browser flow: start -> provider callback -> SSO cookie -> /share/authorize hands off a token."""
    client, backend, st_backend = _make_broker_test_client(monkeypatch)
    domain = _seed_active_share(backend)
    st_backend.register_provider("google", email="visitor@example.com", is_verified=True)
    callback_origin = f"https://auth-x7k9q2w1.{domain}"
    next_path = f"/share/authorize?{urlencode({'machine_domain': domain, 'next': '', 'callback_origin': callback_origin, 'state': 'n-1'})}"

    state = _start_broker_oauth(client, next_path)
    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == next_path
    assert "imbue_sso_session=" in resp.headers["set-cookie"]

    # The SSO cookie now carries the OAuth session; /share/authorize resolves
    # it to the OAuth account and mints the handoff token for that visitor.
    authorize = client.get(next_path, follow_redirects=False)

    assert authorize.status_code == 302
    assert authorize.headers["location"].startswith(f"{callback_origin}/_auth/callback?")
    token = parse_qs(urlsplit(authorize.headers["location"]).query)["token"][0]
    claims = pyjwt.decode(token, _TEST_BROKER_KEY.public_key(), algorithms=["RS256"], audience=domain)
    assert claims["email"] == "visitor@example.com"


def test_broker_oauth_callback_rejects_a_missing_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    state = _start_broker_oauth(client, "/share/authorize?a=b")
    client.cookies.delete("imbue_oauth_nonce", path="/share/oauth")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "could not be verified" in resp.text


def test_broker_oauth_callback_rejects_garbage_or_wrong_purpose_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")

    garbage = client.get("/share/oauth/google/callback?code=c&state=not-a-jwt", follow_redirects=False)
    assert garbage.status_code == 401
    assert "invalid or has expired" in garbage.text

    # A share handoff token is a valid signature under the same key but the
    # wrong purpose; it must not open the OAuth callback.
    handoff = mint_share_handoff_token(
        signing_key=_TEST_BROKER_KEY,
        user_id=_STUB_USER_ID,
        email=_STUB_EMAIL,
        machine_domain="x.example.com",
        nonce="n",
    )
    wrong_purpose = client.get(f"/share/oauth/google/callback?code=c&state={handoff}", follow_redirects=False)
    assert wrong_purpose.status_code == 401


def test_broker_oauth_callback_reports_provider_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google")
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?error=access_denied&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "cancelled" in resp.text


def test_broker_oauth_callback_refuses_an_email_registered_with_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="alice@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.register_provider("google", email="alice@example.com")
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 401
    assert "already signs in with a password" in resp.text
    assert "imbue_sso_session=" not in resp.headers.get("set-cookie", "")


def test_broker_oauth_callback_shows_verify_page_for_an_unverified_provider_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend, st_backend = _make_broker_test_client(monkeypatch)
    st_backend.register_provider("google", email="fresh@example.com", is_verified=False)
    state = _start_broker_oauth(client, "/share/authorize?a=b")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 200
    assert "Check your inbox" in resp.text
    assert len(st_backend.sent_verification_emails) == 1
    # The session cookie is still set, matching the password flow: reloading
    # the share link after verifying continues without another sign-in.
    assert "imbue_sso_session=" in resp.headers["set-cookie"]
