"""Tests for the hosted accounts surface (browser auth, device handoff, OAuth, attribution)."""

import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit

import jwt as pyjwt
import psycopg2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.session.exceptions import TryRefreshTokenError

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.share_broker as share_broker_module
from imbue.remote_service_connector.accounts_web import _mark_next_confirmed
from imbue.remote_service_connector.accounts_web import compute_pkce_challenge
from imbue.remote_service_connector.accounts_web import is_valid_loopback_redirect_uri
from imbue.remote_service_connector.attribution import ATTRIBUTION_COOKIE_NAME
from imbue.remote_service_connector.testing import FakeProvider
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryDeviceAuthCodeStore
from imbue.remote_service_connector.testing import _make_accounts_web_test_client
from imbue.remote_service_connector.testing import encode_attribution_cookie

_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_KEY_PEM = _TEST_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")


def _sign_in_browser(
    client: TestClient,
    st_backend: FakeSuperTokensBackend,
    email: str = "alice@example.com",
    verified: bool = False,
) -> str:
    """Sign up + establish a browser session on the client; returns the user id."""
    signup = st_backend.sign_up(tenant_id="public", email=email, password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    if verified:
        st_backend.mark_email_verified(signup.user.id)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    return signup.user.id


def _authorize_query(redirect_uri: str = "http://127.0.0.1:8123/callback", verifier: str = "v" * 43) -> dict[str, str]:
    return {
        "redirect_uri": redirect_uri,
        "code_challenge": compute_pkce_challenge(verifier),
        "state": "state-1",
    }


# ---------------------------------------------------------------------------
# Pages + config
# ---------------------------------------------------------------------------


def test_login_page_serves_placeholder_without_a_built_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", "/nonexistent-dist-dir")

    resp = client.get("/login")

    assert resp.status_code == 503
    assert "not built" in resp.text


def test_pages_serve_the_built_bundle_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    (tmp_path / "index.html").write_text("<!doctype html><title>Minds accounts</title>")
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", str(tmp_path))

    for page in ("/login", "/signup", "/manage", "/auth/reset-password", "/auth/verify-email", "/check-inbox"):
        resp = client.get(page)
        assert resp.status_code == 200
        assert "Minds accounts" in resp.text


def test_assets_route_serves_bundle_files_and_blocks_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('minds')")
    (tmp_path / "secret.txt").write_text("outside assets")
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", str(tmp_path))

    ok = client.get("/accounts/assets/app.js")
    assert ok.status_code == 200
    assert "minds" in ok.text

    missing = client.get("/accounts/assets/nope.js")
    assert missing.status_code == 404

    traversal = client.get("/accounts/assets/%2e%2e/secret.txt")
    assert traversal.status_code == 404


def test_config_reports_turnstile_and_google_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)

    before = client.get("/accounts/api/config").json()
    assert before == {"turnstile_site_key": "", "google_enabled": False}

    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key-1")
    st_backend.register_provider("google")
    after = client.get("/accounts/api/config").json()
    assert after == {"turnstile_site_key": "site-key-1", "google_enabled": True}


# ---------------------------------------------------------------------------
# Browser session APIs
# ---------------------------------------------------------------------------


def test_me_reports_signed_out_then_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    signed_out = client.get("/accounts/api/me")
    assert signed_out.status_code == 401
    assert signed_out.json() == {"signed_in": False}

    user_id = _sign_in_browser(client, st_backend, verified=True)
    signed_in = client.get("/accounts/api/me")
    assert signed_in.status_code == 200
    assert signed_in.json() == {
        "signed_in": True,
        "user_id": user_id,
        "email": "alice@example.com",
        "email_verified": True,
    }


def test_me_rejects_and_revokes_a_session_past_the_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend, verified=True)
    session = st_backend.last_browser_session
    assert session is not None
    # Fresh sessions carry the started-at stamp and resolve normally.
    assert client.get("/accounts/api/me").status_code == 200

    # Backdate the session past the ~30-day cap: it is refused AND revoked.
    session.access_token_payload[accounts_web_module._BROWSER_SESSION_STARTED_AT_CLAIM] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).timestamp()
    expired = client.get("/accounts/api/me")
    assert expired.status_code == 401
    assert session.access_token not in st_backend.sessions_by_access_token


def test_me_rejects_a_session_with_no_started_at_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session without a readable stamp (pre-cap mint, tampered payload) counts as expired."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend, verified=True)
    session = st_backend.last_browser_session
    assert session is not None

    session.access_token_payload = {}
    assert client.get("/accounts/api/me").status_code == 401


def test_browser_session_survives_a_refresh_within_the_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """The started-at stamp rides through token refreshes, so refreshing never extends the cap."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend, verified=True)
    session = st_backend.last_browser_session
    assert session is not None
    original_started_at = session.access_token_payload[accounts_web_module._BROWSER_SESSION_STARTED_AT_CLAIM]

    refreshed = st_backend.refresh_session(refresh_token=session.refresh_token)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, refreshed.access_token)

    assert client.get("/accounts/api/me").status_code == 200
    assert refreshed.access_token_payload[accounts_web_module._BROWSER_SESSION_STARTED_AT_CLAIM] == original_started_at


def test_browser_signup_creates_account_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)

    resp = client.post("/accounts/api/signup", json={"email": "new@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    # A browser session was established for the new account.
    assert st_backend.last_browser_session is not None
    assert st_backend.last_browser_session.user_id == body["user"]["user_id"]
    # No verification email at signup (contextual sends only).
    assert st_backend.sent_verification_emails == []


def test_browser_signup_rejects_failed_turnstile(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-1")
    st_backend.is_turnstile_passing = False

    resp = client.post("/accounts/api/signup", json={"email": "bot@example.com", "password": "password123"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "TURNSTILE_FAILED"
    assert "bot@example.com" not in st_backend.accounts_by_email


def test_browser_signup_rejects_duplicate_and_cross_method_emails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    client.post("/accounts/api/signup", json={"email": "dup@example.com", "password": "password123"})
    dup = client.post("/accounts/api/signup", json={"email": "dup@example.com", "password": "password123"})
    assert dup.json()["status"] == "EMAIL_ALREADY_EXISTS"

    st_backend.add_third_party_account(provider_id="google", email="g@example.com", third_party_user_id="tp-1")
    cross = client.post("/accounts/api/signup", json={"email": "g@example.com", "password": "password123"})
    assert cross.json()["status"] == "ACCOUNT_EXISTS_WITH_OTHER_METHOD"


def test_browser_signup_rejects_weak_password_and_malformed_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public signup applies the SDK's default form validation server-side (not just in the frontend)."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    weak = client.post("/accounts/api/signup", json={"email": "weak@example.com", "password": "short"})
    assert weak.json()["status"] == "FIELD_ERROR"

    malformed = client.post("/accounts/api/signup", json={"email": "not-an-email", "password": "password123"})
    assert malformed.json()["status"] == "FIELD_ERROR"

    assert st_backend.accounts_by_email == {}


def test_browser_signup_rejects_cross_site_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)

    resp = client.post(
        "/accounts/api/signup",
        json={"email": "x@example.com", "password": "password123"},
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403


def test_browser_signin_happy_path_and_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="alice@example.com", password="pw-123456")

    ok = client.post("/accounts/api/signin", json={"email": "alice@example.com", "password": "pw-123456"})
    assert ok.json()["status"] == "OK"
    assert st_backend.last_browser_session is not None

    wrong = client.post("/accounts/api/signin", json={"email": "alice@example.com", "password": "nope"})
    assert wrong.json()["status"] == "WRONG_CREDENTIALS"


def test_browser_signout_revokes_only_this_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    user_id = _sign_in_browser(client, st_backend)
    other_session = st_backend.sdk_create_browser_session(None, user_id)

    resp = client.post("/accounts/api/signout")

    assert resp.json() == {"status": "OK"}
    assert client.get("/accounts/api/me").status_code == 401
    # The user's other session (another device/browser) survives.
    assert other_session.access_token in st_backend.sessions_by_access_token


def test_browser_signout_answers_401_when_the_access_token_needs_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired-but-refreshable token must get a 401, not a false OK.

    The SDK raises TryRefreshTokenError for a parseable-but-expired access
    token even with session_required=False. Answering OK would report a
    successful sign-out while the refresh token stays alive; the 401 engages
    the frontend's transparent refresh-and-retry so the revocation lands.
    """
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    st_backend.raise_on("sdk_get_browser_session", TryRefreshTokenError("token expired"))

    resp = client.post("/accounts/api/signout")

    assert resp.status_code == 401
    # Nothing was revoked: the session is still alive server-side.
    assert st_backend.sessions_by_access_token


def test_real_browser_session_seam_checks_the_core_database() -> None:
    """The real seam must pass check_database=True to the SDK.

    Without it SuperTokens verifies access tokens statelessly (a
    signature-valid token never consults the core), so a session revoked by
    sign-out would keep resolving -- and keep minting device codes and share
    handoffs -- until the access token expires. The fake backend replaces this
    seam wholesale, so the SDK arguments are asserted here directly.
    """
    captured_kwargs: dict[str, object] = {}

    def capturing_get_session(request: object, **kwargs: object) -> None:
        del request
        captured_kwargs.update(kwargs)
        return None

    request = Request({"type": "http", "method": "GET", "headers": []})
    result = accounts_web_module._sdk_get_browser_session(request, get_session_fn=capturing_get_session)

    assert result is None
    assert captured_kwargs["check_database"] is True
    assert captured_kwargs["session_required"] is False


def test_browser_signout_all_revokes_every_session(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    user_id = _sign_in_browser(client, st_backend)
    st_backend.sdk_create_browser_session(None, user_id)
    # Re-plant the first session cookie (creating the second overwrote last_browser_session only).

    resp = client.post("/accounts/api/signout-all")

    assert resp.json()["status"] == "OK"
    assert resp.json()["revoked_count"] == 2
    assert not st_backend.sessions_by_access_token


def test_change_password_requires_current_password(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)

    wrong = client.post(
        "/accounts/api/change-password",
        json={"current_password": "wrong", "new_password": "newpassword1"},
    )
    assert wrong.json()["status"] == "WRONG_CREDENTIALS"

    ok = client.post(
        "/accounts/api/change-password",
        json={"current_password": "pw-123456", "new_password": "newpassword1"},
    )
    assert ok.json()["status"] == "OK"
    assert st_backend.accounts_by_email["alice@example.com"].password == "newpassword1"


def test_send_verification_sends_for_unverified_and_skips_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend, verified=False)

    first = client.post("/accounts/api/send-verification")
    assert first.json() == {"status": "OK", "sent": True, "already_verified": False}
    assert len(st_backend.sent_verification_emails) == 1

    account = st_backend.accounts_by_email["alice@example.com"]
    st_backend.mark_email_verified(account.user_id)
    verified = client.post("/accounts/api/send-verification")
    assert verified.json() == {"status": "OK", "sent": False, "already_verified": True}
    assert len(st_backend.sent_verification_emails) == 1


# ---------------------------------------------------------------------------
# Device handoff: authorize + token exchange
# ---------------------------------------------------------------------------


def test_loopback_redirect_uri_validation() -> None:
    assert is_valid_loopback_redirect_uri("http://127.0.0.1:8123/callback")
    assert is_valid_loopback_redirect_uri("http://localhost:39241/cb")
    assert is_valid_loopback_redirect_uri("http://[::1]:8123/callback")
    assert not is_valid_loopback_redirect_uri("https://127.0.0.1:8123/callback")
    assert not is_valid_loopback_redirect_uri("http://evil.example.com/callback")
    assert not is_valid_loopback_redirect_uri("http://127.0.0.1.evil.example:80/cb")
    assert not is_valid_loopback_redirect_uri("http://127.0.0.1/callback")


def test_authorize_without_session_redirects_to_login_with_resumable_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    query = _authorize_query()

    resp = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)

    assert resp.status_code == 302
    expected_next = f"/accounts/authorize?{urlencode(query)}"
    assert resp.headers["location"] == f"/login?next={quote(expected_next, safe='')}"


def test_authorize_with_session_but_no_confirmation_redirects_to_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing session never silently authorizes a device: the interstitial must confirm it."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)

    resp = client.get(f"/accounts/authorize?{urlencode(_authorize_query())}", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?next=")


def test_authorize_rejects_non_loopback_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    query = _authorize_query(redirect_uri="https://evil.example.com/steal")
    query["confirmed"] = "1"

    resp = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)

    assert resp.status_code == 400


def test_authorize_and_exchange_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full handoff: confirmed authorize mints a code, the exchange returns a fresh session."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    user_id = _sign_in_browser(client, st_backend, verified=False)
    verifier = secrets.token_urlsafe(32)
    query = _authorize_query(verifier=verifier)
    query["confirmed"] = "1"

    authorize = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)

    assert authorize.status_code == 302
    location = authorize.headers["location"]
    assert location.startswith("http://127.0.0.1:8123/callback?")
    callback_query = parse_qs(urlsplit(location).query)
    assert callback_query["state"] == ["state-1"]
    code = callback_query["code"][0]

    exchange = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:8123/callback"},
    )

    assert exchange.status_code == 200
    body = exchange.json()
    assert body["status"] == "OK"
    assert body["user"]["user_id"] == user_id
    assert body["user"]["email"] == "alice@example.com"
    # The device got its own fresh session, not the browser's.
    device_access_token = body["tokens"]["access_token"]
    assert device_access_token in st_backend.sessions_by_access_token
    assert device_access_token != client.cookies.get(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE)
    assert body["tokens"]["refresh_token"]

    # Single use: a replay of the same code is refused.
    replay = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:8123/callback"},
    )
    assert replay.status_code == 400


def test_exchange_rejects_wrong_verifier_and_wrong_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    verifier = secrets.token_urlsafe(32)
    query = _authorize_query(verifier=verifier)
    query["confirmed"] = "1"
    authorize = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)
    code = parse_qs(urlsplit(authorize.headers["location"]).query)["code"][0]

    wrong_verifier = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": "not-the-verifier", "redirect_uri": "http://127.0.0.1:8123/callback"},
    )
    assert wrong_verifier.status_code == 400

    # The failed PKCE check consumed the code (single use); mint a fresh one
    # to check the redirect_uri binding independently.
    authorize2 = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)
    code2 = parse_qs(urlsplit(authorize2.headers["location"]).query)["code"][0]
    wrong_uri = client.post(
        "/auth/device/token",
        json={"code": code2, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:9999/other"},
    )
    assert wrong_uri.status_code == 400


def test_exchange_rejects_expired_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, code_store = _make_accounts_web_test_client(monkeypatch)
    _sign_in_browser(client, st_backend)
    verifier = secrets.token_urlsafe(32)
    query = _authorize_query(verifier=verifier)
    query["confirmed"] = "1"
    authorize = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)
    code = parse_qs(urlsplit(authorize.headers["location"]).query)["code"][0]
    # Age the stored row past its expiry.
    assert isinstance(code_store, InMemoryDeviceAuthCodeStore)
    for row in code_store.rows_by_code_hash.values():
        row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    resp = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:8123/callback"},
    )

    assert resp.status_code == 400


def test_mark_next_confirmed_only_touches_authorize_paths() -> None:
    assert _mark_next_confirmed("/accounts/authorize?a=b") == "/accounts/authorize?a=b&confirmed=1"
    assert _mark_next_confirmed("/accounts/authorize") == "/accounts/authorize?confirmed=1"
    assert _mark_next_confirmed("/accounts/authorize?confirmed=1&a=b") == "/accounts/authorize?confirmed=1&a=b"
    assert _mark_next_confirmed("/share/authorize?a=b") == "/share/authorize?a=b&confirmed=1"
    assert _mark_next_confirmed("/share/authorize") == "/share/authorize?confirmed=1"
    assert _mark_next_confirmed("/share/authorize?confirmed=1&a=b") == "/share/authorize?confirmed=1&a=b"
    assert _mark_next_confirmed("/manage") == "/manage"
    assert _mark_next_confirmed("/") == "/"


# ---------------------------------------------------------------------------
# Browser Google OAuth on the merged surface
# ---------------------------------------------------------------------------


def _make_oauth_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeSuperTokensBackend]:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", _TEST_KEY_PEM)
    st_backend.register_provider("google", email="visitor@example.com", is_verified=True)
    return client, st_backend


def _start_oauth(client: TestClient, next_path: str) -> str:
    resp = client.get(f"/accounts/oauth/google/start?next={quote(next_path, safe='')}", follow_redirects=False)
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]


def test_oauth_start_redirects_with_signed_state_and_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st = _make_oauth_client(monkeypatch)

    resp = client.get("/accounts/oauth/google/start?next=%2Faccounts%2Fauthorize%3Fa%3Db", follow_redirects=False)

    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert location.netloc == "google.example.com"
    query = parse_qs(location.query)
    # The redirect URI is our own registered callback path, derived from the request.
    assert query["redirect_uri"] == ["https://testserver/share/oauth/google/callback"]
    claims = pyjwt.decode(query["state"][0], _TEST_KEY.public_key(), algorithms=["RS256"])
    assert claims["purpose"] == "accounts_oauth"
    assert claims["next"] == "/accounts/authorize?a=b"
    assert claims["cb"] == "https://testserver/share/oauth/google/callback"
    assert claims["nonce"] == client.cookies.get("imbue_oauth_nonce")


def test_oauth_start_uses_the_tier_redirector_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev/CI tiers register only the fixed redirector; the state carries the real callback."""
    client, _st = _make_oauth_client(monkeypatch)
    monkeypatch.setenv("OAUTH_REDIRECTOR_URL", "https://oauth-redirector.example.com/forward")

    resp = client.get("/accounts/oauth/google/start?next=%2F", follow_redirects=False)

    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["redirect_uri"] == ["https://oauth-redirector.example.com/forward"]
    claims = pyjwt.decode(query["state"][0], _TEST_KEY.public_key(), algorithms=["RS256"])
    assert claims["cb"] == "https://testserver/share/oauth/google/callback"


def test_oauth_start_404s_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", _TEST_KEY_PEM)

    resp = client.get("/accounts/oauth/google/start?next=%2F", follow_redirects=False)

    assert resp.status_code == 404


def test_oauth_callback_signs_in_and_marks_a_pending_authorize_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit OAuth login IS the confirmation, so the pending handoff resumes without an interstitial."""
    client, st_backend = _make_oauth_client(monkeypatch)
    next_path = f"/accounts/authorize?{urlencode(_authorize_query())}"
    state = _start_oauth(client, next_path)

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"{next_path}&confirmed=1"
    # The browser session belongs to the OAuth account.
    assert st_backend.last_browser_session is not None
    account = st_backend.accounts_by_email["visitor@example.com"]
    assert st_backend.last_browser_session.user_id == account.user_id
    # Exactly one session exists: the cookie session. The code exchange must
    # not also mint a bearer session -- nothing would ever deliver or revoke
    # it, so it would linger in the core as an orphan.
    user_sessions = [s for s in st_backend.sessions_by_access_token.values() if s.user_id == account.user_id]
    assert len(user_sessions) == 1


class _ProviderExplosionError(Exception):
    """Stands in for the SDK provider layer's failure exceptions.

    The real ones include httpx transport errors, pyjwt errors, and plain
    ``Exception`` raises (e.g. "third party user id is missing" for a
    consumed/expired code) -- none of them domain error types.
    """


def test_oauth_callback_redirects_cleanly_when_the_provider_exchange_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider-layer failure mid-exchange must become the oauth_failed banner, not a raw 500."""
    client, st_backend = _make_oauth_client(monkeypatch)

    class _ExplodingProvider(FakeProvider):
        # A plain def (deliberately not a coroutine, keeping the asynchrony
        # ratchet flat) is fine here: the handler calls the method inside its
        # try block before running it, so a synchronous raise takes the
        # identical code path.
        def exchange_auth_code_for_oauth_tokens(
            self,
            redirect_uri_info: object,
            user_context: dict[str, object],
        ) -> NoReturn:
            raise _ProviderExplosionError("third party user id is missing")

    exploding = _ExplodingProvider()
    exploding.provider_id = "google"
    st_backend.registered_providers["google"] = exploding
    state = _start_oauth(client, "/")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
    assert "error=oauth_failed" in resp.headers["location"]


def test_oauth_callback_rejects_missing_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st = _make_oauth_client(monkeypatch)
    state = _start_oauth(client, "/")
    client.cookies.delete("imbue_oauth_nonce")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert resp.headers["location"].startswith("/login")


def test_oauth_callback_rejects_non_ascii_nonce_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crafted cookie value must yield the clean nonce_mismatch redirect, not a 500.

    compare_digest raises TypeError on str operands with non-ASCII characters,
    so the comparison has to run over bytes.
    """
    client, _st = _make_oauth_client(monkeypatch)
    state = _start_oauth(client, "/")
    client.cookies.delete("imbue_oauth_nonce")

    resp = client.get(
        f"/share/oauth/google/callback?code=code-1&state={state}",
        follow_redirects=False,
        headers={b"Cookie": "imbue_oauth_nonce=nonc\xe9".encode("latin-1")},
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
    assert "error=" in resp.headers["location"]


def test_oauth_callback_rejects_garbage_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st = _make_oauth_client(monkeypatch)

    resp = client.get("/share/oauth/google/callback?code=c&state=not-a-jwt", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?")
    assert "error=" in resp.headers["location"]


def test_oauth_callback_rejects_wrong_purpose_state_under_the_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A share-handoff JWT is validly signed under the SAME key but has the wrong purpose.

    verify_oauth_state's purpose check is the only thing separating the two
    token types minted under BROKER_JWT_SIGNING_KEY_PEM, so a handoff token
    must not open the OAuth callback.
    """
    client, _st = _make_oauth_client(monkeypatch)
    handoff = share_broker_module.mint_share_handoff_token(
        signing_key=_TEST_KEY,
        user_id="user-1",
        email="visitor@example.com",
        machine_domain="x.example.com",
        nonce="n",
        is_owner=False,
    )

    resp = client.get(f"/share/oauth/google/callback?code=c&state={handoff}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?")
    assert "error=" in resp.headers["location"]


def test_oauth_callback_reports_provider_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st = _make_oauth_client(monkeypatch)
    state = _start_oauth(client, "/manage")

    resp = client.get(f"/share/oauth/google/callback?error=access_denied&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert "cancelled" in resp.headers["location"].replace("+", " ")


def test_oauth_callback_refuses_an_email_registered_with_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_oauth_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="visitor@example.com", password="pw-123456")
    state = _start_oauth(client, "/")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert "password" in resp.headers["location"].replace("+", " ")
    # No browser session was minted for the refused login.
    assert st_backend.last_browser_session is None


# ---------------------------------------------------------------------------
# Marketing attribution (signup capture + the /download redirect)
# ---------------------------------------------------------------------------


def _plant_attribution_cookie(client: TestClient) -> None:
    client.cookies.set(
        ATTRIBUTION_COOKIE_NAME,
        encode_attribution_cookie(
            {
                "v": 1,
                "id": "visitor-1",
                "first": {"utm_source": "ads", "utm_campaign": "launch", "at": "t1"},
                "last": {"utm_source": "newsletter", "at": "t2"},
            }
        ),
    )


def test_browser_signup_records_attribution_from_cookie_and_page_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cookie supplies visitor id + first touch; the signup page's own params overwrite last."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _plant_attribution_cookie(client)

    resp = client.post(
        "/accounts/api/signup",
        json={
            "email": "new@example.com",
            "password": "pw-123456",
            "attribution_page_query": "utm_source=signup-link&next=%2Faccounts%2Fauthorize",
            "attribution_page_path": "/signup",
            "attribution_next": "/accounts/authorize?redirect_uri=x",
        },
    )

    assert resp.json()["status"] == "OK"
    rows = st_backend.attribution_store.account_rows
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "new@example.com"
    assert row["visitor_id"] == "visitor-1"
    assert row["first_touch"] == {"utm_source": "ads", "utm_campaign": "launch", "at": "t1"}
    assert row["last_touch"]["utm_source"] == "signup-link"
    assert row["last_touch"]["path"] == "/signup"
    assert row["signup_context"] == "desktop_app"
    assert row["signup_method"] == "password"


def test_browser_signup_without_cookie_or_params_records_an_unattributed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    resp = client.post("/accounts/api/signup", json={"email": "plain@example.com", "password": "pw-123456"})

    assert resp.json()["status"] == "OK"
    rows = st_backend.attribution_store.account_rows
    assert len(rows) == 1
    assert rows[0]["visitor_id"] is None
    assert rows[0]["first_touch"] is None
    assert rows[0]["last_touch"] is None
    assert rows[0]["signup_context"] == "web"


def test_browser_signin_and_refused_signup_record_no_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _plant_attribution_cookie(client)
    signup = client.post("/accounts/api/signup", json={"email": "once@example.com", "password": "pw-123456"})
    assert signup.json()["status"] == "OK"
    assert len(st_backend.attribution_store.account_rows) == 1

    # A sign-in of the existing account records nothing new.
    signin = client.post("/accounts/api/signin", json={"email": "once@example.com", "password": "pw-123456"})
    assert signin.json()["status"] == "OK"
    assert len(st_backend.attribution_store.account_rows) == 1

    # A refused signup (Turnstile) records nothing.
    st_backend.is_turnstile_passing = False
    refused = client.post("/accounts/api/signup", json={"email": "bot@example.com", "password": "pw-123456"})
    assert refused.json()["status"] == "TURNSTILE_FAILED"
    assert len(st_backend.attribution_store.account_rows) == 1


def test_browser_signup_succeeds_when_the_attribution_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attribution capture fails open: a storage error must never break account creation."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    st_backend.attribution_store.raise_on_insert = psycopg2.OperationalError("neon is down")

    resp = client.post("/accounts/api/signup", json={"email": "resilient@example.com", "password": "pw-123456"})

    assert resp.json()["status"] == "OK"
    assert st_backend.attribution_store.account_rows == []


def test_oauth_signup_records_attribution_but_returning_signin_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the exchange that CREATED the account stamps attribution (context from next, params from pq/pp)."""
    client, st_backend = _make_oauth_client(monkeypatch)
    _plant_attribution_cookie(client)
    start = client.get(
        "/accounts/oauth/google/start?next=%2Fweb%2Foverview&pq=utm_source%3Dsignup-link&pp=%2Fsignup",
        follow_redirects=False,
    )
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    first_login = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert first_login.status_code == 303
    rows = st_backend.attribution_store.account_rows
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "visitor@example.com"
    assert row["visitor_id"] == "visitor-1"
    assert row["first_touch"] == {"utm_source": "ads", "utm_campaign": "launch", "at": "t1"}
    assert row["last_touch"]["utm_source"] == "signup-link"
    assert row["last_touch"]["path"] == "/signup"
    assert row["signup_context"] == "web_chrome"
    assert row["signup_method"] == "google"

    # The same Google account signing in again records nothing new.
    state2 = _start_oauth(client, "/manage")
    returning = client.get(f"/share/oauth/google/callback?code=code-2&state={state2}", follow_redirects=False)
    assert returning.status_code == 303
    assert len(st_backend.attribution_store.account_rows) == 1


def test_download_redirects_per_platform_and_404s_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)

    mac = client.get("/download?platform=mac-arm64", follow_redirects=False)
    assert mac.status_code == 302
    assert mac.headers["location"] == "https://dl.todesktop.com/26032588hqdzk/mac/dmg/arm64"

    # The friendly alias resolves server-side to the same target.
    alias = client.get("/download?platform=mac", follow_redirects=False)
    assert alias.headers["location"] == mac.headers["location"]

    source = client.get("/download?platform=source", follow_redirects=False)
    assert source.status_code == 302
    assert source.headers["location"] == "https://github.com/imbue-ai/mngr"

    assert client.get("/download?platform=windows", follow_redirects=False).status_code == 404
    assert client.get("/download", follow_redirects=False).status_code == 404


def test_download_records_an_event_tagged_from_the_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    _plant_attribution_cookie(client)

    resp = client.get(
        "/download?platform=mac", follow_redirects=False, headers={"user-agent": "TestBrowser/1.0 36284"}
    )

    assert resp.status_code == 302
    rows = st_backend.attribution_store.download_rows
    assert len(rows) == 1
    row = rows[0]
    # The alias is resolved before recording, so SQL groups by canonical name.
    assert row["platform"] == "mac-arm64"
    assert row["visitor_id"] == "visitor-1"
    assert row["first_touch"] == {"utm_source": "ads", "utm_campaign": "launch", "at": "t1"}
    assert row["last_touch"] == {"utm_source": "newsletter", "at": "t2"}
    assert row["user_agent"] == "TestBrowser/1.0 36284"


def test_download_without_cookie_tags_the_event_from_its_own_url_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consent-declined visitors have no cookie; campaign params on the link itself still count."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    resp = client.get("/download?platform=mac-arm64&utm_source=launch-email", follow_redirects=False)

    assert resp.status_code == 302
    rows = st_backend.attribution_store.download_rows
    assert len(rows) == 1
    assert rows[0]["visitor_id"] is None
    assert rows[0]["first_touch"]["utm_source"] == "launch-email"
    assert rows[0]["first_touch"] == rows[0]["last_touch"]


def test_download_still_redirects_when_the_event_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    st_backend.attribution_store.raise_on_insert = psycopg2.OperationalError("neon is down")

    resp = client.get("/download?platform=mac-arm64", follow_redirects=False)

    assert resp.status_code == 302
    assert st_backend.attribution_store.download_rows == []
