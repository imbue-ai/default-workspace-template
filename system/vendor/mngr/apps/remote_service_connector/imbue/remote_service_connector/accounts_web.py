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
import html
import http.client
import logging
import os
import re
import secrets
import threading
import urllib.request
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
import psycopg2
import yaml
from cachetools import TTLCache
from cachetools import cached
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
from supertokens_python.syncio import delete_user
from supertokens_python.types import RecipeUserId

import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.auth_proxy as auth_proxy_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.signup_hardening as signup_hardening_module
import imbue.remote_service_connector.suspension as suspension_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.attribution import ATTRIBUTION_COOKIE_NAME
from imbue.remote_service_connector.attribution import record_account_attribution
from imbue.remote_service_connector.attribution import record_download_event
from imbue.remote_service_connector.auth_proxy import ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS
from imbue.remote_service_connector.auth_proxy import AUTH_TENANT_ID
from imbue.remote_service_connector.auth_proxy import AuthUser
from imbue.remote_service_connector.auth_proxy import build_session_tokens
from imbue.remote_service_connector.auth_proxy import require_supertokens_configured
from imbue.remote_service_connector.entitlements import SIGNUP_SELECTABLE_PLAN_NAMES
from imbue.remote_service_connector.entitlements import create_entitlements_row_from_plan
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()

# Where the built accounts frontend bundle lives inside the deployed image
# (added as an image directory by app.py; ``minds-admin env deploy`` builds it
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

# The only ``x-forwarded-proto`` values ``accounts_public_base_url`` will trust
# (the header is client-controlled behind Modal's ingress); anything else falls
# back to the ASGI scheme rather than being spliced into the base URL verbatim.
_TRUSTED_FORWARDED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

# The only platform that tracks a release channel meaningfully.
_MAC_ARM64_PLATFORM: Final[str] = "mac-arm64"

# Default per-platform installer links.
_DEFAULT_TARGET_BY_PLATFORM: Final[dict[str, str]] = {
    # For _MAC_ARM64_PLATFORM, this is the hardcoded fallback, used only when the live manifest is down.
    _MAC_ARM64_PLATFORM: "https://dl.todesktop.com/26032588hqdzk/mac/dmg/arm64",
    "source": "https://github.com/imbue-ai/mngr",
}

_STABLE_CHANNEL_MANIFEST_URL: Final[str] = "https://updates.imbueminds.com/stable-mac.yml"
_STABLE_CHANNEL_CACHE_SECONDS: Final[float] = 60.0
_STABLE_CHANNEL_FETCH_TIMEOUT_SECONDS: Final[float] = 2.0
_ARM64_DMG_SUFFIX: Final[str] = "-arm64.dmg"
# Where ToDesktop serves builds, and so the only host this route will redirect to.
_TODESKTOP_DOWNLOAD_PREFIX: Final[str] = "https://download.todesktop.com/"


def _arm64_dmg_urls(manifest: str) -> set[str]:
    """Every arm64 .dmg an electron-updater channel manifest offers.

    Read out of ``files[].url``, which is what names an artifact -- a url under
    some other key, now or later, is not one. A url that does not point at
    ToDesktop is not a candidate either, so the route falls back rather than
    sending anyone wherever the feed happened to say.
    """
    document = yaml.safe_load(manifest)
    entries = document.get("files") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return set()
    urls = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", ""))
        if url.startswith(_TODESKTOP_DOWNLOAD_PREFIX) and url.endswith(_ARM64_DMG_SUFFIX):
            urls.add(url)
    return urls


def _fetch_stable_channel_manifest() -> str:
    # The feed's CDN answers 403 to `Python-urllib/<version>` by name.
    request = urllib.request.Request(_STABLE_CHANNEL_MANIFEST_URL, headers={"User-Agent": "minds-connector"})
    with urllib.request.urlopen(request, timeout=_STABLE_CHANNEL_FETCH_TIMEOUT_SECONDS) as response:
        return response.read().decode()


def resolve_stable_mac_arm64_url(fetch: Callable[[], str] = _fetch_stable_channel_manifest) -> str | None:
    """The arm64 .dmg the stable channel serves, or None to fall back.

    Uncached; ``stable_mac_arm64_url`` is the entry point the route uses.
    """
    try:
        manifest = fetch()
    except (OSError, http.client.HTTPException, UnicodeDecodeError) as exc:
        # OSError to capture socket level errors
        logger.warning("Could not resolve the stable download: %s", exc)
        return None
    try:
        urls = _arm64_dmg_urls(manifest)
    except yaml.YAMLError as exc:
        logger.warning("Could not resolve the stable download: unreadable manifest: %s", exc)
        return None
    if len(urls) != 1:
        logger.warning(
            "Could not resolve the stable download: the manifest names %d distinct arm64 .dmg urls, not 1", len(urls)
        )
        return None
    return urls.pop()


# The condition serialises concurrent misses, so a cold container hit by several
# downloads at once reads the feed once rather than once per request.
@cached(cache=TTLCache(maxsize=1, ttl=_STABLE_CHANNEL_CACHE_SECONDS), condition=threading.Condition())
def stable_mac_arm64_url() -> str | None:
    """What the route redirects to, re-read at most once per TTL.

    Each container caches independently, so a promotion reaches every one of
    them within the TTL. A read that fails caches its None too, so an outage
    costs one download the fetch timeout rather than every one.
    """
    return resolve_stable_mac_arm64_url()


# Friendly aliases resolve server-side so marketing links stay stable if a
# platform's default target ever changes (e.g. "mac" moving off arm64).
_DOWNLOAD_PLATFORM_BY_ALIAS: Final[dict[str, str]] = {
    "mac": _MAC_ARM64_PLATFORM,
}

# Caps on the campaign context carried through the OAuth state JWT: the whole
# state rides Google's authorize URL, so keep it comfortably small.
_OAUTH_STATE_MAX_PAGE_QUERY_CHARS: Final[int] = 512
_OAUTH_STATE_MAX_PAGE_PATH_CHARS: Final[int] = 256
_OAUTH_STATE_MAX_PLAN_CHARS: Final[int] = 32


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
            logger.warning("Could not revoke an over-max-age browser session", exc_info=exc)
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


def resolve_web_user_identity(
    request: Request,
    # Force the against-the-core session check on a read route whose response
    # is sensitive enough to warrant it (no current route needs this; the
    # state-modifying methods below always get it).
    is_database_check_required: bool = False,
) -> tuple[auth_module.UserAuth, str]:
    """Return ``(UserAuth, full user_id)`` from a Bearer token or the browser session.

    The full user id is what share coordinates and LiteLLM keys are scoped by
    (a ``UserAuth`` alone only carries the 16-hex prefix).

    State-modifying methods (anything but GET/HEAD/OPTIONS) verify the Bearer
    session against the SuperTokens core (``check_database``), so a revoked
    session is refused within one request instead of coasting on its
    signature-valid access token for up to ~1h. Read methods keep the cheap
    stateless validation. The browser branch always checks the core (see
    ``_sdk_get_browser_session``), so the two credential paths agree on
    state-modifying requests.
    """
    is_state_modifying = request.method not in ("GET", "HEAD", "OPTIONS")
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        user = auth_module.authenticate_request(
            request, check_database=is_state_modifying or is_database_check_required
        )
        return user, auth_module.get_user_id_from_bearer_header(request)
    if is_state_modifying:
        _reject_cross_site_post(request)
    identity = _resolve_browser_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    user_id, email, is_verified = identity
    auth_module.stash_authenticated_user_for_access_log(request, user_id)
    user = auth_module.UserAuth(
        user_id_prefix=auth_module.derive_user_id_prefix(user_id),
        email=email,
        is_email_verified=is_verified,
        user_id=user_id,
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
    "by <code>minds-admin env deploy</code>) or point ACCOUNTS_FRONTEND_DIST at a build.</p>"
    "</body></html>"
)


def _serve_frontend_page(filename: str) -> HTMLResponse | FileResponse:
    """Serve one file from the built accounts bundle (the SPA index or a static doc page)."""
    page_path = frontend_dist_dir() / filename
    if not page_path.is_file():
        return HTMLResponse(_PLACEHOLDER_PAGE, status_code=503)
    return FileResponse(page_path, media_type="text/html")


def _serve_frontend_index() -> HTMLResponse | FileResponse:
    return _serve_frontend_page("index.html")


def _configured_accounts_origin() -> str:
    """The tier's dedicated accounts origin (no trailing slash), or '' when there is none (dev/CI)."""
    return os.environ.get("ACCOUNTS_BASE_URL", "").strip().rstrip("/")


def _configured_chrome_origin_host() -> str:
    """Host of the tier's web-chrome origin, or '' when none is configured."""
    origin = os.environ.get("SHARE_CHROME_ORIGIN", "").strip().rstrip("/")
    if not origin:
        return ""
    return urlsplit(origin).netloc.lower()


def _refuse_misdirected_browser_page(
    request: Request,
    # Whether the page still works on the chrome origin. The account pages do
    # (the session cookie is apex-scoped, shared with the chrome); the Google
    # OAuth start does not (its nonce cookie is host-only while the callback
    # always lands on the accounts origin), so it passes False.
    is_chrome_origin_allowed: bool,
    # Path + query (leading slash) appended to the accounts origin to build
    # the "continue" link on the refusal page.
    continue_path: str,
) -> HTMLResponse | None:
    """Refuse a browser identity page served on a host where it cannot work, or None to serve it.

    On tiers with a dedicated accounts origin, sign-in only functions there:
    the session cookie's Domain is the accounts apex (rejected on unrelated
    hosts like the connector's *.modal.run URL), and a Google flow started
    elsewhere strands its host-only nonce cookie and dead-ends in
    nonce_mismatch after the whole provider round-trip. Refusing up front with
    a link to the right origin turns that silent late failure into an
    immediate, recoverable one (old shipped clients still open these pages on
    the connector host). Tiers without an accounts origin serve everything on
    the connector host, so the guard is inert there.
    """
    accounts_origin = _configured_accounts_origin()
    if not accounts_origin:
        return None
    request_host = request.url.netloc.lower()
    if request_host == urlsplit(accounts_origin).netloc.lower():
        return None
    if is_chrome_origin_allowed and request_host == _configured_chrome_origin_host():
        return None
    emit_metric("misdirected_browser_page", 1, {"path": request.url.path})
    continue_url = html.escape(accounts_origin + continue_path, quote=True)
    body = (
        "<!doctype html><html><head><title>Wrong sign-in address</title></head><body>"
        "<h1>This page is served at a different address</h1>"
        f"<p>Sign-in pages for this service live at <a href='{continue_url}'>{continue_url}</a>. "
        "Continue there to sign in.</p>"
        "</body></html>"
    )
    # 421 Misdirected Request: this server name cannot produce a working
    # response for the target, and the status is distinct enough that it can
    # never be mistaken for an ordinary 404.
    return HTMLResponse(body, status_code=421)


def _request_path_with_query(request: Request) -> str:
    if request.url.query:
        return f"{request.url.path}?{request.url.query}"
    return request.url.path


@router.get("/login", response_model=None)
def accounts_login_page(request: Request) -> HTMLResponse | FileResponse:
    """The hosted sign-in page (also renders the sign-up tab and the continue-as interstitial)."""
    misdirected = _refuse_misdirected_browser_page(
        request, is_chrome_origin_allowed=True, continue_path=_request_path_with_query(request)
    )
    if misdirected is not None:
        return misdirected
    return _serve_frontend_index()


@router.get("/signup", response_model=None)
def accounts_signup_page(request: Request) -> HTMLResponse | FileResponse:
    """The hosted sign-up page (the same bundle, leading with the sign-up tab)."""
    misdirected = _refuse_misdirected_browser_page(
        request, is_chrome_origin_allowed=True, continue_path=_request_path_with_query(request)
    )
    if misdirected is not None:
        return misdirected
    return _serve_frontend_index()


@router.get("/manage", response_model=None)
def accounts_manage_page(request: Request) -> HTMLResponse | FileResponse:
    """The signed-in account-management page (identity, verification, password, sessions).

    Deliberately NOT ``/account`` -- that path is the deprecated JSON account
    API released clients still call.
    """
    misdirected = _refuse_misdirected_browser_page(
        request, is_chrome_origin_allowed=True, continue_path=_request_path_with_query(request)
    )
    if misdirected is not None:
        return misdirected
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


@router.get("/terms-of-service", response_model=None)
def terms_of_service_page() -> HTMLResponse | FileResponse:
    """The Terms of Service, linked from the signup form's agreement checkbox.

    A plain static HTML document shipped in the accounts bundle
    (``frontend/public/terms-of-service.html``), not part of the SPA.
    """
    return _serve_frontend_page("terms-of-service.html")


@router.get("/code-of-conduct", response_model=None)
def code_of_conduct_page() -> HTMLResponse | FileResponse:
    """The Code of Conduct, linked from the signup form's agreement checkbox."""
    return _serve_frontend_page("code-of-conduct.html")


@router.get("/privacy-policy", response_model=None)
def privacy_policy_page() -> HTMLResponse | FileResponse:
    """The privacy policy, linked from the plan selector's per-plan descriptions."""
    return _serve_frontend_page("privacy-policy.html")


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
    "by <code>minds-admin env deploy</code>) or point WEB_CHROME_FRONTEND_DIST at a build.</p>"
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
    """The trusted end-client IP: the socket peer, never a forwarding header.

    Modal's ingress delivers the real client as the connection peer and
    strips ``X-Forwarded-For``; every other forwarding-style header passes
    through unsanitized and must never be consulted (see
    ``signup_hardening.client_ip_for_request``). This value is load-bearing:
    it keys the signup velocity limits and reputation checks, not just
    Turnstile's advisory ``remoteip``.
    """
    return signup_hardening_module.client_ip_for_request(request)


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
        emit_metric("turnstile_verify_request_failed", 1, {})
        logger.warning("Turnstile verification failed", exc_info=exc)
        return False


def _record_signup_plan_choice(user_id: str, plan_name: str) -> None:
    """Create the just-created account's entitlements row from the signup plan selector.

    Fails open: a failed write logs a warning and account creation proceeds --
    the lazy backfill then assigns the free plan, which never grants analytics
    consent, so a lost explorer choice costs benefits rather than privacy (the
    user can re-select the plan on their Accounts page). An empty or unknown
    plan (frontends predating the selector, crafted values) writes nothing.
    KeyError covers a missing DATABASE_URL, like the attribution writer.
    """
    normalized_plan = plan_name.strip().lower()
    if normalized_plan not in SIGNUP_SELECTABLE_PLAN_NAMES:
        if normalized_plan:
            logger.warning("Ignoring an unknown signup plan choice %r for user %s", normalized_plan, user_id[:8])
        return
    try:
        create_entitlements_row_from_plan(
            entitlements_module.get_entitlements_store(),
            user_id=user_id,
            user_id_prefix=auth_module.derive_user_id_prefix(user_id),
            plan_name=normalized_plan,
        )
    except (psycopg2.Error, KeyError) as exc:
        emit_metric("signup_plan_choice_write_failed", 1, {"plan": normalized_plan})
        logger.warning("Could not record the signup plan choice for user %s", user_id[:8], exc_info=exc)


class BrowserSignupRequest(BaseModel):
    email: str = Field(description="Email address to register")
    password: str = Field(description="Password for the new account")
    turnstile_token: str = Field(default="", description="Cloudflare Turnstile response token")
    plan: str = Field(
        default="",
        description="The signup plan selector's choice ('explorer' or 'free'); empty from older frontends",
    )
    # Marketing-attribution context from the signup page itself (all
    # optional; released frontends that predate attribution omit them).
    attribution_page_query: str = Field(
        default="", description="The signup page's own query string (campaign params are extracted server-side)"
    )
    attribution_page_path: str = Field(default="", description="The signup page's path (e.g. /signup)")
    attribution_next: str = Field(
        default="", description="The page's next= target, classifying which surface sent the user here"
    )


class BrowserSigninRequest(BaseModel):
    email: str = Field(description="Email address")
    password: str = Field(description="Password")


class BrowserAuthResponse(BaseModel):
    status: str = Field(
        description=(
            "OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, TURNSTILE_FAILED, "
            "RATE_LIMITED, SIGNUP_BLOCKED, OAUTH_ONLY, ... or ERROR"
        )
    )
    message: str | None = Field(default=None)
    user: AuthUser | None = Field(default=None)


def _signup_ip_gate_rejection(
    assessment: signup_hardening_module.SignupIpAssessment, email: str
) -> BrowserAuthResponse | None:
    """The IP gate's refusal for a password signup, or None when it may proceed.

    Every refusal is recorded with its verdict (the caller records the
    allowed ones) so floods are visible in real time. Enforcement only
    applies on the restricted tiers; elsewhere the verdict is recorded but
    never refuses.
    """
    if not signup_hardening_module.is_signup_ip_enforcement_enabled():
        return None
    if assessment.is_rate_limited:
        signup_hardening_module.record_signup_attempt(
            assessment, email, "password", signup_hardening_module.SignupGateOutcome.RATE_LIMITED
        )
        return BrowserAuthResponse(
            status="RATE_LIMITED",
            message="Too many sign-ups from your network right now. Please try again later.",
        )
    if assessment.verdict is signup_hardening_module.SignupIpVerdict.ABUSIVE:
        signup_hardening_module.record_signup_attempt(
            assessment, email, "password", signup_hardening_module.SignupGateOutcome.BLOCKED
        )
        return BrowserAuthResponse(
            status="SIGNUP_BLOCKED",
            message="Sign-ups from this network are not accepted. Please try a different network connection.",
        )
    if assessment.verdict is signup_hardening_module.SignupIpVerdict.SUSPICIOUS:
        signup_hardening_module.record_signup_attempt(
            assessment, email, "password", signup_hardening_module.SignupGateOutcome.OAUTH_ONLY
        )
        return BrowserAuthResponse(
            status="OAUTH_ONLY",
            message=(
                "Email-and-password sign-up is not available from your network. "
                "Continue with Google to create your account."
            ),
        )
    return None


@router.post("/accounts/api/signup", response_model=BrowserAuthResponse)
def accounts_signup(request: Request, body: BrowserSignupRequest) -> BrowserAuthResponse:
    """Browser sign-up: create the account and establish the cookie session.

    Verification is non-blocking (no verification email is sent here). Two
    gates guard this public form: the IP gate (velocity limits + reputation,
    fail-open, enforced on restricted tiers -- see ``signup_hardening``) and
    the Turnstile human check (fail-closed).
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        _reject_cross_site_post(request)
        email = body.email.strip()
        if not email or not body.password:
            return BrowserAuthResponse(status="FIELD_ERROR", message="Email and password are required")
        client_ip = _client_ip(request)
        assessment = signup_hardening_module.assess_signup_ip(client_ip)
        gate_rejection = _signup_ip_gate_rejection(assessment, email)
        if gate_rejection is not None:
            return gate_rejection
        signup_hardening_module.record_signup_attempt(
            assessment, email, "password", signup_hardening_module.SignupGateOutcome.ALLOWED
        )
        if not _verify_turnstile_token(body.turnstile_token, client_ip):
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
            # Defensive: a just-created account has no suspension row, but every
            # session-creation path carries the gate so none can be missed.
            if suspension_module.is_user_suspended_at_gate(result.user.id, gate="browser_signup"):
                return BrowserAuthResponse(
                    status=suspension_module.ACCOUNT_SUSPENDED_STATUS,
                    message=suspension_module.SUSPENDED_USER_MESSAGE,
                )
            _sdk_create_browser_session(request, result.user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during browser signup", exc_info=exc)
            return BrowserAuthResponse(status="ERROR", message="Auth backend unavailable")
        # Stamp marketing attribution for the just-created account (creation
        # only -- sign-in never records anything). Fails open inside.
        record_account_attribution(
            user_id=result.user.id,
            email=email,
            cookie_value=request.cookies.get(ATTRIBUTION_COOKIE_NAME),
            page_query=body.attribution_page_query,
            page_path=body.attribution_page_path,
            next_path=body.attribution_next,
            signup_method="password",
        )
        # Record the plan the signup form selected (fails open inside; an
        # unrecorded choice degrades to the consent-free lazy default).
        _record_signup_plan_choice(result.user.id, body.plan)
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
            if suspension_module.is_user_suspended_at_gate(result.user.id, gate="browser_signin"):
                return BrowserAuthResponse(
                    status=suspension_module.ACCOUNT_SUSPENDED_STATUS,
                    message=suspension_module.SUSPENDED_USER_MESSAGE,
                )
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
        except ValueError as exc:
            # A malformed/expired token is routine client input, not a fault.
            emit_metric("email_verification_token_invalid", 1, {})
            logger.info("Rejected an email verification token: %s", exc)
            return {"status": "INVALID_TOKEN"}
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
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
        # The account may have been suspended between the browser authorize
        # step and this exchange; refuse rather than mint a device session.
        suspension_module.require_not_suspended(user_id, gate="device_token_exchange")
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
    configured = _configured_accounts_origin()
    if configured:
        return configured
    # Clamp the forwarded scheme (see _TRUSTED_FORWARDED_SCHEMES): an untrusted
    # value must never reach the f-string below, where it could change the
    # constructed URL's effective host (e.g. ``https://evil/?`` yields a URL
    # whose host parses as ``evil``). A ``minds://`` custom scheme is an
    # OS-level deep link, never an inbound request scheme, so it is not trusted.
    forwarded_scheme = request.headers.get("x-forwarded-proto", "").strip().lower()
    scheme = forwarded_scheme if forwarded_scheme in _TRUSTED_FORWARDED_SCHEMES else request.url.scheme
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


def mint_oauth_state(
    signing_key: rsa.RSAPrivateKey,
    nonce: str,
    next_path: str,
    callback_url: str,
    page_query: str,
    page_path: str,
    plan: str,
    is_terms_accepted: bool,
) -> str:
    """Mint the self-contained OAuth state: browser nonce + post-login path + our callback URL.

    Self-contained (signed, never stored) because the connector runs as
    multiple concurrent containers. The ``cb`` claim is what the per-tier
    OAuth redirector reads (unverified -- it validates the host pattern
    instead) to know which env's connector to forward the callback to. The
    ``pq``/``pp`` claims carry the login page's own query string and path
    across the provider round-trip so a Google *signup* can be attributed to
    the campaign params the page was opened with; ``pl``/``ta`` carry the
    signup form's plan choice and terms agreement the same way (both only
    consumed when the exchange CREATES an account).
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "purpose": _OAUTH_STATE_PURPOSE,
        "nonce": nonce,
        "next": next_path,
        "cb": callback_url,
        "iat": now,
        "exp": now + timedelta(seconds=_OAUTH_STATE_TTL_SECONDS),
    }
    if page_query:
        payload["pq"] = page_query[:_OAUTH_STATE_MAX_PAGE_QUERY_CHARS]
    if page_path:
        payload["pp"] = page_path[:_OAUTH_STATE_MAX_PAGE_PATH_CHARS]
    if plan:
        payload["pl"] = plan[:_OAUTH_STATE_MAX_PLAN_CHARS]
    if is_terms_accepted:
        payload["ta"] = True
    return pyjwt.encode(payload, signing_key, algorithm=_OAUTH_STATE_ALGORITHM)


class VerifiedOAuthState(BaseModel):
    """The claims recovered from a valid browser OAuth state JWT."""

    nonce: str = Field(description="The browser-binding nonce (matched against the nonce cookie)")
    next_path: str = Field(description="The sanitized post-login path")
    page_query: str = Field(default="", description="The login page's query string at OAuth start")
    page_path: str = Field(default="", description="The login page's path at OAuth start")
    plan: str = Field(default="", description="The signup form's plan choice at OAuth start ('' when absent)")
    is_terms_accepted: bool = Field(
        default=False, description="Whether the signup form's terms checkbox was checked at OAuth start"
    )


def verify_oauth_state(public_key: rsa.RSAPublicKey, state: str) -> VerifiedOAuthState | None:
    """Return the verified claims from an OAuth state token, or None when invalid/expired."""
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
    page_query = claims.get("pq")
    page_path = claims.get("pp")
    plan = claims.get("pl")
    return VerifiedOAuthState(
        nonce=nonce,
        next_path=sanitize_local_next_path(next_path),
        page_query=page_query if isinstance(page_query, str) else "",
        page_path=page_path if isinstance(page_path, str) else "",
        plan=plan if isinstance(plan, str) else "",
        is_terms_accepted=claims.get("ta") is True,
    )


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


def _roll_back_oauth_created_account(user: AuthUser, refusal: str) -> None:
    """Delete the SuperTokens user a refused OAuth exchange just created.

    Best-effort: the refusal stands either way; a surviving account got no
    session and is inert until a clean signup or sign-in.
    """
    try:
        delete_user(user.user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Could not roll back a %s OAuth-signup account %s: %s", refusal, user.user_id[:8], exc)


def _oauth_signup_ip_gate_redirect(request: Request, user: AuthUser, next_path: str) -> RedirectResponse | None:
    """The IP gate for a just-created Google account: a refusal redirect, or None to proceed.

    Applies only the velocity caps and the abusive band (vpn/proxy/relay IPs
    pass -- Google OAuth is the step-up path the suspicious band is sent to).
    A refusal deletes the account the exchange just created, records the
    outcome, and bounces to the login page's ``signup_blocked`` banner.
    """
    assessment = signup_hardening_module.assess_signup_ip(_client_ip(request))
    is_refused = signup_hardening_module.is_signup_ip_enforcement_enabled() and (
        assessment.is_rate_limited or assessment.verdict is signup_hardening_module.SignupIpVerdict.ABUSIVE
    )
    if not is_refused:
        signup_hardening_module.record_signup_attempt(
            assessment, user.email, "google", signup_hardening_module.SignupGateOutcome.ALLOWED
        )
        return None
    outcome = (
        signup_hardening_module.SignupGateOutcome.RATE_LIMITED
        if assessment.is_rate_limited
        else signup_hardening_module.SignupGateOutcome.BLOCKED
    )
    signup_hardening_module.record_signup_attempt(assessment, user.email, "google", outcome)
    _roll_back_oauth_created_account(user, "refused")
    return _login_redirect(next_path, "signup_blocked")


def _oauth_terms_gate_redirect(user: AuthUser, next_path: str) -> RedirectResponse:
    """Refuse a Google exchange that CREATED an account without the terms agreement.

    Account creation requires agreeing to the Terms of Service and Code of
    Conduct. The signup tab's Google button carries the checked box through
    the OAuth state (``ta``); a new-account exchange arriving without it (the
    sign-in tab's Google button on an email with no account yet) is rolled
    back and bounced to the login page's ``terms_required`` banner, whose
    remedy is the Create-account tab.
    """
    _roll_back_oauth_created_account(user, "terms-refused")
    return _login_redirect(next_path, "terms_required")


@router.get("/accounts/oauth/google/start", response_model=None)
def accounts_oauth_start(request: Request) -> RedirectResponse | HTMLResponse:
    """Begin the browser Google sign-in: stamp a nonce cookie and bounce to the provider."""
    with handle_endpoint_errors():
        next_path = sanitize_local_next_path(request.query_params.get("next", "/"))
        # A Google flow started on any host but the accounts origin can never
        # complete (the nonce cookie is host-only; the callback is registered
        # on the accounts origin), so refuse it up front -- on every other
        # host, the chrome origin included.
        continue_query = f"?{urlencode({'next': next_path})}" if next_path != "/" else ""
        misdirected = _refuse_misdirected_browser_page(
            request, is_chrome_origin_allowed=False, continue_path=f"/login{continue_query}"
        )
        if misdirected is not None:
            return misdirected
        require_supertokens_configured()
        provider = get_accounts_oauth_provider()
        if provider is None:
            raise HTTPException(status_code=404, detail="Google sign-in is not configured on this server")
        nonce = secrets.token_urlsafe(16)
        redirect_uri = _oauth_redirect_uri(request)
        callback_url = accounts_public_base_url(request) + OAUTH_GOOGLE_CALLBACK_PATH
        state = mint_oauth_state(
            accounts_signing_key(),
            nonce,
            next_path,
            callback_url,
            # The login page's own query/path, so a signup completed via
            # Google can still be attributed to the campaign params the page
            # was opened with.
            page_query=request.query_params.get("pq", ""),
            page_path=request.query_params.get("pp", ""),
            # The signup form's plan choice and terms agreement, consumed by
            # the callback only when the exchange creates a new account.
            plan=request.query_params.get("plan", ""),
            is_terms_accepted=request.query_params.get("terms") == "1",
        )
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
            emit_metric("oauth_state_invalid", 1, {"provider": "google-browser"})
            return _login_redirect("/", "invalid_state")
        nonce = verified_state.nonce
        next_path = verified_state.next_path
        if request.query_params.get("error"):
            # The provider reported a failure (most commonly a cancelled consent screen).
            return _login_redirect(next_path, "provider_cancelled")
        cookie_nonce = request.cookies.get(_OAUTH_NONCE_COOKIE_NAME, "")
        # Compare over UTF-8 bytes: compare_digest raises TypeError on str
        # operands containing non-ASCII characters, and the cookie value is
        # attacker-controllable -- a crafted cookie must yield the clean
        # nonce_mismatch redirect, not a 500. (The minted nonce is ASCII.)
        if not cookie_nonce or not secrets.compare_digest(cookie_nonce.encode("utf-8"), nonce.encode("utf-8")):
            emit_metric("oauth_nonce_mismatch", 1, {"provider": "google-browser"})
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
            emit_metric("oauth_callback_failed", 1, {"provider": "google-browser"})
            logger.warning("Accounts OAuth callback failed: %s", auth_result.message)
            return _login_redirect(next_path, "oauth_failed")

        if auth_result.is_new_account:
            # The IP gate for Google account CREATION (returning sign-ins are
            # untouched): only the velocity caps and the abusive band apply --
            # completing a real Google OAuth exchange IS the suspicious band's
            # step-up remedy. Refusal rolls the just-created SuperTokens user
            # back, so a blocked flood leaves no inert accounts behind.
            oauth_gate_redirect = _oauth_signup_ip_gate_redirect(request, auth_result.user, next_path)
            if oauth_gate_redirect is not None:
                return oauth_gate_redirect
            # Account creation requires the terms agreement, which only the
            # signup tab's Google button carries; without it the exchange is
            # rolled back (returning sign-ins never reach this).
            if not verified_state.is_terms_accepted:
                return _oauth_terms_gate_redirect(auth_result.user, next_path)

        if suspension_module.is_user_suspended_at_gate(auth_result.user.user_id, gate="browser_oauth"):
            return _login_redirect(next_path, "account_suspended")

        _sdk_create_browser_session(request, auth_result.user.user_id)
        if auth_result.is_new_account:
            # This OAuth exchange created the account (a returning Google
            # sign-in never records anything). Fails open inside.
            record_account_attribution(
                user_id=auth_result.user.user_id,
                email=auth_result.user.email,
                cookie_value=request.cookies.get(ATTRIBUTION_COOKIE_NAME),
                page_query=verified_state.page_query,
                page_path=verified_state.page_path,
                next_path=next_path,
                signup_method="google",
            )
            # Record the plan the signup form selected (fails open inside; an
            # unrecorded choice degrades to the consent-free lazy default).
            _record_signup_plan_choice(auth_result.user.user_id, verified_state.plan)
        # This login IS the account confirmation, so the handoff proceeds
        # without a second interstitial.
        resume_path = _mark_next_confirmed(next_path)
        response = RedirectResponse(url=resume_path, status_code=303)
        response.delete_cookie(key=_OAUTH_NONCE_COOKIE_NAME, path="/")
        return response


# ---------------------------------------------------------------------------
# Download redirect (the campaign -> download funnel denominator)
# ---------------------------------------------------------------------------


@router.get("/download")
def download_redirect(request: Request) -> RedirectResponse:
    """Record a campaign-tagged download event and bounce to the platform's installer.

    The marketing site's download buttons point here so campaign -> download
    conversion is measurable independent of signup. The event is tagged by the
    usual merge rule: the imbue_attribution cookie supplies the visitor id and
    first touch, and campaign params on this URL itself overwrite the last
    touch -- or synthesize the sole touch when the cookie is absent, so
    consent-declined downloads still count per-campaign. Fails open: the
    redirect always happens, a failed write loses one row.
    """
    with handle_endpoint_errors():
        raw_platform = request.query_params.get("platform", "")
        platform = _DOWNLOAD_PLATFORM_BY_ALIAS.get(raw_platform, raw_platform)
        if platform not in _DEFAULT_TARGET_BY_PLATFORM:
            raise HTTPException(status_code=404, detail="Unknown platform")
        target_url = _DEFAULT_TARGET_BY_PLATFORM[platform]
        if platform == _MAC_ARM64_PLATFORM:
            target_url = stable_mac_arm64_url() or target_url
        record_download_event(
            cookie_value=request.cookies.get(ATTRIBUTION_COOKIE_NAME),
            request_query=request.url.query,
            platform=platform,
            user_agent=request.headers.get("user-agent", ""),
        )
        return RedirectResponse(url=target_url, status_code=302)


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
