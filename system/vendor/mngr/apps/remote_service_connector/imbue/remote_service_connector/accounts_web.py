"""The hosted accounts surface: browser sign-in/sign-up pages and the device handoff.

This is the primary way users get an Imbue account and sign clients into it:

- The Minds-branded pages (login, signup, account management) are a small
  built frontend bundle served at ``/login`` / ``/signup`` / ``/manage``,
  with its JSON API under ``/accounts/api/*``.
- Browser sessions are SuperTokens' own cookie-based sessions (the SDK
  middleware is mounted by ``init_supertokens`` with the accounts api base
  path), so rotation, revocation, and refresh come from the SDK rather than a
  hand-rolled cookie.
- The desktop app / CLI sign in via the loopback handoff: the app opens
  ``/login?next=/accounts/authorize?...`` in the system browser, the user
  authenticates (or confirms "Continue as ..."), and ``/accounts/authorize``
  mints a short-lived one-time code bound to a PKCE challenge and redirects
  to the app's ``http://127.0.0.1:<port>/callback``. The app exchanges the
  code at ``POST /auth/device/token`` for its own SuperTokens session.
- The share flow uses the same surface: the accounts broker's
  ``/share/authorize`` resolves the same browser session and bounces
  visitors to the same ``/login`` page (see ``share_broker.py``).

The SuperTokens SDK calls are wrapped in module-level ``_sdk_*`` seams so the
unit tests can drive the handlers against the in-memory fake backend without
a real core (matching the seam pattern used across this package).
"""

import base64
import hashlib
import logging
import os
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import Protocol
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlsplit

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic import Field
from supertokens_python.async_to_sync_wrapper import sync as _supertokens_sync_run
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailpassword.syncio import sign_in as ep_sign_in
from supertokens_python.recipe.emailpassword.syncio import sign_up as ep_sign_up
from supertokens_python.recipe.emailpassword.syncio import update_email_or_password
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.syncio import verify_email_using_token
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.exceptions import TryRefreshTokenError
from supertokens_python.recipe.session.syncio import create_new_session
from supertokens_python.recipe.session.syncio import get_session
from supertokens_python.recipe.session.syncio import revoke_all_sessions_for_user
from supertokens_python.recipe.session.syncio import revoke_session
from supertokens_python.recipe.thirdparty.provider import Provider
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput
from supertokens_python.recipe.thirdparty.providers.config_utils import find_and_create_provider_instance
from supertokens_python.types import RecipeUserId

import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.auth_proxy as auth_proxy_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth_proxy import ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS
from imbue.remote_service_connector.auth_proxy import AUTH_TENANT_ID
from imbue.remote_service_connector.auth_proxy import AuthUser
from imbue.remote_service_connector.auth_proxy import build_session_tokens
from imbue.remote_service_connector.auth_proxy import require_supertokens_configured
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()

# Where the built accounts frontend bundle lives inside the deployed image
# (added as an image directory by app.py; ``minds env deploy`` builds it
# before ``modal deploy``). Overridable for local development and tests.
_FRONTEND_DIST_ENV: Final[str] = "ACCOUNTS_FRONTEND_DIST"
_DEFAULT_FRONTEND_DIST: Final[str] = "/root/accounts_frontend_dist"

# Where the built web-chrome bundle (the hosted minds web client SPA) lives
# inside the deployed image; built from ``frontend_web/`` and attached by
# app.py the same way as the accounts bundle. Served path-routed under
# ``/web`` on this same origin, so it shares the accounts browser session.
_WEB_CHROME_DIST_ENV: Final[str] = "WEB_CHROME_FRONTEND_DIST"
_DEFAULT_WEB_CHROME_DIST: Final[str] = "/root/web_chrome_frontend_dist"

# One-time device authorization codes: short-lived, single-use, PKCE-bound.
_DEVICE_CODE_TTL_SECONDS: Final[int] = 600
_DEVICE_CODE_BYTES: Final[int] = 32

# Loopback-only redirect targets for the device handoff. Anything else is an
# open-redirect / token-exfiltration surface and is refused at both the
# authorize and exchange steps.
_LOOPBACK_REDIRECT_URI_RE: Final[re.Pattern[str]] = re.compile(
    r"^http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}(/[^\s]*)?$"
)

_TURNSTILE_VERIFY_URL: Final[str] = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Absolute lifetime cap for browser sessions, enforced at resolution time.
# The SuperTokens SDK cannot configure the core's refresh-token validity (a
# core-level setting, ~100 days by default), so the connector bounds browser
# sessions itself: the session's creation time is stamped into its access
# token payload (which survives refreshes) and any session older than this is
# revoked on sight. Within the cap, sessions roll via the SDK's refresh.
_BROWSER_SESSION_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60
_BROWSER_SESSION_STARTED_AT_CLAIM: Final[str] = "browser_session_started_at"

# Browser OAuth state JWTs (same RS256 key as the share-handoff JWTs).
_OAUTH_STATE_TTL_SECONDS: Final[int] = 600
_OAUTH_STATE_PURPOSE: Final[str] = "accounts_oauth"
_OAUTH_STATE_ALGORITHM: Final[str] = "RS256"
_OAUTH_NONCE_COOKIE_NAME: Final[str] = "imbue_oauth_nonce"

# The Google callback path. Kept at the pre-merge broker path because it is
# the redirect URI registered on every tier's Web-application OAuth client;
# renaming it would require re-registering every tier.
OAUTH_GOOGLE_CALLBACK_PATH: Final[str] = "/share/oauth/google/callback"


# ---------------------------------------------------------------------------
# SuperTokens seams (patched by tests; see FakeSuperTokensBackend)
# ---------------------------------------------------------------------------


def _new_browser_session_access_token_payload() -> dict[str, Any]:
    """The custom claims stamped into every browser session at creation.

    The started-at stamp is what the ~30-day lifetime cap is measured against;
    SuperTokens carries custom access token payload across refreshes, so the
    stamp keeps its creation-time value for the session's whole life.
    """
    return {_BROWSER_SESSION_STARTED_AT_CLAIM: int(datetime.now(timezone.utc).timestamp())}


def _sdk_create_browser_session(request: Request, user_id: str) -> Any:
    """Create a cookie-based SuperTokens session on this request/response."""
    return create_new_session(
        request,
        tenant_id=AUTH_TENANT_ID,
        recipe_user_id=RecipeUserId(user_id),
        access_token_payload=_new_browser_session_access_token_payload(),
    )


def _sdk_get_browser_session(request: Request, get_session_fn: Callable[..., Any] = get_session) -> Any:
    """Resolve the request's cookie-based SuperTokens session, or None.

    ``check_database=True`` is load-bearing: without it the SDK accepts any
    signature-valid access token statelessly, so a session revoked by sign-out
    (or the ~30-day cap) would keep resolving here -- and keep minting device
    codes and share handoffs -- until the access token expires. The identity
    resolved here gates security-sensitive flows, so the extra core roundtrip
    is worth revocation taking effect immediately. ``get_session_fn`` is
    injectable so the unit test can assert those SDK arguments (the fake
    backend replaces this seam wholesale and cannot model stateless
    verification).
    """
    return get_session_fn(
        request,
        session_required=False,
        anti_csrf_check=False,
        check_database=True,
        override_global_claim_validators=lambda *_args, **_kwargs: [],
    )


def _resolve_browser_identity(request: Request) -> tuple[str, str, bool] | None:
    """Return ``(user_id, email, is_email_verified)`` for the browser session, or None.

    Shared with the share broker's ``/share/authorize`` so the app-login and
    share-visit flows resolve the exact same session.
    """
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        return None
    try:
        session = _sdk_get_browser_session(request)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.debug("Browser session resolution failed: %s", exc)
        return None
    if session is None:
        return None
    if _is_browser_session_past_max_age(session):
        # The session is rejected either way; a core hiccup during the
        # revocation must not turn the caller's request into a 500 (the next
        # resolution attempt re-revokes).
        try:
            revoke_session(session.get_handle())
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.warning("Could not revoke an over-max-age browser session: %s", exc)
        return None
    user_id = session.get_user_id()
    email, is_verified = auth_module.resolve_account_email(user_id)
    if email is None:
        return None
    return user_id, email, is_verified


def _is_browser_session_past_max_age(session: Any) -> bool:
    """Whether the browser session is older than the absolute ~30-day cap.

    A session without a readable started-at stamp (minted before the cap
    existed, or with a tampered payload) is treated as expired, so the cap is
    a hard guarantee rather than a best effort.
    """
    started_at = session.get_access_token_payload().get(_BROWSER_SESSION_STARTED_AT_CLAIM)
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return True
    age_seconds = datetime.now(timezone.utc).timestamp() - float(started_at)
    return age_seconds > _BROWSER_SESSION_MAX_AGE_SECONDS


def get_browser_session_identity(request: Request) -> tuple[str, str, bool] | None:
    """Public wrapper for other modules (the share broker)."""
    return _resolve_browser_identity(request)


def authenticate_web_request(request: Request) -> auth_module.UserAuth:
    """Authenticate a resource request from either a Bearer token or the browser session.

    The hosted web chrome is served same-origin with the connector's API, so
    its calls carry the SuperTokens browser-session cookie rather than an
    ``Authorization`` header. A Bearer header, when present, always wins (the
    desktop / CLI path, unchanged); otherwise the cookie session is resolved
    (including the ~30-day cap) and state-changing methods get the same
    cross-site-Origin refusal the accounts API applies.
    """
    return resolve_web_user_identity(request)[0]


def resolve_web_user_identity(request: Request) -> tuple[auth_module.UserAuth, str]:
    """Return ``(UserAuth, full user_id)`` from a Bearer token or the browser session.

    The full user id is what share coordinates and LiteLLM keys are scoped by
    (a ``UserAuth`` alone only carries the 16-hex prefix).
    """
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        user = auth_module.authenticate_request(request)
        return user, auth_module.get_user_id_from_bearer_header(request)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _reject_cross_site_post(request)
    identity = _resolve_browser_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    user_id, email, is_verified = identity
    user = auth_module.UserAuth(
        user_id_prefix=auth_module.derive_user_id_prefix(user_id),
        email=email,
        is_email_verified=is_verified,
    )
    return user, user_id


# ---------------------------------------------------------------------------
# Frontend bundle serving
# ---------------------------------------------------------------------------


def frontend_dist_dir() -> Path:
    return Path(os.environ.get(_FRONTEND_DIST_ENV) or _DEFAULT_FRONTEND_DIST)


_PLACEHOLDER_PAGE = (
    "<!doctype html><html><head><title>Imbue accounts</title></head><body>"
    "<h1>Accounts UI is not built</h1>"
    "<p>The accounts frontend bundle was not found on this server. Build it with "
    "<code>pnpm -C apps/remote_service_connector/frontend build</code> (normally done "
    "by <code>minds env deploy</code>) or point ACCOUNTS_FRONTEND_DIST at a build.</p>"
    "</body></html>"
)


def _serve_frontend_index() -> HTMLResponse | FileResponse:
    index_path = frontend_dist_dir() / "index.html"
    if not index_path.is_file():
        return HTMLResponse(_PLACEHOLDER_PAGE, status_code=503)
    return FileResponse(index_path, media_type="text/html")


@router.get("/login", response_model=None)
def accounts_login_page() -> HTMLResponse | FileResponse:
    """The hosted sign-in page (also renders the sign-up tab and the continue-as interstitial)."""
    return _serve_frontend_index()


@router.get("/signup", response_model=None)
def accounts_signup_page() -> HTMLResponse | FileResponse:
    """The hosted sign-up page (the same bundle, leading with the sign-up tab)."""
    return _serve_frontend_index()


@router.get("/manage", response_model=None)
def accounts_manage_page() -> HTMLResponse | FileResponse:
    """The signed-in account-management page (identity, verification, password, sessions).

    Deliberately NOT ``/account`` -- that path is the deprecated JSON account
    API released clients still call.
    """
    return _serve_frontend_index()


@router.get("/auth/reset-password", response_model=None)
def accounts_reset_password_page() -> HTMLResponse | FileResponse:
    """The password-reset form linked from reset emails (``?token=...``).

    The path is baked into every previously-sent reset email (built from
    ``AUTH_WEBSITE_DOMAIN``), so the bundle serves it here rather than under a
    new accounts path. The page posts to the JSON ``/auth/password/reset``.
    """
    return _serve_frontend_index()


@router.get("/auth/verify-email", response_model=None)
def accounts_verify_email_page() -> HTMLResponse | FileResponse:
    """The verify-email result page linked from verification emails (``?token=...``).

    Same baked-into-sent-emails constraint as the reset page. The page
    consumes the token via ``POST /accounts/api/verify-email``.
    """
    return _serve_frontend_index()


@router.get("/check-inbox", response_model=None)
def accounts_check_inbox_page() -> HTMLResponse | FileResponse:
    """The share flow's check-your-inbox page (an unverified visitor was just emailed a link)."""
    return _serve_frontend_index()


@router.get("/accounts/assets/{asset_path:path}")
def accounts_asset(asset_path: str) -> FileResponse:
    """Serve one built asset from the bundle (the pages reference /accounts/assets/*).

    Resolved lazily (not a StaticFiles mount) so the dist directory can come
    from the environment at request time; the resolved path must stay inside
    the dist directory (no traversal).
    """
    assets_dir = (frontend_dist_dir() / "assets").resolve()
    candidate = (assets_dir / asset_path).resolve()
    if not candidate.is_relative_to(assets_dir) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(candidate)


def web_chrome_dist_dir() -> Path:
    return Path(os.environ.get(_WEB_CHROME_DIST_ENV) or _DEFAULT_WEB_CHROME_DIST)


_WEB_CHROME_PLACEHOLDER_PAGE = (
    "<!doctype html><html><head><title>minds</title></head><body>"
    "<h1>The minds web client is not built</h1>"
    "<p>The web-chrome bundle was not found on this server. Build it with "
    "<code>pnpm -C apps/remote_service_connector/frontend_web build</code> (normally done "
    "by <code>minds env deploy</code>) or point WEB_CHROME_FRONTEND_DIST at a build.</p>"
    "</body></html>"
)


def _serve_web_chrome_index() -> HTMLResponse | FileResponse:
    index_path = web_chrome_dist_dir() / "index.html"
    if not index_path.is_file():
        return HTMLResponse(_WEB_CHROME_PLACEHOLDER_PAGE, status_code=503)
    return FileResponse(index_path, media_type="text/html")


@router.get("/web/assets/{asset_path:path}")
def web_chrome_asset(asset_path: str) -> FileResponse:
    """Serve one built asset from the web-chrome bundle (referenced as /web/assets/*).

    Same lazy resolution + traversal guard as the accounts asset route.
    """
    assets_dir = (web_chrome_dist_dir() / "assets").resolve()
    candidate = (assets_dir / asset_path).resolve()
    if not candidate.is_relative_to(assets_dir) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(candidate)


@router.get("/web", response_model=None)
@router.get("/web/{page_path:path}", response_model=None)
def web_chrome_page(page_path: str = "") -> HTMLResponse | FileResponse:
    """Serve the web-chrome SPA shell for every /web page path.

    The SPA does its own client-side routing (overview, create, workspace
    shell, settings), so every page path gets the same index.html. Asset
    requests never land here -- the more specific /web/assets route above is
    registered first.
    """
    return _serve_web_chrome_index()


# ---------------------------------------------------------------------------
# JSON API for the hosted pages
# ---------------------------------------------------------------------------


def _reject_cross_site_post(request: Request) -> None:
    """Refuse a state-changing request whose Origin names a different site.

    Login CSRF needs no existing cookie (the attack SETS the victim's session
    to the attacker's account), so SameSite alone is not enough. Browsers send
    Origin on all fetch POSTs; an absent header (non-browser clients, tests)
    is allowed -- they are not CSRF victims.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return
    if urlparse(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(status_code=403, detail="Cross-site requests are not accepted")


class AccountsConfigResponse(BaseModel):
    turnstile_site_key: str = Field(description="Cloudflare Turnstile site key; empty disables the widget")
    google_enabled: bool = Field(description="Whether the Continue-with-Google button should render")


@router.get("/accounts/api/config", response_model=AccountsConfigResponse)
def accounts_config() -> AccountsConfigResponse:
    """Static page configuration (safe to serve unauthenticated)."""
    return AccountsConfigResponse(
        turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", ""),
        google_enabled=get_accounts_oauth_provider() is not None,
    )


@router.get("/accounts/api/me")
def accounts_me(request: Request) -> JSONResponse:
    """The browser session's identity, or 401 when signed out."""
    with handle_endpoint_errors():
        identity = _resolve_browser_identity(request)
        if identity is None:
            return JSONResponse({"signed_in": False}, status_code=401)
        user_id, email, is_verified = identity
        return JSONResponse({"signed_in": True, "user_id": user_id, "email": email, "email_verified": is_verified})


def _client_ip(request: Request) -> str | None:
    """The end-client IP for Turnstile's optional ``remoteip`` check.

    Behind Modal's ingress the direct peer is the proxy, so the first
    ``x-forwarded-for`` hop is the visitor (matching how
    :func:`accounts_public_base_url` trusts ``x-forwarded-proto``); the
    socket peer is the fallback for direct (local/test) connections.
    """
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        first_hop = forwarded_for.split(",")[0].strip()
        if first_hop:
            return first_hop
    return request.client.host if request.client else None


def _verify_turnstile_token(token: str, remote_ip: str | None) -> bool:
    """Verify a Turnstile response token against Cloudflare's siteverify API.

    Fails closed on transport errors: an unverifiable signup is refused (the
    user can retry) rather than waved through while the check is down.
    """
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    if not secret:
        # No secret configured -> Turnstile is disabled on this tier.
        return True
    payload: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        response = httpx.post(_TURNSTILE_VERIFY_URL, data=payload, timeout=15.0)
        response.raise_for_status()
        return bool(response.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Turnstile verification failed: %s", exc)
        return False


class BrowserSignupRequest(BaseModel):
    email: str = Field(description="Email address to register")
    password: str = Field(description="Password for the new account")
    turnstile_token: str = Field(default="", description="Cloudflare Turnstile response token")


class BrowserSigninRequest(BaseModel):
    email: str = Field(description="Email address")
    password: str = Field(description="Password")


class BrowserAuthResponse(BaseModel):
    status: str = Field(description="OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, TURNSTILE_FAILED, ... or ERROR")
    message: str | None = Field(default=None)
    user: AuthUser | None = Field(default=None)


@router.post("/accounts/api/signup", response_model=BrowserAuthResponse)
def accounts_signup(request: Request, body: BrowserSignupRequest) -> BrowserAuthResponse:
    """Browser sign-up: create the account and establish the cookie session.

    Verification is non-blocking (no verification email is sent here); the
    Turnstile check is the bot gate for this public form.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        email = body.email.strip()
        if not email or not body.password:
            return BrowserAuthResponse(status="FIELD_ERROR", message="Email and password are required")
        if not _verify_turnstile_token(body.turnstile_token, _client_ip(request)):
            return BrowserAuthResponse(
                status="TURNSTILE_FAILED", message="Could not verify you are human. Please retry the challenge."
            )
        field_rejection = auth_proxy_module.signup_field_rejection(email, body.password)
        if field_rejection is not None:
            return BrowserAuthResponse(status=field_rejection.status, message=field_rejection.message)
        try:
            rejection = auth_proxy_module.cross_method_signup_rejection(email, "emailpassword")
            if rejection is not None:
                return BrowserAuthResponse(status=rejection.status, message=rejection.message)
            result = ep_sign_up(tenant_id=AUTH_TENANT_ID, email=email, password=body.password)
            if isinstance(result, EmailAlreadyExistsError):
                return BrowserAuthResponse(
                    status="EMAIL_ALREADY_EXISTS", message="An account with this email already exists"
                )
            if not isinstance(result, EPSignUpOkResult):
                return BrowserAuthResponse(status="ERROR", message="Sign-up failed")
            _sdk_create_browser_session(request, result.user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during browser signup", exc_info=exc)
            return BrowserAuthResponse(status="ERROR", message="Auth backend unavailable")
        return BrowserAuthResponse(status="OK", user=AuthUser(user_id=result.user.id, email=email))


@router.post("/accounts/api/signin", response_model=BrowserAuthResponse)
def accounts_signin(request: Request, body: BrowserSigninRequest) -> BrowserAuthResponse:
    """Browser sign-in: establish the cookie session."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        email = body.email.strip()
        if not email or not body.password:
            return BrowserAuthResponse(status="FIELD_ERROR", message="Email and password are required")
        try:
            result = ep_sign_in(tenant_id=AUTH_TENANT_ID, email=email, password=body.password)
            if isinstance(result, WrongCredentialsError):
                return BrowserAuthResponse(status="WRONG_CREDENTIALS", message="Incorrect email or password")
            if not isinstance(result, EPSignInOkResult):
                return BrowserAuthResponse(status="ERROR", message="Sign-in failed")
            _sdk_create_browser_session(request, result.user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during browser signin", exc_info=exc)
            return BrowserAuthResponse(status="ERROR", message="Auth backend unavailable")
        return BrowserAuthResponse(status="OK", user=AuthUser(user_id=result.user.id, email=email))


@router.post("/accounts/api/signout")
def accounts_signout(request: Request) -> dict[str, object]:
    """Sign the browser out (revokes only this browser's session)."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        try:
            session = _sdk_get_browser_session(request)
        except TryRefreshTokenError as exc:
            # An expired-but-refreshable access token is NOT "already signed
            # out": answering OK would leave the refresh token alive. A 401
            # makes the frontend's transparent refresh-and-retry re-send the
            # sign-out with a fresh token, so the revocation actually lands.
            raise HTTPException(status_code=401, detail="Session needs refresh") from exc
        except (SuperTokensSessionError, SuperTokensGeneralError):
            session = None
        if session is not None:
            revoke_session(session.get_handle())
        return {"status": "OK"}


@router.post("/accounts/api/signout-all")
def accounts_signout_all(request: Request) -> dict[str, object]:
    """Sign out of ALL devices: revokes every session for the browser session's user."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        identity = _resolve_browser_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        user_id, _email, _verified = identity
        revoked = revoke_all_sessions_for_user(user_id=user_id)
        return {"status": "OK", "revoked_count": len(revoked)}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(description="The account's current password")
    new_password: str = Field(description="The new password to set")


@router.post("/accounts/api/change-password")
def accounts_change_password(request: Request, body: ChangePasswordRequest) -> dict[str, object]:
    """Change the signed-in user's password (requires the current password)."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        identity = _resolve_browser_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        user_id, email, _verified = identity
        if not body.current_password or not body.new_password:
            return {"status": "FIELD_ERROR", "message": "Both passwords are required"}
        try:
            signin_result = ep_sign_in(tenant_id=AUTH_TENANT_ID, email=email, password=body.current_password)
            if isinstance(signin_result, WrongCredentialsError):
                return {"status": "WRONG_CREDENTIALS", "message": "Current password is incorrect"}
            if not isinstance(signin_result, EPSignInOkResult):
                return {"status": "ERROR", "message": "Password change failed"}
            update_result = update_email_or_password(recipe_user_id=RecipeUserId(user_id), password=body.new_password)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during password change", exc_info=exc)
            return {"status": "ERROR", "message": "Auth backend unavailable"}
        if not isinstance(update_result, UpdateEmailOrPasswordOkResult):
            return {"status": "FIELD_ERROR", "message": "The new password does not meet the password policy"}
        return {"status": "OK"}


class VerifyEmailTokenRequest(BaseModel):
    token: str = Field(description="The verification token from the emailed link")
    tenant_id: str = Field(
        default="", description="The tenantId from the emailed link (defaults to the public tenant)"
    )


@router.post("/accounts/api/verify-email")
def accounts_verify_email_token(request: Request, body: VerifyEmailTokenRequest) -> dict[str, object]:
    """Consume an email-verification token from a clicked email link.

    Deliberately unauthenticated: the emailed token IS the credential, and the
    click may land in a browser with no session. Returns ``{"status": "OK"}``
    or ``{"status": "INVALID_TOKEN"}`` for the page to render.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        if not body.token:
            return {"status": "INVALID_TOKEN"}
        tenant_id = body.tenant_id or AUTH_TENANT_ID
        try:
            result = verify_email_using_token(tenant_id=tenant_id, token=body.token)
        except (SuperTokensSessionError, SuperTokensGeneralError, ValueError) as exc:
            logger.error("Email verification error", exc_info=exc)
            return {"status": "INVALID_TOKEN"}
        if isinstance(result, VerifyEmailUsingTokenOkResult):
            return {"status": "OK"}
        return {"status": "INVALID_TOKEN"}


@router.post("/accounts/api/send-verification")
def accounts_send_verification(request: Request) -> dict[str, object]:
    """Send the browser session's own verification email (server cooldown applies)."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        identity = _resolve_browser_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        user_id, email, is_verified = identity
        if is_verified:
            return {"status": "OK", "sent": False, "already_verified": True}
        recipe_user_id = auth_proxy_module.recipe_user_id_for_callers_email(user_id, email)
        is_sent = auth_proxy_module.send_verification_email_with_cooldown(
            user_id=user_id, recipe_user_id=recipe_user_id, email=email
        )
        return {"status": "OK", "sent": is_sent, "already_verified": False}


# ---------------------------------------------------------------------------
# Device handoff: authorize (mint one-time code) + token exchange
# ---------------------------------------------------------------------------


class DeviceAuthCodeStore(Protocol):
    """Persistence for one-time device authorization codes."""

    def insert_code(
        self, code_hash: str, user_id: str, code_challenge: str, redirect_uri: str, expires_at: datetime
    ) -> None: ...

    def consume_code(self, code_hash: str) -> dict[str, Any] | None:
        """Atomically consume an unexpired, unconsumed code; returns its row or None."""
        ...


class PostgresDeviceAuthCodeStore:
    """Neon-backed store; single-use consumption is an atomic UPDATE."""

    def insert_code(
        self, code_hash: str, user_id: str, code_challenge: str, redirect_uri: str, expires_at: datetime
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Opportunistic cleanup keeps the table tiny without a cron.
                    cur.execute("DELETE FROM device_auth_codes WHERE expires_at < NOW()")
                    cur.execute(
                        "INSERT INTO device_auth_codes (code_hash, user_id, code_challenge, redirect_uri, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (code_hash, user_id, code_challenge, redirect_uri, expires_at),
                    )
        finally:
            conn.close()

    def consume_code(self, code_hash: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE device_auth_codes SET consumed_at = NOW() "
                        "WHERE code_hash = %s AND consumed_at IS NULL AND expires_at > NOW() "
                        "RETURNING user_id, code_challenge, redirect_uri",
                        (code_hash,),
                    )
                    row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"user_id": row[0], "code_challenge": row[1], "redirect_uri": row[2]}


_device_code_store: DeviceAuthCodeStore | None = None


def get_device_code_store() -> DeviceAuthCodeStore:
    global _device_code_store
    if _device_code_store is None:
        _device_code_store = PostgresDeviceAuthCodeStore()
    return _device_code_store


def _hash_device_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def is_valid_loopback_redirect_uri(redirect_uri: str) -> bool:
    return _LOOPBACK_REDIRECT_URI_RE.match(redirect_uri) is not None


def sanitize_local_next_path(candidate: str) -> str:
    """Clamp a post-login redirect target to a same-host path (no scheme/host smuggling)."""
    if candidate.startswith("/") and not candidate.startswith("//") and not candidate.startswith("/\\"):
        return candidate
    return "/"


@router.get("/accounts/authorize")
def accounts_authorize(request: Request) -> RedirectResponse:
    """Authorize a device handoff: mint a one-time code and bounce to the app's loopback.

    Query parameters: ``redirect_uri`` (loopback only), ``code_challenge``
    (PKCE S256), ``state`` (opaque, echoed back), and ``confirmed=1`` once the
    user has confirmed the account on the login page. Without a session (or
    without confirmation) the browser is sent to the login page, which brings
    them back here, so the official flow always goes through an explicit user
    action. Note that ``confirmed`` is client-supplied (a crafted link can
    carry it), so the interstitial is a UX property, not a security boundary;
    what actually protects the handoff is that the code only ever goes to a
    loopback ``redirect_uri`` on the user's own machine and is useless
    without the PKCE verifier.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        redirect_uri = request.query_params.get("redirect_uri", "")
        code_challenge = request.query_params.get("code_challenge", "")
        state = request.query_params.get("state", "")
        if not is_valid_loopback_redirect_uri(redirect_uri):
            raise HTTPException(status_code=400, detail="redirect_uri must be a loopback http URL")
        if not code_challenge or not state:
            raise HTTPException(status_code=400, detail="code_challenge and state are required")
        identity = _resolve_browser_identity(request)
        is_confirmed = request.query_params.get("confirmed") == "1"
        if identity is None or not is_confirmed:
            self_url = f"/accounts/authorize?{urlencode({'redirect_uri': redirect_uri, 'code_challenge': code_challenge, 'state': state})}"
            return RedirectResponse(url=f"/login?next={quote(self_url, safe='')}", status_code=302)
        user_id, _email, _verified = identity
        code = secrets.token_urlsafe(_DEVICE_CODE_BYTES)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=_DEVICE_CODE_TTL_SECONDS)
        get_device_code_store().insert_code(
            code_hash=_hash_device_code(code),
            user_id=user_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            url=f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}", status_code=302
        )


class DeviceTokenRequest(BaseModel):
    code: str = Field(description="The one-time code delivered to the loopback redirect")
    code_verifier: str = Field(description="The PKCE verifier whose S256 hash was sent as code_challenge")
    redirect_uri: str = Field(description="The exact loopback redirect_uri the code was minted for")


def compute_pkce_challenge(code_verifier: str) -> str:
    """The S256 PKCE challenge: base64url(sha256(verifier)) without padding.

    utf-8 (not ascii) because the verifier is client-supplied: an RFC
    7636-valid verifier is ASCII (identical bytes either way), and a
    non-ASCII one must fail the challenge comparison with the normal 400
    rather than blow up encoding (a 500).
    """
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@router.post("/auth/device/token")
def device_token_exchange(body: DeviceTokenRequest) -> dict[str, object]:
    """Exchange a one-time device code (+ PKCE verifier) for a fresh SuperTokens session.

    The minted session belongs to the device; the browser session that
    authorized the handoff is untouched. Single-use: a replayed code gets the
    same generic 400 as an unknown one.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        if not body.code or not body.code_verifier:
            raise HTTPException(status_code=400, detail="code and code_verifier are required")
        row = get_device_code_store().consume_code(_hash_device_code(body.code))
        if row is None:
            raise HTTPException(status_code=400, detail="Invalid, expired, or already-used code")
        if not secrets.compare_digest(compute_pkce_challenge(body.code_verifier), str(row["code_challenge"])):
            raise HTTPException(status_code=400, detail="PKCE verification failed")
        if body.redirect_uri != row["redirect_uri"]:
            raise HTTPException(status_code=400, detail="redirect_uri does not match the authorized request")
        user_id = str(row["user_id"])
        email, _is_verified = auth_module.resolve_account_email(user_id)
        if email is None:
            raise HTTPException(status_code=400, detail="Account no longer resolvable")
        tokens = build_session_tokens(user_id)
        return {
            "status": "OK",
            "user": {"user_id": user_id, "email": email, "display_name": None},
            "tokens": {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token},
        }


# ---------------------------------------------------------------------------
# Browser Google OAuth (Continue with Google on the hosted pages)
# ---------------------------------------------------------------------------


def accounts_signing_key() -> rsa.RSAPrivateKey:
    """The RS256 key OAuth state JWTs are signed with (shared with the share broker)."""
    key_pem = os.environ.get("BROKER_JWT_SIGNING_KEY_PEM", "")
    if not key_pem:
        raise MissingShareConfigError("BROKER_JWT_SIGNING_KEY_PEM")
    try:
        private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except ValueError as exc:
        raise MissingShareConfigError("BROKER_JWT_SIGNING_KEY_PEM (not a valid PEM private key)") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise MissingShareConfigError("BROKER_JWT_SIGNING_KEY_PEM (must be an RSA private key)")
    return private_key


def get_accounts_oauth_provider() -> Provider | None:
    """The Google provider for browser sign-in, or None when not configured.

    Built from the ``BROKER_GOOGLE_CLIENT_ID`` / ``BROKER_GOOGLE_CLIENT_SECRET``
    pair in the ``sharing-<env>`` secret: a Web-application OAuth client with
    each tier's callback URL (or the tier's OAuth redirector) registered.
    Distinct from the supertokens secret's Desktop-type ``GOOGLE_CLIENT_*``
    pair, which serves the deprecated CLI loopback OAuth flow.
    """
    client_id = os.environ.get("BROKER_GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("BROKER_GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    provider_input = ProviderInput(
        config=ProviderConfig(
            third_party_id="google",
            clients=[ProviderClientConfig(client_id=client_id, client_secret=client_secret)],
        ),
    )
    # Async-only on the SDK; for Google it assembles static endpoint config
    # without any network calls, so the sync wrapper is safe here.
    return _supertokens_sync_run(find_and_create_provider_instance([provider_input], "google", None, {}))


def accounts_public_base_url(request: Request) -> str:
    """The externally-visible base URL, for building OAuth redirect URIs.

    ``ACCOUNTS_BASE_URL`` (sharing secret) wins when set; otherwise derived
    from the request (dev tiers: the per-env connector URL).
    """
    configured = os.environ.get("ACCOUNTS_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


def _oauth_redirect_uri(request: Request) -> str:
    """The redirect URI to hand Google: the tier's fixed OAuth redirector when configured, else our own callback.

    Dev/CI tiers register exactly one redirect URI per tier -- the redirector
    (``OAUTH_REDIRECTOR_URL``), which forwards the provider callback to the
    per-env connector callback carried in the state JWT's ``cb`` claim.
    """
    redirector = os.environ.get("OAUTH_REDIRECTOR_URL", "").strip()
    if redirector:
        return redirector
    return accounts_public_base_url(request) + OAUTH_GOOGLE_CALLBACK_PATH


def mint_oauth_state(signing_key: rsa.RSAPrivateKey, nonce: str, next_path: str, callback_url: str) -> str:
    """Mint the self-contained OAuth state: browser nonce + post-login path + our callback URL.

    Self-contained (signed, never stored) because the connector runs as
    multiple concurrent containers. The ``cb`` claim is what the per-tier
    OAuth redirector reads (unverified -- it validates the host pattern
    instead) to know which env's connector to forward the callback to.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "purpose": _OAUTH_STATE_PURPOSE,
        "nonce": nonce,
        "next": next_path,
        "cb": callback_url,
        "iat": now,
        "exp": now + timedelta(seconds=_OAUTH_STATE_TTL_SECONDS),
    }
    return pyjwt.encode(payload, signing_key, algorithm=_OAUTH_STATE_ALGORITHM)


def verify_oauth_state(public_key: rsa.RSAPublicKey, state: str) -> tuple[str, str] | None:
    """Return ``(nonce, next_path)`` from a valid OAuth state token, or None when invalid/expired."""
    try:
        claims = pyjwt.decode(state, public_key, algorithms=[_OAUTH_STATE_ALGORITHM])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("purpose") != _OAUTH_STATE_PURPOSE:
        return None
    nonce = claims.get("nonce")
    next_path = claims.get("next")
    if not isinstance(nonce, str) or not nonce or not isinstance(next_path, str):
        return None
    return nonce, sanitize_local_next_path(next_path)


def _login_redirect(next_path: str, error_code: str = "") -> RedirectResponse:
    """Bounce the browser back to the hosted login page (optionally carrying an error banner).

    ``error_code`` is a short code the login page maps to its own copy (see
    the frontend's ``LOGIN_ERROR_COPY``) -- never free text, so a crafted link
    cannot make the official login page display attacker-chosen prose.
    """
    params: dict[str, str] = {}
    if next_path and next_path != "/":
        params["next"] = next_path
    if error_code:
        params["error"] = error_code
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/login{suffix}", status_code=303)


@router.get("/accounts/oauth/google/start")
def accounts_oauth_start(request: Request) -> RedirectResponse:
    """Begin the browser Google sign-in: stamp a nonce cookie and bounce to the provider."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_accounts_oauth_provider()
        if provider is None:
            raise HTTPException(status_code=404, detail="Google sign-in is not configured on this server")
        next_path = sanitize_local_next_path(request.query_params.get("next", "/"))
        nonce = secrets.token_urlsafe(16)
        redirect_uri = _oauth_redirect_uri(request)
        callback_url = accounts_public_base_url(request) + OAUTH_GOOGLE_CALLBACK_PATH
        state = mint_oauth_state(accounts_signing_key(), nonce, next_path, callback_url)
        redirect = _supertokens_sync_run(
            provider.get_authorisation_redirect_url(
                redirect_uri_on_provider_dashboard=redirect_uri,
                user_context={},
            )
        )
        # The SDK builds the provider URL without a ``state``; splice ours in
        # (replacing any present) rather than blindly appending a second one.
        authorize_parts = urlsplit(redirect.url_with_query_params)
        authorize_query = dict(parse_qsl(authorize_parts.query))
        authorize_query["state"] = state
        authorize_url = authorize_parts._replace(query=urlencode(authorize_query)).geturl()
        response = RedirectResponse(url=authorize_url, status_code=302)
        # The nonce cookie binds the callback to the browser that started the
        # flow (login CSRF: the signed state alone would let an attacker force
        # a victim's browser into the attacker's account).
        response.set_cookie(
            key=_OAUTH_NONCE_COOKIE_NAME,
            value=nonce,
            max_age=_OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response


@router.get(OAUTH_GOOGLE_CALLBACK_PATH)
def accounts_oauth_callback(request: Request) -> RedirectResponse:
    """Finish the browser Google sign-in: verify state + nonce, create the cookie session, resume ``next``."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_accounts_oauth_provider()
        if provider is None:
            raise HTTPException(status_code=404, detail="Google sign-in is not configured on this server")
        state_param = request.query_params.get("state", "")
        verified_state = verify_oauth_state(accounts_signing_key().public_key(), state_param)
        if verified_state is None:
            return _login_redirect("/", "invalid_state")
        nonce, next_path = verified_state
        if request.query_params.get("error"):
            # The provider reported a failure (most commonly a cancelled consent screen).
            return _login_redirect(next_path, "provider_cancelled")
        cookie_nonce = request.cookies.get(_OAUTH_NONCE_COOKIE_NAME, "")
        # Compare over UTF-8 bytes: compare_digest raises TypeError on str
        # operands containing non-ASCII characters, and the cookie value is
        # attacker-controllable -- a crafted cookie must yield the clean
        # nonce_mismatch redirect, not a 500. (The minted nonce is ASCII.)
        if not cookie_nonce or not secrets.compare_digest(cookie_nonce.encode("utf-8"), nonce.encode("utf-8")):
            return _login_redirect(next_path, "nonce_mismatch")

        redirect_uri = _oauth_redirect_uri(request)
        auth_result = auth_proxy_module.complete_oauth_code_exchange(
            provider=provider,
            provider_id="google",
            callback_url=redirect_uri,
            query_params=dict(request.query_params),
        )
        if auth_result.status == ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS:
            return _login_redirect(next_path, "password_account")
        if auth_result.status != "OK" or auth_result.user is None:
            logger.warning("Accounts OAuth callback failed: %s", auth_result.message)
            return _login_redirect(next_path, "oauth_failed")

        _sdk_create_browser_session(request, auth_result.user.user_id)
        # This login IS the account confirmation, so the handoff proceeds
        # without a second interstitial.
        resume_path = _mark_next_confirmed(next_path)
        response = RedirectResponse(url=resume_path, status_code=303)
        response.delete_cookie(key=_OAUTH_NONCE_COOKIE_NAME, path="/")
        return response


def _mark_next_confirmed(next_path: str) -> str:
    """Stamp ``confirmed=1`` onto a pending authorize next path.

    An explicit sign-in (password form or OAuth) is itself the account
    confirmation, so the handoff must not show a second "Continue as ..."
    interstitial. Covers both ``/accounts/authorize`` (device handoff) and
    ``/share/authorize`` (share visit), mirroring the frontend's
    ``markNextConfirmed``. Non-authorize paths pass through unchanged.
    """
    if not (next_path.startswith("/accounts/authorize") or next_path.startswith("/share/authorize")):
        return next_path
    separator = "&" if "?" in next_path else "?"
    if "confirmed=1" in next_path:
        return next_path
    return f"{next_path}{separator}confirmed=1"
