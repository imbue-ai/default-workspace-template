from typing import Any

import pytest
from starlette.testclient import TestClient
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.emailpassword.api.implementation import (
    APIImplementation as EmailPasswordAPIImplementation,
)
from supertokens_python.recipe.emailverification.recipe import APIImplementation as EmailVerificationAPIImplementation
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.thirdparty.api.implementation import APIImplementation as ThirdPartyAPIImplementation

import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.auth_proxy as auth_proxy_mod
from imbue.remote_service_connector.auth_proxy import EnsureAsgiRootPathMiddleware
from imbue.remote_service_connector.auth_proxy import PartitionedCookieMiddleware
from imbue.remote_service_connector.auth_proxy import _add_partitioned_to_cross_site_cookies
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


def _install_fake_supertokens(monkeypatch: pytest.MonkeyPatch) -> FakeSuperTokensBackend:
    """Wire the FakeSuperTokensBackend into the app module and return it."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://st.example.com")
    backend = make_fake_supertokens_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_auth_signup_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup creates an account and issues a session; verification is non-blocking.

    No verification email is sent at signup (the first verification-gated
    action triggers a contextual send), and ``needs_email_verification`` is
    pinned False for wire compat with released clients.
    """
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "new@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["user"]["email"] == "new@example.com"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is False
    # is_new_account is deliberately kept OFF the wire (the pre-tolerant fleet's
    # strict models reject unknown response fields); it lives on the in-process
    # AuthResponse object only. See the CLEANUP note on the route decorator.
    assert "is_new_account" not in body
    assert backend.sent_verification_emails == []
    assert "new@example.com" in backend.accounts_by_email
    assert backend.accounts_by_email["new@example.com"].is_verified is False


def test_auth_signup_field_error_on_empty_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup returns FIELD_ERROR for empty email or password."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "  ", "password": "x"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FIELD_ERROR"


def test_auth_signup_field_error_on_weak_password_or_malformed_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/signup enforces the SDK's default form validation server-side.

    The recipe function bypasses the SDK API layer's form-field validators
    (which this app disables anyway), so the endpoint applies the same
    defaults itself rather than trusting frontend validation.
    """
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)

    weak = client.post("/auth/signup", json={"email": "weak@example.com", "password": "short"})
    assert weak.status_code == 200
    assert weak.json()["status"] == "FIELD_ERROR"

    malformed = client.post("/auth/signup", json={"email": "not-an-email", "password": "password123"})
    assert malformed.status_code == 200
    assert malformed.json()["status"] == "FIELD_ERROR"

    assert backend.accounts_by_email == {}


def test_auth_signup_is_disabled_on_production_and_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the restricted tiers the JSON signup refuses to create accounts (browser flow only)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    for tier in ("production", "staging"):
        monkeypatch.setenv("MNGR_DEPLOY_ENV", tier)
        resp = client.post("/auth/signup", json={"email": "blocked@example.com", "password": "password123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SIGNUP_DISABLED"
        assert body["tokens"] is None
        assert "mngr imbue_cloud auth login" in body["message"]
    assert backend.accounts_by_email == {}


def test_auth_signup_fails_closed_when_the_deploy_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset MNGR_DEPLOY_ENV means a bare production deploy, so signup is refused."""
    backend = _install_fake_supertokens(monkeypatch)
    monkeypatch.delenv("MNGR_DEPLOY_ENV", raising=False)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "blocked@example.com", "password": "password123"})
    assert resp.json()["status"] == "SIGNUP_DISABLED"
    assert backend.accounts_by_email == {}


def test_auth_signup_stays_available_on_ci_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI (and dev) env names keep the headless signup so tests can mint accounts."""
    backend = _install_fake_supertokens(monkeypatch)
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "ci-orchestrator-3")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/auth/signup", json={"email": "ci@example.com", "password": "password123"})
    assert resp.json()["status"] == "OK"
    assert "ci@example.com" in backend.accounts_by_email


def test_auth_signin_still_works_on_restricted_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling signup must not touch sign-in: existing accounts keep working on production."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "existing@example.com", "password": "password123"})
    assert signup.json()["status"] == "OK"
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")

    resp = client.post("/auth/signin", json={"email": "existing@example.com", "password": "password123"})

    assert resp.json()["status"] == "OK"
    assert "existing@example.com" in backend.accounts_by_email


def test_admin_test_signup_still_works_on_restricted_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin-key test-signup path is unaffected by the tier restriction (deployment tests rely on it)."""
    backend = _install_fake_supertokens(monkeypatch)
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")
    monkeypatch.setenv("MINDS_ADMIN_KEY", "admin-key-7712")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post(
        "/admin/test-signup",
        headers={"Authorization": "Bearer admin-key-7712"},
        json={"email": "tester@example.com", "password": "password123", "verified": True},
    )

    assert resp.json()["status"] == "OK"
    assert backend.accounts_by_email["tester@example.com"].is_verified is True


def test_auth_signup_duplicate_email_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing up with an email that already exists returns EMAIL_ALREADY_EXISTS."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    assert len(backend.accounts_by_email) == 1


def _exchange_google_code(backend: FakeSuperTokensBackend) -> Any:
    """Run the shared OAuth code exchange directly, as the browser callback does.

    The old JSON ``/auth/oauth/*`` routes are gone, so the exchange (and the
    one-account-per-email guard inside it) is exercised as a function against
    the fake backend's registered Google provider. The fake provider is not an
    SDK ``Provider`` subclass (it only implements the async surface the
    exchange calls), hence the Any-typed handoff.
    """
    fake_google_provider: Any = backend.registered_providers["google"]
    return auth_proxy_mod.complete_oauth_code_exchange(
        provider=fake_google_provider,
        provider_id="google",
        callback_url="http://127.0.0.1:9999/cb",
        query_params={"code": "abc", "state": "xyz"},
    )


def test_auth_signup_rejected_when_email_has_oauth_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password signup for an email that already has a Google account is refused before touching the recipe."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.add_third_party_account(provider_id="google", email="both@example.com", third_party_user_id="tp-both")
    client = TestClient(web_app, raise_server_exceptions=False)
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
    backend.add_third_party_account(provider_id="google", email="case@example.com", third_party_user_id="tp-case")
    client = TestClient(web_app, raise_server_exceptions=False)
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


def test_oauth_code_exchange_rejected_when_email_has_password_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OAuth exchange for an email that already has a password account is refused before creating a user."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup_resp = client.post("/auth/signup", json={"email": "both@example.com", "password": "password123"})
    assert signup_resp.json()["status"] == "OK"
    backend.register_provider("google", email="both@example.com", third_party_user_id="tp-both")
    account_count_before = len(backend.accounts_by_id)

    result = _exchange_google_code(backend)

    assert result.status == "ACCOUNT_EXISTS_WITH_OTHER_METHOD"
    assert result.message is not None and "password" in result.message
    assert result.tokens is None
    # The existing password account is untouched and no thirdparty user appeared.
    assert len(backend.accounts_by_id) == account_count_before
    assert backend.accounts_by_email["both@example.com"].provider_id == "emailpassword"


def test_oauth_code_exchange_allows_returning_oauth_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeat OAuth sign-in for an email whose account uses that same provider still succeeds."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="repeat@example.com", third_party_user_id="tp-repeat")
    first = _exchange_google_code(backend)
    assert first.status == "OK"

    second = _exchange_google_code(backend)

    assert second.status == "OK"
    assert second.user is not None and second.user.email == "repeat@example.com"
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

    oauth_result = _exchange_google_code(backend)
    signin_resp = client.post("/auth/signin", json={"email": "dup@example.com", "password": "password123"})
    resignup_resp = client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123"})

    # The OAuth sign-in resolves to the google account, not the password one.
    assert oauth_result.status == "OK"
    assert oauth_result.user is not None and oauth_result.user.user_id == google_user_id
    # The password sign-in keeps working too.
    assert signin_resp.json()["status"] == "OK"
    # A password re-signup gets the recipe's own answer, not the cross-method status.
    assert resignup_resp.json()["status"] == "EMAIL_ALREADY_EXISTS"
    # No third account appeared along the way.
    assert len(backend.accounts_by_id) == 2


def test_oauth_code_exchange_returns_error_when_account_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens outage during the one-account-per-email lookup surfaces as AuthResponse(status='ERROR')."""
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider("google", email="down@example.com", third_party_user_id="tp-down")
    backend.raise_on("list_users_by_account_info", SuperTokensGeneralError("core down"))

    result = _exchange_google_code(backend)

    assert result.status == "ERROR"
    assert result.message == "Auth backend unavailable"
    assert result.tokens is None
    # The refused exchange wrote nothing to the backend.
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


def test_auth_signup_paid_email_is_not_auto_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paid-listed email's signup is NOT auto-verified: nothing may mark an email verified without proof.

    (The old behavior auto-verified paid emails so the then-global verified
    gate would not lock them out; with verification non-blocking, that
    shortcut is an impersonation hole and is gone.)
    """
    st_backend = _install_fake_supertokens(monkeypatch)
    _install_paid_pool_backend(monkeypatch, "paid@example.com")
    client = TestClient(web_app, raise_server_exceptions=False)

    resp = client.post("/auth/signup", json={"email": "paid@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert st_backend.sent_verification_emails == []
    assert st_backend.accounts_by_email["paid@example.com"].is_verified is False


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


def test_auth_signin_unverified_email_succeeds_without_sending_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signing in to an unverified account succeeds and sends no verification email.

    Verification is non-blocking; the contextual send happens only when the
    user hits a verification-gated action (via /auth/email/send-verification).
    """
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "unv@example.com", "password": "password123"})
    resp = client.post("/auth/signin", json={"email": "unv@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is False
    assert backend.sent_verification_emails == []


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


def test_auth_session_revoke_current_revokes_only_presented_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """/auth/session/revoke-current tears down only the presented session, leaving other devices alone."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    first = client.post("/auth/signup", json={"email": "dev1@e.com", "password": "password123"}).json()
    second = client.post("/auth/signin", json={"email": "dev1@e.com", "password": "password123"}).json()
    first_access = first["tokens"]["access_token"]
    second_access = second["tokens"]["access_token"]
    assert len(backend.sessions_by_access_token) == 2

    resp = client.post(
        "/auth/session/revoke-current",
        headers={"Authorization": f"Bearer {first_access}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "OK", "revoked": True}
    # The other device's session survives; the revoked token is gone.
    assert first_access not in backend.sessions_by_access_token
    assert second_access in backend.sessions_by_access_token


def test_auth_session_revoke_current_rejects_stale_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-revoked (or unknown) token gets a 401, like any other stale token."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/auth/session/revoke-current",
        headers={"Authorization": "Bearer at-nonexistent"},
    )
    assert resp.status_code == 401


def test_admin_test_signup_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """/admin/test-signup rejects callers without the operator admin key."""
    _install_fake_supertokens(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", "test-admin-key-77aa")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/admin/test-signup",
        json={"email": "t@example.com", "password": "password123", "verified": True},
    )
    assert resp.status_code == 401


def test_admin_test_signup_creates_verified_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """With verified=true, the test account is created pre-verified and gets session tokens."""
    backend = _install_fake_supertokens(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", "test-admin-key-77aa")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/admin/test-signup",
        json={"email": "t-verified@example.com", "password": "password123", "verified": True},
        headers={"Authorization": "Bearer test-admin-key-77aa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["tokens"]["access_token"].startswith("at-")
    assert body["needs_email_verification"] is False
    assert backend.accounts_by_email["t-verified@example.com"].is_verified is True


def test_admin_test_signup_creates_unverified_account_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the verified flag, the test account is created unverified (the default signup shape)."""
    backend = _install_fake_supertokens(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", "test-admin-key-77aa")
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post(
        "/admin/test-signup",
        json={"email": "t-unverified@example.com", "password": "password123"},
        headers={"Authorization": "Bearer test-admin-key-77aa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["needs_email_verification"] is True
    assert backend.accounts_by_email["t-unverified@example.com"].is_verified is False


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
    """A second send right after the first reports sent=False and delivers nothing (per-user cooldown)."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "vc@e.com", "password": "password123"}).json()
    access_token = signup["tokens"]["access_token"]
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    first = client.post("/auth/email/send-verification", headers=auth_headers, json={"email": "vc@e.com"})
    assert first.json() == {"status": "OK", "sent": True}
    before = len(backend.sent_verification_emails)
    resp = client.post("/auth/email/send-verification", headers=auth_headers, json={"email": "vc@e.com"})
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
    """The verify-email consume API accepts a valid token and marks the account verified."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "ve@e.com", "password": "password123"}).json()
    # Signup no longer sends the verification email; the contextual send is
    # what mints the token the emailed link carries.
    client.post(
        "/auth/email/send-verification",
        headers={"Authorization": f"Bearer {signup['tokens']['access_token']}"},
        json={"email": "ve@e.com"},
    )
    token = next(iter(backend.verification_tokens.keys()))
    resp = client.post("/accounts/api/verify-email", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"
    user_id = backend.accounts_by_email["ve@e.com"].user_id
    assert backend.accounts_by_id[user_id].is_verified is True


def test_auth_verify_email_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting an invalid (or missing) verification token reports INVALID_TOKEN."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    resp = client.post("/accounts/api/verify-email", json={"token": "bogus"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "INVALID_TOKEN"
    missing = client.post("/accounts/api/verify-email", json={"token": ""})
    assert missing.status_code == 200
    assert missing.json()["status"] == "INVALID_TOKEN"


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


def test_legacy_json_oauth_routes_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deprecated JSON OAuth pair is removed: Google sign-in goes through the browser flow only."""
    _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    authorize = client.post(
        "/auth/oauth/authorize",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb"},
    )
    callback = client.post(
        "/auth/oauth/callback",
        json={"provider_id": "google", "callback_url": "http://127.0.0.1:9999/cb", "query_params": {}},
    )
    assert authorize.status_code == 404
    assert callback.status_code == 404


def test_oauth_code_exchange_creates_user_without_bearer_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exchange links the provider user and creates the account; no bearer session is minted.

    The browser callback creates its own cookie session on the response, so a
    bearer session minted here would be orphaned in the core.
    """
    backend = _install_fake_supertokens(monkeypatch)
    backend.register_provider(
        "google",
        email="cb@e.com",
        third_party_user_id="tp-1",
        display_name="Callback User",
    )

    result = _exchange_google_code(backend)

    assert result.status == "OK"
    assert result.user is not None
    assert result.user.email == "cb@e.com"
    assert result.user.display_name == "Callback User"
    assert result.tokens is None
    assert "cb@e.com" in backend.accounts_by_email
    assert backend.sessions_by_access_token == {}


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
    backend.add_third_party_account(provider_id="google", email="oauth-user@e.com", third_party_user_id="tp-gu")
    client = TestClient(web_app, raise_server_exceptions=False)
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


def test_sdk_middleware_default_recipe_apis_are_all_disabled() -> None:
    """The SDK middleware must only serve session routes: every other recipe API is disabled.

    The recipe's own signup/signin/reset/verify routes would bypass the
    hand-rolled endpoints' Turnstile gate, cross-method signup rejection, and
    verification-email cooldown. Iterating over the concrete implementations'
    ``disable_*`` flags means an SDK upgrade that adds a new API makes this
    test fail, forcing an explicit decision about the new route.
    """
    apis = (
        auth_proxy_mod.disable_emailpassword_default_apis(EmailPasswordAPIImplementation()),
        auth_proxy_mod.disable_thirdparty_default_apis(ThirdPartyAPIImplementation()),
        auth_proxy_mod.disable_emailverification_default_apis(EmailVerificationAPIImplementation()),
    )
    for api in apis:
        disable_flags = {name: value for name, value in vars(api).items() if name.startswith("disable_")}
        assert disable_flags, "expected the SDK APIInterface to expose disable_* flags"
        assert all(disable_flags.values()), disable_flags


def test_append_next_to_email_verify_link_handles_both_query_shapes() -> None:
    appended = auth_proxy_mod.append_next_to_email_verify_link(
        "https://accounts.example/auth/verify-email?token=t1&tenantId=public",
        "/share/authorize?machine_domain=d&state=s",
    )
    assert appended == (
        "https://accounts.example/auth/verify-email?token=t1&tenantId=public"
        "&next=%2Fshare%2Fauthorize%3Fmachine_domain%3Dd%26state%3Ds"
    )
    bare = auth_proxy_mod.append_next_to_email_verify_link("https://accounts.example/verify", "/x")
    assert bare == "https://accounts.example/verify?next=%2Fx"


def test_continue_path_from_send_user_context_accepts_only_root_relative_paths() -> None:
    key = "verification_email_next_path"
    accepted = auth_proxy_mod.continue_path_from_send_user_context({key: "/share/authorize?a=1"})
    assert accepted == "/share/authorize?a=1"
    # Absent, foreign-host, scheme-relative, and non-string values are all refused.
    assert auth_proxy_mod.continue_path_from_send_user_context({}) is None
    assert auth_proxy_mod.continue_path_from_send_user_context({key: "https://evil.example/x"}) is None
    assert auth_proxy_mod.continue_path_from_send_user_context({key: "//evil.example/x"}) is None
    assert auth_proxy_mod.continue_path_from_send_user_context({key: 7}) is None


def test_ensure_asgi_root_path_middleware_defaults_a_missing_root_path() -> None:
    """Modal's ASGI shim omits root_path; the wrapper must default it to "".

    The coroutines are stepped synchronously (they never suspend on anything
    unresolved), so no event loop is needed.
    """
    recorded_scopes: list[dict[str, Any]] = []

    class _RecordingAsgiApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            recorded_scopes.append(scope)

    middleware = EnsureAsgiRootPathMiddleware(_RecordingAsgiApp())

    with pytest.raises(StopIteration):
        middleware({"type": "http", "path": "/health/liveness"}, None, None).send(None)
    assert recorded_scopes[0]["root_path"] == ""
    assert recorded_scopes[0]["path"] == "/health/liveness"

    # A scope that already carries a root_path passes through untouched.
    with pytest.raises(StopIteration):
        middleware({"type": "http", "root_path": "/api"}, None, None).send(None)
    assert recorded_scopes[1]["root_path"] == "/api"


def test_add_partitioned_only_to_cross_site_secure_cookies() -> None:
    headers = [
        (b"content-type", b"application/json"),
        # A SameSite=None; Secure cookie gets Partitioned appended.
        (b"set-cookie", b"sAccessToken=abc; Path=/; Secure; HttpOnly; SameSite=None"),
        # A Lax cookie is left alone (not meant to cross sites).
        (b"set-cookie", b"other=1; Path=/; Secure; HttpOnly; SameSite=Lax"),
        # An already-partitioned cookie is not double-appended.
        (b"set-cookie", b"sRefreshToken=xyz; Path=/; Secure; SameSite=None; Partitioned"),
    ]

    rewritten = _add_partitioned_to_cross_site_cookies(headers)

    assert rewritten[0] == (b"content-type", b"application/json")
    assert rewritten[1] == (b"set-cookie", b"sAccessToken=abc; Path=/; Secure; HttpOnly; SameSite=None; Partitioned")
    assert rewritten[2] == (b"set-cookie", b"other=1; Path=/; Secure; HttpOnly; SameSite=Lax")
    assert rewritten[3] == (b"set-cookie", b"sRefreshToken=xyz; Path=/; Secure; SameSite=None; Partitioned")
    # Applied exactly once across the whole header set.
    assert sum(v.lower().count(b"partitioned") for _n, v in rewritten) == 2


def test_partitioned_cookie_middleware_rewrites_response_start_headers() -> None:
    captured: list[dict[str, Any]] = []

    async def _capturing_send(message: dict[str, Any]) -> None:
        captured.append(message)

    class _CookieSettingApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"set-cookie", b"sAccessToken=abc; Secure; SameSite=None")],
                }
            )

    middleware = PartitionedCookieMiddleware(_CookieSettingApp())

    with pytest.raises(StopIteration):
        middleware({"type": "http"}, None, _capturing_send).send(None)

    assert captured[0]["headers"] == [(b"set-cookie", b"sAccessToken=abc; Secure; SameSite=None; Partitioned")]


def test_partitioned_cookie_middleware_ignores_non_http_scopes() -> None:
    seen: list[Any] = []

    class _PassthroughApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            seen.append(scope)

    middleware = PartitionedCookieMiddleware(_PassthroughApp())

    with pytest.raises(StopIteration):
        middleware({"type": "lifespan"}, None, None).send(None)
    assert seen[0]["type"] == "lifespan"


def test_auth_signin_refused_for_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A suspended account's JSON sign-in gets ACCOUNT_SUSPENDED and no session."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    client.post("/auth/signup", json={"email": "banned@example.com", "password": "password123"})
    account = backend.accounts_by_email["banned@example.com"]
    session_count_before = len(backend.sessions_by_access_token)
    backend.suspended_user_ids.add(account.user_id)

    resp = client.post("/auth/signin", json={"email": "banned@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCOUNT_SUSPENDED"
    assert "support@imbue.com" in body["message"]
    assert body["tokens"] is None
    assert len(backend.sessions_by_access_token) == session_count_before


def test_auth_session_refresh_refused_for_suspended_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh landing in the suspend race window is refused and its session revoked."""
    backend = _install_fake_supertokens(monkeypatch)
    client = TestClient(web_app, raise_server_exceptions=False)
    signup = client.post("/auth/signup", json={"email": "racer@example.com", "password": "password123"}).json()
    account = backend.accounts_by_email["racer@example.com"]
    backend.suspended_user_ids.add(account.user_id)

    resp = client.post("/auth/session/refresh", json={"refresh_token": signup["tokens"]["refresh_token"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACCOUNT_SUSPENDED"
    assert body["tokens"] is None
    # Nothing usable escaped: the refreshed session was revoked on the spot.
    assert not any(s.user_id == account.user_id for s in backend.sessions_by_access_token.values())
