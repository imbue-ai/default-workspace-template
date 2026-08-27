"""Tests for the hosted accounts surface (browser auth, device handoff, OAuth, attribution)."""

import re
import secrets
import tomllib
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlsplit

import jwt as pyjwt
import psycopg2
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.session.exceptions import TryRefreshTokenError

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.share_broker as share_broker_module
from imbue.remote_service_connector import accounts_web
from imbue.remote_service_connector.accounts_web import _mark_next_confirmed
from imbue.remote_service_connector.accounts_web import compute_pkce_challenge
from imbue.remote_service_connector.accounts_web import is_valid_loopback_redirect_uri
from imbue.remote_service_connector.attribution import ATTRIBUTION_COOKIE_NAME
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.errors import DownloadLinkError
from imbue.remote_service_connector.testing import FakeProvider
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryDeviceAuthCodeStore
from imbue.remote_service_connector.testing import TEST_OAUTH_SIGNING_KEY
from imbue.remote_service_connector.testing import TEST_OAUTH_SIGNING_KEY_PEM
from imbue.remote_service_connector.testing import _make_accounts_web_test_client
from imbue.remote_service_connector.testing import _make_share_test_client_with_fakes
from imbue.remote_service_connector.testing import encode_attribution_cookie
from imbue.remote_service_connector.testing import hold_stable_download_link


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


def test_account_pages_are_refused_off_the_accounts_origin_with_a_link_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a dedicated accounts origin configured, the identity pages cannot work
    on other hosts (the session cookie's Domain is the accounts apex), so they are
    refused with a link to the same page on the right origin."""
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    (tmp_path / "index.html").write_text("<!doctype html><title>Minds accounts</title>")
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", str(tmp_path))
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com")

    refused = client.get("/login?next=%2Fmanage")
    assert refused.status_code == 421
    assert "https://accounts.example.com/login?next=%2Fmanage" in refused.text

    served = client.get("https://accounts.example.com/login?next=%2Fmanage")
    assert served.status_code == 200
    assert "Minds accounts" in served.text


def test_account_pages_still_serve_on_the_chrome_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The chrome origin shares the apex session cookie, so the account pages keep working there."""
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    (tmp_path / "index.html").write_text("<!doctype html><title>Minds accounts</title>")
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", str(tmp_path))
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com")
    monkeypatch.setenv("SHARE_CHROME_ORIGIN", "https://chrome.example.com")

    for page in ("/login", "/signup", "/manage"):
        resp = client.get(f"https://chrome.example.com{page}")
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
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", TEST_OAUTH_SIGNING_KEY_PEM)
    st_backend.register_provider("google", email="visitor@example.com", is_verified=True)
    return client, st_backend


def _start_oauth(client: TestClient, next_path: str, is_terms_accepted: bool = True, plan: str = "") -> str:
    # Terms ride the start URL by default (the signup tab's button always
    # carries them); pass False to model the sign-in tab's button. A non-empty
    # plan models the signup tab's plan selector.
    terms_suffix = "&terms=1" if is_terms_accepted else ""
    plan_suffix = f"&plan={quote(plan, safe='')}" if plan else ""
    resp = client.get(
        f"/accounts/oauth/google/start?next={quote(next_path, safe='')}{terms_suffix}{plan_suffix}",
        follow_redirects=False,
    )
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
    claims = pyjwt.decode(query["state"][0], TEST_OAUTH_SIGNING_KEY.public_key(), algorithms=["RS256"])
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
    claims = pyjwt.decode(query["state"][0], TEST_OAUTH_SIGNING_KEY.public_key(), algorithms=["RS256"])
    assert claims["cb"] == "https://testserver/share/oauth/google/callback"


def test_oauth_start_is_refused_off_the_accounts_origin_even_on_the_chrome_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Google flow started anywhere but the accounts origin can never complete
    (host-only nonce cookie; the callback is registered on the accounts origin),
    so the start is refused up front -- on the chrome origin included -- with a
    link to the accounts-origin login carrying the pending ``next``."""
    client, _st = _make_oauth_client(monkeypatch)
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com")
    monkeypatch.setenv("SHARE_CHROME_ORIGIN", "https://chrome.example.com")

    for start_host in ("https://testserver", "https://chrome.example.com"):
        refused = client.get(
            f"{start_host}/accounts/oauth/google/start?next=%2Fmanage",
            follow_redirects=False,
        )
        assert refused.status_code == 421
        assert "https://accounts.example.com/login?next=%2Fmanage" in refused.text
        assert "imbue_oauth_nonce" not in refused.headers.get("set-cookie", "")

    started = client.get(
        "https://accounts.example.com/accounts/oauth/google/start?next=%2Fmanage",
        follow_redirects=False,
    )
    assert started.status_code == 302
    assert "imbue_oauth_nonce" in started.headers.get("set-cookie", "")


def test_oauth_start_404s_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("BROKER_JWT_SIGNING_KEY_PEM", TEST_OAUTH_SIGNING_KEY_PEM)

    resp = client.get("/accounts/oauth/google/start?next=%2F", follow_redirects=False)

    assert resp.status_code == 404


def _make_base_url_request(scheme: str, host: str, forwarded_proto: str | None) -> Request:
    """A minimal ASGI request for exercising ``accounts_public_base_url`` directly."""
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("latin-1"))]
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": "/",
        "query_string": b"",
        "headers": headers,
        "server": (host, 443 if scheme == "https" else 80),
    }
    return Request(scope)


def test_accounts_public_base_url_prefers_configured_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCOUNTS_BASE_URL", "https://accounts.example.com")
    request = _make_base_url_request("https", "rsc-dev.modal.run", forwarded_proto="http")

    assert accounts_web_module.accounts_public_base_url(request) == "https://accounts.example.com"


def test_accounts_public_base_url_trusts_a_plain_forwarded_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNTS_BASE_URL", raising=False)
    # Modal terminates TLS at ingress, so the ASGI scheme is http and the
    # https origin must be recovered from the forwarded-proto header.
    request = _make_base_url_request("http", "rsc-dev.modal.run", forwarded_proto="https")

    assert accounts_web_module.accounts_public_base_url(request) == "https://rsc-dev.modal.run"


def test_accounts_public_base_url_falls_back_when_header_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNTS_BASE_URL", raising=False)
    request = _make_base_url_request("https", "rsc-dev.modal.run", forwarded_proto=None)

    assert accounts_web_module.accounts_public_base_url(request) == "https://rsc-dev.modal.run"


@pytest.mark.parametrize(
    "poisoned_proto",
    [
        "https://evil.example/?",
        "https://evil.example",
        "javascript:alert(1)//",
        "https ",
        "ftp",
        "minds",
    ],
)
def test_accounts_public_base_url_rejects_a_poisoned_forwarded_scheme(
    monkeypatch: pytest.MonkeyPatch, poisoned_proto: str
) -> None:
    """An untrusted forwarded-proto never reaches the f-string, so it can never
    change the constructed URL's effective host."""
    monkeypatch.delenv("ACCOUNTS_BASE_URL", raising=False)
    request = _make_base_url_request("https", "rsc-dev.modal.run", forwarded_proto=poisoned_proto)

    base_url = accounts_web_module.accounts_public_base_url(request)

    # The clamp falls back to the ASGI scheme; the host stays the real request host.
    assert base_url == "https://rsc-dev.modal.run"
    assert urlsplit(base_url).hostname == "rsc-dev.modal.run"


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
        signing_key=TEST_OAUTH_SIGNING_KEY,
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
# Signup plan choice + terms agreement + the static doc pages
# ---------------------------------------------------------------------------


def test_browser_signup_records_the_selected_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    resp = client.post(
        "/accounts/api/signup", json={"email": "new@example.com", "password": "pw-123456", "plan": "free"}
    )

    assert resp.json()["status"] == "OK"
    user_id = resp.json()["user"]["user_id"]
    row = st_backend.entitlements_store.get_entitlements(user_id)
    assert row is not None
    assert row["plan_name"] == "free"
    assert row["user_id_prefix"] == user_id.replace("-", "")[:16]


def test_browser_signup_ignores_an_unknown_or_absent_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the signup selector's plans are honored; anything else defers to the lazy backfill."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)

    crafted = client.post(
        "/accounts/api/signup", json={"email": "crafty@example.com", "password": "pw-123456", "plan": "ally"}
    )
    assert crafted.json()["status"] == "OK"

    legacy = client.post("/accounts/api/signup", json={"email": "old-frontend@example.com", "password": "pw-123456"})
    assert legacy.json()["status"] == "OK"

    assert st_backend.entitlements_store.rows_by_user_id == {}


def test_browser_signup_succeeds_when_the_plan_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan choice fails open: the lazy backfill's free default is consent-safe."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    st_backend.entitlements_store.raise_on_insert = psycopg2.OperationalError("neon is down")

    resp = client.post(
        "/accounts/api/signup", json={"email": "resilient@example.com", "password": "pw-123456", "plan": "explorer"}
    )

    assert resp.json()["status"] == "OK"
    assert st_backend.entitlements_store.rows_by_user_id == {}


def test_oauth_signup_carries_the_plan_and_terms_through_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signup tab's Google button carries plan + terms; a new account gets its chosen row."""
    client, st_backend = _make_oauth_client(monkeypatch)
    state = _start_oauth(client, "/manage", plan="free")

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/manage"
    account = st_backend.accounts_by_email["visitor@example.com"]
    row = st_backend.entitlements_store.get_entitlements(account.user_id)
    assert row is not None
    assert row["plan_name"] == "free"


def test_oauth_new_account_without_terms_is_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Google exchange creating an account without the terms agreement (the sign-in
    tab's button) is rolled back and bounced to the terms_required banner."""
    client, st_backend = _make_oauth_client(monkeypatch)
    state = _start_oauth(client, "/manage", is_terms_accepted=False)

    resp = client.get(f"/share/oauth/google/callback?code=code-1&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
    assert "error=terms_required" in resp.headers["location"]
    # The just-created account was rolled back: no account, no session, no
    # attribution, no entitlements row.
    assert "visitor@example.com" not in st_backend.accounts_by_email
    assert st_backend.last_browser_session is None
    assert st_backend.attribution_store.account_rows == []
    assert st_backend.entitlements_store.rows_by_user_id == {}


def test_oauth_returning_signin_needs_no_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    """The terms gate applies only to account CREATION; returning Google sign-ins are untouched."""
    client, st_backend = _make_oauth_client(monkeypatch)
    signup_state = _start_oauth(client, "/manage", plan="explorer")
    created = client.get(f"/share/oauth/google/callback?code=code-1&state={signup_state}", follow_redirects=False)
    assert created.status_code == 303
    assert "error=" not in created.headers["location"]

    # The same account signing in again from the sign-in tab (no plan/terms).
    signin_state = _start_oauth(client, "/manage", is_terms_accepted=False)
    returning = client.get(f"/share/oauth/google/callback?code=code-2&state={signin_state}", follow_redirects=False)

    assert returning.status_code == 303
    assert returning.headers["location"] == "/manage"
    account = st_backend.accounts_by_email["visitor@example.com"]
    row = st_backend.entitlements_store.get_entitlements(account.user_id)
    assert row is not None
    assert row["plan_name"] == "explorer"


def test_terms_conduct_and_privacy_pages_serve_from_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    monkeypatch.setenv("ACCOUNTS_FRONTEND_DIST", str(tmp_path))
    page_title_by_path = {
        "/terms-of-service": "Terms of Service",
        "/code-of-conduct": "Code of Conduct",
        "/privacy-policy": "Privacy Policy",
    }
    for path, title in page_title_by_path.items():
        # Missing from the dist (an unbuilt bundle) answers the 503 placeholder.
        assert client.get(path).status_code == 503
        (tmp_path / f"{path.lstrip('/')}.html").write_text(f"<!doctype html><h1>{title}</h1>")
        served = client.get(path)
        assert served.status_code == 200
        assert title in served.text


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
        "/accounts/oauth/google/start?next=%2Fweb%2Foverview&pq=utm_source%3Dsignup-link&pp=%2Fsignup&terms=1",
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
    # Resolved from a fixture manifest, so this covers routing rather than what
    # stable happens to serve -- and so it does not reach the network.
    _hold_stable_download(_STABLE_MANIFEST)

    mac = client.get("/download?platform=mac-arm64", follow_redirects=False)
    assert mac.status_code == 302
    assert mac.headers["location"] == _STABLE_ARM64_DMG

    # The friendly alias resolves server-side to the same target.
    alias = client.get("/download?platform=mac", follow_redirects=False)
    assert alias.headers["location"] == mac.headers["location"]

    # Only mac-arm64 resolves; every other platform keeps its declared target.
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


# /download is the link marketing hands to a person, so it has to serve what the
# stable channel serves.

# Shaped like the live feed: an arm64-only fixture would let a resolver that
# picks any .dmg at all pass.
_STABLE_MANIFEST = """version: 0.4.1
files:
  - url: https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-x64-mac.zip
    sha512: abc==
    size: 303377197
  - url: https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-arm64-mac.zip
    sha512: def==
    size: 296819574
  - url: https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-x64.dmg
    sha512: ghi==
    size: 309416827
  - url: https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-arm64.dmg
    sha512: jkl==
    size: 302771815
path: https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-x64-mac.zip
sha512: abc==
releaseDate: '2026-08-18T23:46:47.920Z'
"""


_STABLE_ARM64_DMG = "https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-arm64.dmg"


def _parse_manifest(manifest: str) -> str:
    return accounts_web._arm64_dmg_url_from(manifest)


def _hold_stable_download(manifest: str) -> None:
    """Seed what the route reads, so it stays off the network."""
    hold_stable_download_link(_parse_manifest(manifest))


def test_the_download_link_is_the_dmg_stable_serves() -> None:
    assert _parse_manifest(_STABLE_MANIFEST) == _STABLE_ARM64_DMG


def test_a_manifest_naming_no_arm64_dmg_at_all_is_refused() -> None:
    """The link is only correct if the manifest names exactly one arm64 .dmg."""
    with pytest.raises(DownloadLinkError):
        _parse_manifest("version: 0.4.1\nfiles: []\n")


def test_the_route_reads_a_cached_link_rather_than_the_feed() -> None:
    """Otherwise every download click would put the feed in the request path."""
    seeded = "https://download.todesktop.com/x/Seeded-arm64.dmg"
    hold_stable_download_link(seeded)

    assert accounts_web.stable_mac_arm64_url() == seeded
    assert accounts_web.stable_mac_arm64_url() == seeded


def test_download_serves_what_stable_serves_not_todesktops_own_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    _hold_stable_download(_STABLE_MANIFEST)

    resp = client.get("/download?platform=mac", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == _STABLE_ARM64_DMG


def test_download_falls_back_when_stable_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    hold_stable_download_link(None)

    resp = client.get("/download?platform=mac", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == accounts_web._DEFAULT_TARGET_BY_PLATFORM[accounts_web._MAC_ARM64_PLATFORM]


def test_the_same_url_named_twice_is_still_one_answer() -> None:
    """Two entries naming one artifact are not ambiguous."""
    repeated = (
        "version: 0.4.1\n"
        "files:\n"
        "  - url: https://download.todesktop.com/x/Only-arm64.dmg\n"
        "    size: 1\n"
        "  - url: https://download.todesktop.com/x/Only-arm64.dmg\n"
        "    size: 1\n"
    )
    assert _parse_manifest(repeated) == "https://download.todesktop.com/x/Only-arm64.dmg"


def test_two_different_dmgs_are_ambiguous_and_refused() -> None:
    two = (
        "version: 0.4.1\n"
        "files:\n"
        "  - url: https://download.todesktop.com/x/One-arm64.dmg\n"
        "    size: 1\n"
        "  - url: https://download.todesktop.com/x/Two-arm64.dmg\n"
        "    size: 2\n"
    )
    with pytest.raises(DownloadLinkError):
        _parse_manifest(two)


def test_a_dmg_hosted_anywhere_but_todesktop_is_not_a_candidate() -> None:
    """The feed says where to send people, so a compromised one must not be able to."""
    elsewhere = "version: 0.4.1\nfiles:\n  - url: https://evil.example/Minds-arm64.dmg\n    size: 1\n"
    with pytest.raises(DownloadLinkError):
        _parse_manifest(elsewhere)


def test_a_bare_filename_is_not_a_candidate() -> None:
    """electron-builder writes these; relative would resolve against the connector's own host."""
    relative = "version: 0.4.1\nfiles:\n  - url: Minds-0.4.1-arm64.dmg\n    size: 1\n"
    with pytest.raises(DownloadLinkError):
        _parse_manifest(relative)


def test_the_download_fallback_names_the_build_stable_declares() -> None:
    """Promoting stable bumps this by hand, so nothing else would notice it drifting.

    Ahead of stable is the direction to avoid: ``allowDowngrade`` is false, so an
    install that takes the fallback never comes back down.
    """
    repo_root = Path(__file__).parents[4]
    declared = tomllib.loads((repo_root / "apps/minds/release-channels.toml").read_text())["channels"]["stable"]
    fallback = unquote(accounts_web._DEFAULT_TARGET_BY_PLATFORM[accounts_web._MAC_ARM64_PLATFORM])

    assert f"Minds {declared['version']} - Build {declared['build_id']}" in fallback, (
        "the connector's download fallback no longer names the build stable serves -- see the "
        "Release channels section of apps/minds/docs/deploy/release.md"
    )


def test_the_download_fallback_names_the_todesktop_app_builds_are_served_from() -> None:
    """The app id is the segment of the hand-typed url the tests above both skip.

    The prefix check stops at the host and the drift test reads only the name, so
    a typo here would otherwise ship and surface as a 404 during the outage the
    fallback exists to cover.
    """
    repo_root = Path(__file__).parents[4]
    todesktop_config = (repo_root / "apps/minds/todesktop.js").read_text()
    declared_app_id = re.search(r"^\s*id: '([^']+)',$", todesktop_config, re.MULTILINE)
    assert declared_app_id is not None, "apps/minds/todesktop.js no longer declares `id` as a quoted literal"

    fallback = accounts_web._DEFAULT_TARGET_BY_PLATFORM[accounts_web._MAC_ARM64_PLATFORM]

    assert fallback.startswith(f"{accounts_web._TODESKTOP_DOWNLOAD_PREFIX}{declared_app_id.group(1)}/"), (
        "the connector's download fallback names a different ToDesktop app than minds is built as"
    )


def test_the_download_fallback_would_pass_the_rules_the_feed_is_held_to() -> None:
    """The route serves this url beside the ones it resolves, so it has to look like one.

    The drift test above reads only the version and build id out of the name, so
    a mistyped host or suffix passes it.
    """
    fallback = accounts_web._DEFAULT_TARGET_BY_PLATFORM[accounts_web._MAC_ARM64_PLATFORM]

    assert fallback.startswith(accounts_web._TODESKTOP_DOWNLOAD_PREFIX)
    assert fallback.endswith(accounts_web._ARM64_DMG_SUFFIX)


def test_a_manifest_nested_deep_enough_to_exhaust_the_stack_is_refused() -> None:
    """A RecursionError is not a YAMLError, so it would otherwise reach the route as a 500.

    Four kilobytes of brackets, so a size cap would not catch this one.
    """
    nested = "a: " + "[" * 2000 + "]" * 2000 + "\n"

    with pytest.raises(DownloadLinkError):
        _parse_manifest(nested)


@pytest.mark.parametrize(
    "unconvertible",
    [
        pytest.param("releaseDate: 2026-13-45T23:46:47.920Z\n", id="month out of range"),
        pytest.param("releaseDate: 2026-02-30\n", id="day out of range for month"),
        pytest.param("size: " + "9" * 5000 + "\n", id="int past the digit cap"),
        pytest.param("releaseDate: !!timestamp nonsense\n", id="unparsable timestamp"),
        pytest.param("size: !!int notanumber\n", id="unparsable int"),
        pytest.param("draft: !!bool notabool\n", id="unparsable bool"),
    ],
)
def test_a_scalar_that_resolves_but_will_not_convert_is_refused(unconvertible: str) -> None:
    """The constructors raise ValueError/AttributeError/KeyError, none of them a parse error.

    Each would otherwise reach the route as a 500 rather than the fallback -- and
    cache nothing, so every download would re-fetch and re-raise.
    """
    with pytest.raises(DownloadLinkError):
        _parse_manifest(_STABLE_MANIFEST + unconvertible)


def test_an_arm64_dmg_under_another_key_is_not_an_artifact() -> None:
    """Only `url:` names an artifact.

    Scanning the document instead would see two urls here, call the manifest
    ambiguous, and refuse a perfectly good one -- and would take whatever a
    future key happened to hold.
    """
    decoy = _STABLE_MANIFEST + "path: https://download.todesktop.com/x/Something-Else-arm64.dmg\n"

    assert _parse_manifest(decoy) == "https://download.todesktop.com/x/Minds%200.4.1%20-%20Build%20b1-arm64.dmg"


def test_browser_signin_refused_for_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    signup = st_backend.sign_up(tenant_id="public", email="banned@example.com", password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    st_backend.suspended_user_ids.add(signup.user.id)

    resp = client.post("/accounts/api/signin", json={"email": "banned@example.com", "password": "pw-123456"})

    body = resp.json()
    assert body["status"] == "ACCOUNT_SUSPENDED"
    assert "support@imbue.com" in body["message"]
    # No session was minted for the refused sign-in.
    assert st_backend.last_browser_session is None


def test_device_token_exchange_refused_for_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code authorized before the suspension must not be exchangeable after it."""
    client, st_backend, _codes = _make_accounts_web_test_client(monkeypatch)
    user_id = _sign_in_browser(client, st_backend)
    verifier = secrets.token_urlsafe(32)
    query = _authorize_query(verifier=verifier)
    query["confirmed"] = "1"
    authorize = client.get(f"/accounts/authorize?{urlencode(query)}", follow_redirects=False)
    code = parse_qs(urlsplit(authorize.headers["location"]).query)["code"][0]
    st_backend.suspended_user_ids.add(user_id)

    exchange = client.post(
        "/auth/device/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:8123/callback"},
    )

    assert exchange.status_code == 403
    assert exchange.json()["detail"]["code"] == "account_suspended"


def test_oauth_callback_refuses_a_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    client, st_backend = _make_oauth_client(monkeypatch)
    # First OAuth login creates the account.
    first_state = _start_oauth(client, "/")
    client.get(f"/share/oauth/google/callback?code=code-1&state={first_state}", follow_redirects=False)
    account = st_backend.accounts_by_email["visitor@example.com"]
    st_backend.suspended_user_ids.add(account.user_id)
    st_backend.last_browser_session = None

    second_state = _start_oauth(client, "/")
    resp = client.get(f"/share/oauth/google/callback?code=code-2&state={second_state}", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=account_suspended" in resp.headers["location"]
    assert st_backend.last_browser_session is None


def test_bearer_identity_checks_the_core_on_writes_and_not_on_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: resolve_web_user_identity infers check_database from the request method."""
    checked_databases: list[bool] = []
    user_id = "d3d3d3d3-1111-2222-3333-444455556666"

    def _recording_authenticate_request(request: Request, check_database: bool = False) -> UserAuth:
        checked_databases.append(check_database)
        return UserAuth(user_id_prefix=derive_user_id_prefix(user_id), email="d3@example.com", is_email_verified=True)

    client, _backend = _make_share_test_client_with_fakes(
        monkeypatch,
        {
            "get_user_id_from_access_token": lambda token: user_id,
            "authenticate_request": _recording_authenticate_request,
        },
    )
    headers = {"Authorization": "Bearer d3-session-token"}

    client.get("/shares", headers=headers)
    client.post("/shares", json={"host_id": "host-" + "c" * 32}, headers=headers)

    assert checked_databases == [False, True]
