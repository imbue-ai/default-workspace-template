import pytest
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError

import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.auth_proxy as auth_proxy_mod
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend
from imbue.remote_service_connector.web import web_app


def test_auth_signup_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signup without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_signin_returns_503_when_supertokens_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/signin without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 503


def test_auth_session_refresh_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/refresh without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "r"})
    assert resp.status_code == 503


def test_auth_session_revoke_returns_503_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without SUPERTOKENS_CONNECTION_URI returns 503."""
    monkeypatch.delenv("SUPERTOKENS_CONNECTION_URI", raising=False)
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke", headers={"Authorization": "Bearer any-token"})
    assert resp.status_code == 503


def test_auth_session_revoke_requires_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling /auth/session/revoke without a Bearer access token returns 401.

    This guards against an anonymous caller terminating arbitrary users'
    sessions just by knowing (or guessing) their user_id.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app)
    resp = client.post("/auth/session/revoke")
    assert resp.status_code == 401


def test_auth_verify_email_missing_token_shows_failed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify-email endpoint renders an HTML failure page when the token is missing."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email")
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_reset_password_page_renders_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset-password page renders an HTML form embedding the token."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/reset-password", params={"token": "tok-xyz"})
    assert resp.status_code == 200
    assert "tok-xyz" in resp.text
    assert "Reset password" in resp.text


def _install_fake_supertokens(monkeypatch: pytest.MonkeyPatch) -> FakeSuperTokensBackend:
    """Wire the FakeSuperTokensBackend into the app module and return it."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    backend = make_fake_supertokens_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_auth_signup_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup creates an account, issues a session, and sends a verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == 1
    assert "new@example.com" in backend.accounts_by_email


def test_auth_signup_field_error_on_empty_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup returns FIELD_ERROR for empty email or password."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "  ", "password": "x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FIELD_ERROR"


def test_auth_signup_duplicate_email_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing up with an email that already exists returns EMAIL_ALREADY_EXISTS."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    assert len(backend.accounts_by_email) == 1


def _oauth_callback_payload(provider_id: str) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "callback_url": "http://127.0.0.1:9999/cb",
        "query_params": {"code": "abc", "state": "xyz"},
    }


def test_auth_signup_rejected_when_email_has_oauth_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password signup for an email that already has a Google account is refused before touching the recipe."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="both@example.com", third_party_user_id="tp-both")
    client = TestClient(web_app, raise_server_exceptions=False)
    oauth_resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))
    assert oauth_resp.json()["status"] == "OK"
    account_count_before = len(backend.accounts_by_id)

    resp = client.post("/auth/signup", json={"email": "both@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCOUNT_EXISTS_WITH_OTHER_METHOD"
    assert "Google" in body["message"]
    assert body["tokens"] is None
    # No second account was created and no verification email went out.
    assert len(backend.accounts_by_id) == account_count_before
    assert backend.sent_verification_emails == []


def test_auth_signup_rejected_case_insensitively_when_email_has_oauth_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-method guard matches emails case-insensitively (and ignores surrounding whitespace)."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="case@example.com", third_party_user_id="tp-case")
    client = TestClient(web_app, raise_server_exceptions=False)
    oauth_resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))
    assert oauth_resp.json()["status"] == "OK"
    account_count_before = len(backend.accounts_by_id)

    resp = client.post("/auth/signup", json={"email": "  Case@Example.COM  ", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCOUNT_EXISTS_WITH_OTHER_METHOD"
    assert "Google" in body["message"]
    assert body["tokens"] is None
    # No second account was created and no verification email went out.
    assert len(backend.accounts_by_id) == account_count_before
    assert backend.sent_verification_emails == []


def test_auth_oauth_callback_rejected_when_email_has_password_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OAuth callback for an email that already has a password account is refused before creating a user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup_resp = client.post("/auth/signup", json={"email": "both@example.com", "password": "password123"})
    assert signup_resp.json()["status"] == "OK"
    backend.register_provider("google", email="both@example.com", third_party_user_id="tp-both")
    account_count_before = len(backend.accounts_by_id)

    resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCOUNT_EXISTS_WITH_OTHER_METHOD"
    assert "password" in body["message"]
    assert body["tokens"] is None
    # The existing password account is untouched and no thirdparty user appeared.
    assert len(backend.accounts_by_id) == account_count_before
    assert backend.accounts_by_email["both@example.com"].provider_id == "emailpassword"


def test_auth_oauth_callback_allows_returning_oauth_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeat OAuth sign-in for an email whose account uses that same provider still succeeds."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="repeat@example.com", third_party_user_id="tp-repeat")
    client = TestClient(web_app, raise_server_exceptions=False)
    first = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))
    assert first.json()["status"] == "OK"

    second = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "repeat@example.com"
    assert len(backend.accounts_by_id) == 1


def test_preexisting_cross_method_duplicate_keeps_both_sign_ins_working(monkeypatch: pytest.MonkeyPatch) -> None:
    """An email with pre-guard duplicate accounts (password + OAuth) can still sign in with both methods."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup_resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert signup_resp.json()["status"] == "OK"
    # Seed the duplicate directly: the guard refuses to create one through the routes.
    google_user_id = backend.add_third_party_account(
        provider_id="google", email="dup@example.com", third_party_user_id="tp-dup"
    )
    backend.register_provider("google", email="dup@example.com", third_party_user_id="tp-dup")

    oauth_resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))
    signin_resp = client.post("/auth/signin", json={"email": "dup@example.com", "password": "password123"})
    resignup_resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})

    # The OAuth sign-in resolves to the google account, not the password one.
    oauth_body = oauth_resp.json()
    assert oauth_body["status"] == "OK"
    assert oauth_body["user"]["user_id"] == google_user_id
    # The password sign-in keeps working too.
    assert signin_resp.json()["status"] == "OK"
    # A password re-signup gets the recipe's own answer, not the cross-method status.
    assert resignup_resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    # No third account appeared along the way.
    assert len(backend.accounts_by_id) == 2


def test_auth_oauth_callback_returns_error_when_account_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens outage during the one-account-per-email lookup surfaces as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="down@example.com", third_party_user_id="tp-down")
    backend.raise_on("list_users_by_account_info", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Auth backend unavailable"
    assert body["tokens"] is None
    # The refused callback wrote nothing to the backend.
    assert len(backend.accounts_by_id) == 0


def test_auth_signup_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signup is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_up", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ERROR",
        "message": "Auth backend unavailable",
        "user": None,
        "tokens": None,
        "needs_email_verification": False,
    }


def test_auth_signup_returns_error_when_account_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens outage during signup's one-account-per-email lookup surfaces as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("list_users_by_account_info", SuperTokensGeneralError("core down"))
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Auth backend unavailable"
    # The refused signup created no account.
    assert len(backend.accounts_by_id) == 0


def _install_paid_pool_backend(monkeypatch: pytest.MonkeyPatch, *paid_emails: str) -> FakePoolBackend:
    """Install a fake pool backend (so ``is_email_paid`` works) seeding the given paid emails."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    pool_backend = make_fake_pool_backend()
    for paid_email in paid_emails:
        pool_backend.add_paid_email(paid_email)
    pool_backend.install_on_app_module(app_mod, monkeypatch)
    return pool_backend


def test_auth_signup_paid_email_is_auto_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paid user's email/password signup is auto-verified: no email sent, account already verified."""
    st_backend = _install_fake_supertokens(monkeypatch)
    _install_paid_pool_backend(monkeypatch, "paid@example.com")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "paid@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    # The paid account skips the verification round trip entirely.
    assert body["needs_email_verification"] is False
    assert st_backend.sent_verification_emails == []
    assert st_backend.accounts_by_email["paid@example.com"].is_verified is True


def test_auth_signup_unpaid_email_still_requires_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-paid signup keeps the verify-by-email flow (control for the paid case)."""
    st_backend = _install_fake_supertokens(monkeypatch)
    _install_paid_pool_backend(monkeypatch, "someone-else@example.com")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "free@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_email_verification"] is True
    assert len(st_backend.sent_verification_emails) == 1
    assert st_backend.accounts_by_email["free@example.com"].is_verified is False


def test_auth_signin_happy_path_with_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin against a verified account returns OK and skips resending verification."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "a@b.com", "password": "password123"})
    initial_verify_count = len(backend.sent_verification_emails)
    account = backend.accounts_by_email["a@b.com"]
    backend.mark_email_verified(account.user_id)
    resp = client.post("/auth/signin", json={"email": "a@b.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is False
    assert len(backend.sent_verification_emails) == initial_verify_count


def test_auth_signin_wrong_password_returns_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signin with an incorrect password returns WRONG_CREDENTIALS without issuing a session."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "x@y.com", "password": "password123"})
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "WRONG_CREDENTIALS"
    assert body["tokens"] is None


def test_auth_signin_unverified_email_triggers_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in to an unverified account sends another verification email (once the cooldown allows)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv@example.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    # Signup just sent a verification email; age the cooldown out so the
    # signin resend is not suppressed (simulates a later re-signin).
    auth_proxy_mod._verification_email_sent_at_monotonic_by_user_id.clear()
    resp = client.post("/auth/signin", json={"email": "unv@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_signin_resend_is_suppressed_within_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unverified signin right after signup does not send a second email (per-user cooldown)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv2@example.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/signin", json={"email": "unv2@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["needs_email_verification"] is True
    assert len(backend.sent_verification_emails) == before


def test_auth_signin_returns_error_on_sdk_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens SDK exception in signin is surfaced as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.raise_on("sign_in", SuperTokensSessionError("session store down"))
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signin", json={"email": "x@y.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_refresh_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh rotates tokens and invalidates the old refresh token."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "r@e.com", "password": "password123"}).json()
    initial_refresh = signup["tokens"]["refresh_token"]
    resp = client.post("/auth/session/refresh", json={"refresh_token": initial_refresh})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["tokens"]["refresh_token"] != initial_refresh
    assert initial_refresh not in backend.sessions_by_refresh_token


def test_auth_session_refresh_rejects_unknown_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/refresh returns status=ERROR for an unknown refresh token."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/session/refresh", json={"refresh_token": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_session_revoke_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/revoke tears down every session for the authenticated user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "rev@e.com", "password": "password123"}).json()
    access = signup["tokens"]["access_token"]
    assert len(backend.sessions_by_access_token) == 1
    resp = client.post(
        "/auth/session/revoke",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert resp.json()["revoked_count"] == 1
    assert len(backend.sessions_by_access_token) == 0


def test_auth_send_verification_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/send-verification resends the caller's verification email."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "v@e.com", "password": "password123"}).json()
    access_token = signup["tokens"]["access_token"]
    before = len(backend.sent_verification_emails)
    # Age out the cooldown started by the signup's own verification send.
    auth_proxy_mod._verification_email_sent_at_monotonic_by_user_id.clear()
    resp = client.post(
        "/auth/email/send-verification",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": "v@e.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK", "sent": True}
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_send_verification_email_suppressed_within_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resend right after signup reports sent=False and delivers nothing (per-user cooldown)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "vc@e.com", "password": "password123"}).json()
    access_token = signup["tokens"]["access_token"]
    before = len(backend.sent_verification_emails)
    resp = client.post(
        "/auth/email/send-verification",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": "vc@e.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK", "sent": False}
    assert len(backend.sent_verification_emails) == before


def test_auth_send_verification_email_failed_send_does_not_consume_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send that blows up must not start the cooldown: the immediate retry still delivers."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "flaky@e.com", "password": "password123"}).json()
    access_token = signup["tokens"]["access_token"]
    # Age out the cooldown from the signup's own verification send, then make
    # the next send fail.
    auth_proxy_mod._verification_email_sent_at_monotonic_by_user_id.clear()
    backend.raise_on("send_email_verification_email", SuperTokensGeneralError("email service down"))
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    failed = client.post("/auth/email/send-verification", headers=auth_headers, json={"email": "flaky@e.com"})
    assert failed.status_code >= 500
    # The failure is fixed; a retry within the cooldown window must actually
    # send instead of being suppressed by the failed attempt's reservation.
    del backend.sdk_errors_by_method["send_email_verification_email"]
    before = len(backend.sent_verification_emails)
    retried = client.post("/auth/email/send-verification", headers=auth_headers, json={"email": "flaky@e.com"})
    assert retried.status_code == 200
    assert retried.json() == {"status": "OK", "sent": True}
    assert len(backend.sent_verification_emails) == before + 1


def test_auth_send_verification_email_requires_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without credentials the endpoint is a 401, not an email-sending oracle."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "anon@e.com", "password": "password123"})
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/email/send-verification", json={"email": "anon@e.com"})
    assert resp.status_code == 401
    assert len(backend.sent_verification_emails) == before


def test_auth_send_verification_email_rejects_foreign_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid session cannot trigger verification emails for someone else's address."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "victim@e.com", "password": "password123"})
    attacker = client.post("/auth/signup", json={"email": "attacker@e.com", "password": "password123"}).json()
    before = len(backend.sent_verification_emails)
    resp = client.post(
        "/auth/email/send-verification",
        headers={"Authorization": f"Bearer {attacker['tokens']['access_token']}"},
        json={"email": "victim@e.com"},
    )
    assert resp.status_code == 403
    assert len(backend.sent_verification_emails) == before


def test_auth_is_email_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/email/is-verified reflects the caller's account state."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "iv@e.com", "password": "password123"}).json()
    user_id = signup["user"]["user_id"]
    auth_headers = {"Authorization": f"Bearer {signup['tokens']['access_token']}"}
    resp = client.post("/auth/email/is-verified", headers=auth_headers, json={"email": "iv@e.com"})
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}
    backend.mark_email_verified(user_id)
    resp = client.post("/auth/email/is-verified", headers=auth_headers, json={"email": "iv@e.com"})
    assert resp.json() == {"verified": True}


def test_auth_is_email_verified_requires_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without credentials the endpoint is a 401, not a verification-status oracle."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/email/is-verified", json={"email": "a@b.com"})
    assert resp.status_code == 401


def test_auth_verify_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verify-email page consumes a valid token and marks the account verified."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "ve@e.com", "password": "password123"})
    token = next(iter(backend.verification_tokens.keys()))
    resp = client.get("/auth/verify-email", params={"token": token})
    assert resp.status_code == 200
    assert "Email verified" in resp.text
    user_id = backend.accounts_by_email["ve@e.com"].user_id
    assert backend.accounts_by_id[user_id].is_verified is True


def test_auth_verify_email_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting an invalid verification token renders the failure page."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/verify-email", params={"token": "bogus"})
    assert resp.status_code == 400
    assert "Verification failed" in resp.text


def test_auth_forgot_password_sends_reset_email_for_known_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/forgot enqueues a reset email when the account exists."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "fp@e.com", "password": "password123"})
    resp = client.post("/auth/password/forgot", json={"email": "fp@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert len(backend.sent_reset_emails) == 1


def test_auth_forgot_password_unknown_email_still_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """For unknown emails the endpoint returns the same success shape (anti-enumeration)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/forgot", json={"email": "nobody@e.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.sent_reset_emails == []


def test_auth_reset_password_consumes_token_and_updates_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid reset token updates the account password; it cannot be reused."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "rp@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["rp@e.com"].user_id
    token = backend.issue_reset_token(user_id)
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "newpass456"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    assert backend.accounts_by_id[user_id].password == "newpass456"
    resp = client.post("/auth/password/reset", json={"token": token, "new_password": "again789"})
    assert resp.json()["status"] == "INVALID_TOKEN"


def test_auth_reset_password_rejects_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/password/reset returns 400 when the token or password is missing."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/password/reset", json={"token": "", "new_password": ""})
    assert resp.status_code == 400


def test_auth_oauth_authorize_returns_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize asks the provider for a redirect URL."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oa@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["url"].startswith("https://google.example.com/auth")


def test_auth_oauth_authorize_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/authorize returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "unknown", "callback_url": "http://127.0.0.1/cb"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_oauth_callback_creates_user_and_returns_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback links the provider user, creates an account, and returns tokens."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider(
        "google",
        email="cb@e.com",
        third_party_user_id="tp-1",
        display_name="Callback User",
    )
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/oauth/callback", json=_oauth_callback_payload("google"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "cb@e.com"
    assert body["user"]["display_name"] == "Callback User"
    assert body["tokens"]["access_token"].startswith("at-")
    assert "cb@e.com" in backend.accounts_by_email


def test_auth_oauth_callback_unknown_provider_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/oauth/callback returns status=ERROR for a provider that isn't registered."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "missing",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ERROR"


def test_auth_get_user_returns_provider_email_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports 'email' for password-registered accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "gu@e.com", "password": "password123"})
    user_id = backend.accounts_by_email["gu@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "email"
    assert body["email"] == "gu@e.com"


def test_auth_get_user_reports_third_party_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} reports the OAuth provider ID for OAuth accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="oauth-user@e.com")
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post(
        "/auth/oauth/callback",
        json={
            "provider_id": "google",
            "callback_url": "http://127.0.0.1/cb",
            "query_params": {"code": "a"},
        },
    )
    user_id = backend.accounts_by_email["oauth-user@e.com"].user_id
    resp = client.get(f"/auth/users/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "google"


def test_auth_get_user_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/users/{user_id} returns 404 when the user does not exist."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.get("/auth/users/does-not-exist")
    assert resp.status_code == 404


def test_build_oauth_providers_includes_only_fully_configured_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    # GitHub has an id but no secret, so it must be left out rather than
    # registered half-configured.
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-id")
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)

    providers = auth_proxy_mod._build_oauth_providers()

    assert [provider.config.third_party_id for provider in providers] == ["google"]
    assert providers[0].config.clients is not None
    assert providers[0].config.clients[0].client_id == "google-id"


def test_build_oauth_providers_builds_github_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-secret")

    providers = auth_proxy_mod._build_oauth_providers()

    assert [provider.config.third_party_id for provider in providers] == ["github"]
