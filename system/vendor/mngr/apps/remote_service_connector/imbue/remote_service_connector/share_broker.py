"""Accounts broker for share login handoff (/share/*).

Served at the accounts domain (Modal custom domain; dev tiers use the plain
connector URL). A visitor hitting a shared workspace without a session is
302'd here by the workspace gateway; after browser login the broker mints a
60-second RS256 handoff JWT audience-bound to that one workspace domain and
redirects to the gateway's callback, which verifies it against the JWKS
published below. The broker's own session is a host-scoped HttpOnly cookie
carrying the SuperTokens access token, so subsequent shares authorize
silently while it is valid.

NOTE: this is a hand-rolled minimal login page reusing the connector's
existing SuperTokens proxy calls, not the SuperTokens prebuilt UI the
sharing-redesign spec sketched -- the connector deliberately does not mount
the SuperTokens middleware, and one small form is easier to keep correct.
"""

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlsplit

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from supertokens_python.async_to_sync_wrapper import sync as _supertokens_sync_run
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailpassword.syncio import sign_in as ep_sign_in
from supertokens_python.recipe.emailpassword.syncio import sign_up as ep_sign_up
from supertokens_python.recipe.emailverification.syncio import is_email_verified
from supertokens_python.recipe.emailverification.syncio import send_email_verification_email
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.thirdparty.provider import Provider
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput
from supertokens_python.recipe.thirdparty.providers.config_utils import find_and_create_provider_instance
from supertokens_python.syncio import get_user
from supertokens_python.types import RecipeUserId

import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.shares as shares_module
from imbue.remote_service_connector.auth_proxy import ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS
from imbue.remote_service_connector.auth_proxy import AUTH_TENANT_ID
from imbue.remote_service_connector.auth_proxy import HTML_SHARED_STYLES
from imbue.remote_service_connector.auth_proxy import build_session_tokens
from imbue.remote_service_connector.auth_proxy import complete_oauth_code_exchange
from imbue.remote_service_connector.auth_proxy import require_supertokens_configured
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.shares import require_share_env

logger = logging.getLogger(__name__)

router = APIRouter()

_BROKER_SSO_COOKIE_NAME = "imbue_sso_session"
# The SSO cookie carries only the SuperTokens ACCESS token (the refresh token
# is not persisted), so a session outlives the cookie's max-age only as long as
# that access token stays valid (~1h): _broker_session_user resolves it live
# and returns None once it expires, bouncing the visitor back to the login
# page. Set the cookie lifetime to match that reality rather than advertise a
# multi-day SSO the access token cannot honor; a refresh-token-backed longer
# SSO is a deliberate follow-up.
_BROKER_SSO_COOKIE_MAX_AGE_SECONDS = 3600
_BROKER_HANDOFF_TOKEN_TTL_SECONDS = 60
_BROKER_HANDOFF_ALGORITHM = "RS256"

_BROKER_LOGIN_PAGE_STYLES = (
    HTML_SHARED_STYLES + ".oauth-btn{display:block;width:100%;padding:12px;border:1px solid #e2e8f0;border-radius:8px;"
    "font-size:14px;font-weight:600;color:#0f172a;text-decoration:none;box-sizing:border-box;"
    "background:white}"
    ".oauth-btn:hover{background:#f8fafc}"
    ".divider{display:flex;align-items:center;gap:8px;color:#94a3b8;font-size:12px;margin:16px 0}"
    ".divider::before,.divider::after{content:'';flex:1;height:1px;background:#e2e8f0}"
)

_BROKER_LOGIN_PAGE_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Sign in - Imbue</title><style>" + _BROKER_LOGIN_PAGE_STYLES + "</style></head><body><div class='card'>"
    "<h1>Sign in to Imbue</h1>"
    "<p>Sign in to open the workspace that was shared with you.</p>"
    "{error_block}"
    "{oauth_block}"
    "<form method='post' action='/share/session'>"
    "<input type='hidden' name='next' value='{next_value}'>"
    "<p><input type='email' name='email' placeholder='Email' required autofocus "
    "style='width:100%;padding:8px'></p>"
    "<p><input type='password' name='password' placeholder='Password' required "
    "style='width:100%;padding:8px'></p>"
    "<p><button type='submit' name='mode' value='signin' style='padding:8px 16px'>Sign in</button> "
    "<button type='submit' name='mode' value='signup' style='padding:8px 16px'>Create account</button></p>"
    "</form></div></body></html>"
)

# The OAuth block rendered onto the login page when the broker's Google client
# is configured. ``{next_value}`` is substituted by the shared page render.
_BROKER_LOGIN_OAUTH_BLOCK = (
    "<a class='oauth-btn' href='/share/oauth/google/start?next={next_value}'>Continue with Google</a>"
    "<div class='divider'>or</div>"
)

_BROKER_VERIFY_EMAIL_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Verify your email - Imbue</title><style>"
    + HTML_SHARED_STYLES
    + "</style></head><body><div class='card'><h1>Check your inbox</h1>"
    "<p>We sent a verification link to your email address. Verify it, then reload the "
    "shared workspace link you were given.</p></div></body></html>"
)


def _broker_signing_key() -> rsa.RSAPrivateKey:
    """The broker's RS256 signing key from the sharing-<env> Modal secret."""
    key_pem = require_share_env("BROKER_JWT_SIGNING_KEY_PEM")
    try:
        private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except ValueError as exc:
        raise MissingShareConfigError("BROKER_JWT_SIGNING_KEY_PEM (not a valid PEM private key)") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise MissingShareConfigError("BROKER_JWT_SIGNING_KEY_PEM (must be an RSA private key)")
    return private_key


def _broker_key_id(public_key: rsa.RSAPublicKey) -> str:
    """A stable key id derived from the public key, published in the JWKS and stamped into token headers."""
    spki_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki_der).hexdigest()[:16]


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def build_broker_jwks(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    """The JWKS document workspace gateways verify handoff tokens against."""
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": _BROKER_HANDOFF_ALGORITHM,
                "kid": _broker_key_id(public_key),
                "n": _base64url_uint(numbers.n),
                "e": _base64url_uint(numbers.e),
            }
        ]
    }


def mint_share_handoff_token(
    signing_key: rsa.RSAPrivateKey,
    user_id: str,
    email: str,
    machine_domain: str,
    nonce: str,
) -> str:
    """Mint the 60-second, single-audience JWT the gateway's callback consumes."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "aud": machine_domain,
        "jti": secrets.token_urlsafe(16),
        "nonce": nonce,
        "iat": now,
        "exp": now + timedelta(seconds=_BROKER_HANDOFF_TOKEN_TTL_SECONDS),
    }
    return pyjwt.encode(
        payload,
        signing_key,
        algorithm=_BROKER_HANDOFF_ALGORITHM,
        headers={"kid": _broker_key_id(signing_key.public_key())},
    )


def _broker_session_user(request: Request) -> tuple[str, str] | None:
    """Resolve the broker SSO cookie to (user_id, verified email), or None when absent/invalid/unverified."""
    access_token = request.cookies.get(_BROKER_SSO_COOKIE_NAME, "")
    if not access_token or not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        return None
    try:
        user_id = auth_module.get_user_id_from_access_token(access_token)
    except HTTPException:
        return None
    email = auth_module.default_email_getter(user_id)
    if email is None:
        return None
    return user_id, email


def _sanitize_broker_path(candidate: str) -> str:
    """Clamp a redirect target to a same-host path (no scheme/host smuggling)."""
    if candidate.startswith("/") and not candidate.startswith("//") and not candidate.startswith("/\\"):
        return candidate
    return "/"


def _is_url_under_domain(url: str, machine_domain: str) -> bool:
    """Whether ``url`` is an https URL whose host is (a subdomain of) ``machine_domain``.

    Carrying a host in a redirect target is an open-redirect surface, so any
    host-bearing value the broker forwards a signed token toward -- the
    callback origin, the post-login ``next`` -- is checked against the share's
    own domain before use.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    domain = machine_domain.lower()
    return host == domain or host.endswith("." + domain)


def _is_origin_under_domain(origin: str, machine_domain: str) -> bool:
    """Whether ``origin`` is a bare https origin (no path/query) exactly one label under ``machine_domain``."""
    parsed = urlparse(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    suffix = "." + machine_domain.lower()
    if not host.endswith(suffix):
        return False
    # Exactly one non-empty label (the dedicated auth label) -- not the bare
    # domain (which no longer routes), not a deeper host (which the relay
    # refuses to route and the wildcard cert does not cover), and not a
    # foreign host. Mirrors ``decide_frps_new_proxy``'s per-label model.
    label = host[: -len(suffix)]
    return bool(label) and "." not in label


def _render_broker_login_page(next_path: str, error_message: str) -> HTMLResponse:
    error_block = f"<p style='color:#b91c1c'>{error_message}</p>" if error_message else ""
    oauth_block = _BROKER_LOGIN_OAUTH_BLOCK if get_broker_oauth_provider() is not None else ""
    # str.replace, not str.format: the shared styles block contains CSS braces.
    # The oauth block is substituted before {next_value} so its start link
    # picks up the same encoded next path as the password form.
    page = (
        _BROKER_LOGIN_PAGE_TEMPLATE.replace("{error_block}", error_block)
        .replace("{oauth_block}", oauth_block)
        .replace("{next_value}", quote(next_path, safe=""))
    )
    status_code = 401 if error_message else 200
    return HTMLResponse(content=page, status_code=status_code)


@router.get("/share/login")
def broker_login_page(request: Request) -> HTMLResponse:
    """The broker's minimal sign-in / sign-up page."""
    next_path = _sanitize_broker_path(request.query_params.get("next", "/"))
    return _render_broker_login_page(next_path, "")


def _is_cross_site_form_post(request: Request) -> bool:
    """True when a form POST's Origin header names a different site than the request's own host.

    Login CSRF needs no existing cookie (the attack SETS the victim's session
    to the attacker's account), so SameSite offers no protection here. Every
    current browser sends Origin on form POSTs; a present-but-foreign (or
    ``null``) Origin is a cross-site submission. An absent header (non-browser
    clients, tests) is allowed -- they are not CSRF victims.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return False
    return urlparse(origin).netloc.lower() != request.headers.get("host", "").lower()


@router.post("/share/session", response_model=None)
def broker_create_session(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    mode: str = Form(...),
    next_target: str = Form("/", alias="next"),
) -> HTMLResponse | RedirectResponse:
    """Sign in (or up) via the browser form, set the SSO cookie, and continue to the authorize URL."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        if _is_cross_site_form_post(request):
            raise HTTPException(status_code=403, detail="Cross-site login submissions are not accepted")
        next_path = _sanitize_broker_path(unquote(next_target))
        stripped_email = email.strip()
        if not stripped_email or not password:
            return _render_broker_login_page(next_path, "Email and password are required.")
        try:
            if mode == "signup":
                signup_result = ep_sign_up(tenant_id=AUTH_TENANT_ID, email=stripped_email, password=password)
                if isinstance(signup_result, EmailAlreadyExistsError):
                    return _render_broker_login_page(next_path, "An account with this email already exists.")
                if not isinstance(signup_result, EPSignUpOkResult):
                    return _render_broker_login_page(next_path, "Sign-up failed.")
                user = signup_result.user
            else:
                signin_result = ep_sign_in(tenant_id=AUTH_TENANT_ID, email=stripped_email, password=password)
                if isinstance(signin_result, WrongCredentialsError):
                    return _render_broker_login_page(next_path, "Incorrect email or password.")
                if not isinstance(signin_result, EPSignInOkResult):
                    return _render_broker_login_page(next_path, "Sign-in failed.")
                user = signin_result.user
            recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
            is_verified = is_email_verified(recipe_user_id=recipe_user_id, email=stripped_email)
            tokens = build_session_tokens(user.id)
            if not is_verified:
                send_email_verification_email(
                    tenant_id=AUTH_TENANT_ID,
                    user_id=user.id,
                    recipe_user_id=recipe_user_id,
                    email=stripped_email,
                )
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during broker login", exc_info=exc)
            return _render_broker_login_page(next_path, "Sign-in is temporarily unavailable.")
        if not is_verified:
            response: HTMLResponse | RedirectResponse = HTMLResponse(content=_BROKER_VERIFY_EMAIL_PAGE)
        else:
            response = RedirectResponse(url=next_path, status_code=303)
        _set_broker_sso_cookie(response, tokens.access_token)
        return response


@router.get("/share/authorize")
def broker_authorize(request: Request) -> RedirectResponse:
    """Authorize a visit to one shared workspace: require a session, then hand off a short-lived token.

    Redirect chain: gateway -> here (login if needed) -> gateway's
    ``/_auth/callback`` with the minted JWT. ``state`` is the gateway's
    nonce, echoed both as a query param and inside the token so the callback
    can bind the response to its own pending request.
    """
    with handle_endpoint_errors():
        machine_domain = request.query_params.get("machine_domain", "").lower()
        state = request.query_params.get("state", "")
        # ``next`` is the full origin URL the visitor was reaching, and
        # ``callback_origin`` is the workspace's dedicated auth origin (the one
        # label serving /_auth/*). The bare workspace domain no longer routes,
        # so the callback can no longer be delivered there. Both are validated
        # to be under this share's domain before we redirect a signed token to
        # either.
        next_url = request.query_params.get("next", "")
        callback_origin = request.query_params.get("callback_origin", "")
        if not machine_domain or not state:
            raise HTTPException(status_code=400, detail="machine_domain and state are required")
        if not _is_origin_under_domain(callback_origin, machine_domain):
            raise HTTPException(status_code=400, detail="callback_origin must be an origin under machine_domain")
        # ``next`` is optional; when present it must stay on this workspace, or
        # we drop it (the gateway falls back to a safe landing spot).
        safe_next = next_url if _is_url_under_domain(next_url, machine_domain) else ""
        session_user = _broker_session_user(request)
        if session_user is None:
            login_next = f"/share/authorize?{urlencode({'machine_domain': machine_domain, 'next': next_url, 'callback_origin': callback_origin, 'state': state})}"
            return RedirectResponse(url=f"/share/login?next={quote(login_next, safe='')}", status_code=302)
        user_id, session_email = session_user
        share = shares_module.get_share_store().find_active_share_by_workspace_domain(machine_domain)
        if share is None:
            raise HTTPException(status_code=404, detail="No active share for this domain")
        handoff_token = mint_share_handoff_token(
            signing_key=_broker_signing_key(),
            user_id=user_id,
            email=session_email,
            machine_domain=machine_domain,
            nonce=state,
        )
        callback_query = urlencode({"token": handoff_token, "state": state, "next": safe_next})
        return RedirectResponse(url=f"{callback_origin}/_auth/callback?{callback_query}", status_code=302)


# ---------------------------------------------------------------------------
# Broker browser OAuth (Continue with Google on the share login page)
#
# The password form cannot serve accounts created via Google OAuth (they have
# no password), so the login page offers the provider's own browser flow. The
# whole flow is web-only: the visitor's browser is bounced to the provider and
# back to ``/share/oauth/google/callback`` on this same host, which creates
# the SuperTokens session, sets the SSO cookie exactly as the password form
# does, and resumes the pending ``/share/authorize`` request.
# ---------------------------------------------------------------------------

_BROKER_OAUTH_NONCE_COOKIE_NAME = "imbue_oauth_nonce"
_BROKER_OAUTH_STATE_TTL_SECONDS = 600
_BROKER_OAUTH_STATE_PURPOSE = "broker_oauth"
_BROKER_OAUTH_CALLBACK_PATH = "/share/oauth/google/callback"


def get_broker_oauth_provider() -> Provider | None:
    """The Google provider for the broker's browser sign-in, or None when not configured.

    Built from the ``BROKER_GOOGLE_CLIENT_ID`` / ``BROKER_GOOGLE_CLIENT_SECRET``
    pair in the ``sharing-<env>`` secret: a Web-application OAuth client with
    each tier's ``/share/oauth/google/callback`` URL registered. Deliberately
    distinct from the ``GOOGLE_CLIENT_*`` pair in the supertokens secret --
    that one is a Desktop-type client serving the CLI's loopback flow, and
    Desktop clients cannot accept https redirect URIs.
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
    # ``find_and_create_provider_instance`` is async-only on the SuperTokens
    # SDK (see ``auth_oauth_authorize`` for why the sync wrapper is safe here);
    # for Google it assembles the provider from static endpoint config without
    # any network calls.
    return _supertokens_sync_run(find_and_create_provider_instance([provider_input], "google", None, {}))


def mint_broker_oauth_state(signing_key: rsa.RSAPrivateKey, nonce: str, next_path: str) -> str:
    """Mint the self-contained OAuth ``state``: the browser's nonce + where to resume after login.

    Self-contained (signed, never stored) because the connector runs as
    multiple concurrent containers: the provider's callback can land on a
    different container than the one that started the flow, so a server-side
    pending-flow registry would be invisible to it.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "purpose": _BROKER_OAUTH_STATE_PURPOSE,
        "nonce": nonce,
        "next": next_path,
        "iat": now,
        "exp": now + timedelta(seconds=_BROKER_OAUTH_STATE_TTL_SECONDS),
    }
    return pyjwt.encode(payload, signing_key, algorithm=_BROKER_HANDOFF_ALGORITHM)


def verify_broker_oauth_state(public_key: rsa.RSAPublicKey, state: str) -> tuple[str, str] | None:
    """Return ``(nonce, next_path)`` from a valid OAuth state token, or None when invalid/expired."""
    try:
        claims = pyjwt.decode(state, public_key, algorithms=[_BROKER_HANDOFF_ALGORITHM])
    except pyjwt.InvalidTokenError:
        return None
    if claims.get("purpose") != _BROKER_OAUTH_STATE_PURPOSE:
        return None
    nonce = claims.get("nonce")
    next_path = claims.get("next")
    if not isinstance(nonce, str) or not nonce or not isinstance(next_path, str):
        return None
    return nonce, _sanitize_broker_path(next_path)


def _broker_public_base_url(request: Request) -> str:
    """The broker's externally-visible base URL, for building the OAuth redirect URI.

    ``ACCOUNTS_BASE_URL`` (sharing secret) wins when set; otherwise the URL is
    derived from the request itself, which on a dev tier is the per-env
    connector URL -- the exact host whose callback path must be registered on
    the OAuth client.
    """
    configured = os.environ.get("ACCOUNTS_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.url.netloc}"


def _set_broker_sso_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        key=_BROKER_SSO_COOKIE_NAME,
        value=access_token,
        max_age=_BROKER_SSO_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.get("/share/oauth/google/start")
def broker_oauth_start(request: Request) -> RedirectResponse:
    """Begin the browser Google sign-in: stamp a nonce cookie and bounce to the provider."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_broker_oauth_provider()
        if provider is None:
            raise HTTPException(status_code=404, detail="Google sign-in is not configured on this server")
        next_path = _sanitize_broker_path(request.query_params.get("next", "/"))
        nonce = secrets.token_urlsafe(16)
        state = mint_broker_oauth_state(_broker_signing_key(), nonce, next_path)
        redirect_uri = _broker_public_base_url(request) + _BROKER_OAUTH_CALLBACK_PATH
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
        # flow. Login CSRF needs no existing session (the attack signs the
        # victim into the ATTACKER's account by forcing a callback that
        # carries the attacker's code), so the state signature alone is not
        # enough -- the callback must also present this cookie.
        response.set_cookie(
            key=_BROKER_OAUTH_NONCE_COOKIE_NAME,
            value=nonce,
            max_age=_BROKER_OAUTH_STATE_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/share/oauth",
        )
        return response


@router.get(_BROKER_OAUTH_CALLBACK_PATH, response_model=None)
def broker_oauth_callback(request: Request) -> HTMLResponse | RedirectResponse:
    """Finish the browser Google sign-in: verify state + nonce, create the session, resume the share flow."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_broker_oauth_provider()
        if provider is None:
            raise HTTPException(status_code=404, detail="Google sign-in is not configured on this server")
        state_param = request.query_params.get("state", "")
        verified_state = verify_broker_oauth_state(_broker_signing_key().public_key(), state_param)
        if verified_state is None:
            return _render_broker_login_page("/", "This sign-in link is invalid or has expired. Please try again.")
        nonce, next_path = verified_state
        if request.query_params.get("error"):
            # The provider reported a failure (most commonly the user
            # cancelling the consent screen).
            return _render_broker_login_page(next_path, "Google sign-in was cancelled. Please try again.")
        cookie_nonce = request.cookies.get(_BROKER_OAUTH_NONCE_COOKIE_NAME, "")
        if not cookie_nonce or not secrets.compare_digest(cookie_nonce, nonce):
            return _render_broker_login_page(
                next_path, "This sign-in attempt could not be verified. Please try again."
            )

        redirect_uri = _broker_public_base_url(request) + _BROKER_OAUTH_CALLBACK_PATH
        auth_result = complete_oauth_code_exchange(
            provider=provider,
            provider_id="google",
            callback_url=redirect_uri,
            query_params=dict(request.query_params),
        )
        if auth_result.status == ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS:
            return _render_broker_login_page(
                next_path,
                "An account with this email already signs in with a password. Use the email and password form below.",
            )
        if auth_result.status != "OK" or auth_result.tokens is None or auth_result.user is None:
            logger.warning("Broker OAuth callback failed: %s", auth_result.message)
            return _render_broker_login_page(next_path, "Google sign-in failed. Please try again.")

        if auth_result.needs_email_verification:
            _send_broker_oauth_verification_email(auth_result.user.user_id, auth_result.user.email)
            response: HTMLResponse | RedirectResponse = HTMLResponse(content=_BROKER_VERIFY_EMAIL_PAGE)
        else:
            # 303: the callback is a GET, but stay explicit that the follow-up
            # to /share/authorize must also be a GET.
            response = RedirectResponse(url=next_path, status_code=303)
        _set_broker_sso_cookie(response, auth_result.tokens.access_token)
        response.delete_cookie(key=_BROKER_OAUTH_NONCE_COOKIE_NAME, path="/share/oauth")
        return response


def _send_broker_oauth_verification_email(user_id: str, email: str) -> None:
    """Send a verification email for an OAuth account whose provider reported the email unverified.

    Mirrors the password form's inline send (the broker has no other surface
    to trigger it from). Best-effort: the verify page it accompanies already
    tells the visitor what to do, so a send failure is logged, not fatal.
    """
    user = get_user(user_id)
    if user is None:
        logger.warning("Could not send a verification email: user %s not found", user_id[:8])
        return
    recipe_user_id = next(
        (method.recipe_user_id for method in user.login_methods if method.email == email),
        RecipeUserId(user_id),
    )
    try:
        send_email_verification_email(
            tenant_id=AUTH_TENANT_ID,
            user_id=user_id,
            recipe_user_id=recipe_user_id,
            email=email,
        )
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.warning("Failed to send the broker OAuth verification email: %s", exc)


@router.get("/share/jwks.json")
def broker_jwks() -> JSONResponse:
    """The broker's public signing keys; workspace gateways verify handoff tokens against this."""
    with handle_endpoint_errors():
        jwks = build_broker_jwks(_broker_signing_key().public_key())
        return JSONResponse(content=jwks, headers={"Cache-Control": "public, max-age=300"})
