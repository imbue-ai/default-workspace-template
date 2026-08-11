"""SuperTokens auth proxy endpoints and SDK initialization.

These endpoints front the SuperTokens core so that clients (e.g. the minds
desktop client) never need to know the ``SUPERTOKENS_API_KEY``. All endpoints
here are unauthenticated: signing in is itself the authentication flow, and
the sensitive operations (core API key, OAuth client secrets) stay on this
server.
"""

import logging
import os
import threading
import time
from collections.abc import Mapping
from typing import Any
from typing import Final

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from supertokens_python import InputAppInfo
from supertokens_python import SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.async_to_sync_wrapper import sync as _supertokens_sync_run
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe import emailpassword as st_emailpassword_recipe
from supertokens_python.recipe import emailverification as st_emailverification_recipe
from supertokens_python.recipe import session as st_session_recipe
from supertokens_python.recipe import thirdparty as st_thirdparty_recipe
from supertokens_python.recipe.emailpassword.interfaces import APIInterface as EmailPasswordAPIInterface
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import PasswordPolicyViolationError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailpassword.syncio import consume_password_reset_token
from supertokens_python.recipe.emailpassword.syncio import send_reset_password_email
from supertokens_python.recipe.emailpassword.syncio import sign_in as ep_sign_in
from supertokens_python.recipe.emailpassword.syncio import sign_up as ep_sign_up
from supertokens_python.recipe.emailpassword.syncio import update_email_or_password
from supertokens_python.recipe.emailpassword.utils import default_email_validator
from supertokens_python.recipe.emailpassword.utils import default_password_validator
from supertokens_python.recipe.emailverification.interfaces import APIInterface as EmailVerificationAPIInterface
from supertokens_python.recipe.emailverification.interfaces import CreateEmailVerificationTokenOkResult
from supertokens_python.recipe.emailverification.syncio import create_email_verification_token
from supertokens_python.recipe.emailverification.syncio import is_email_verified
from supertokens_python.recipe.emailverification.syncio import send_email_verification_email
from supertokens_python.recipe.emailverification.syncio import verify_email_using_token
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import create_new_session_without_request_response
from supertokens_python.recipe.session.syncio import get_session_without_request_response
from supertokens_python.recipe.session.syncio import refresh_session_without_request_response
from supertokens_python.recipe.session.syncio import revoke_all_sessions_for_user
from supertokens_python.recipe.session.syncio import revoke_session
from supertokens_python.recipe.thirdparty.interfaces import APIInterface as ThirdPartyAPIInterface
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import Provider
from supertokens_python.recipe.thirdparty.provider import ProviderClientConfig
from supertokens_python.recipe.thirdparty.provider import ProviderConfig
from supertokens_python.recipe.thirdparty.provider import ProviderInput
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.recipe.thirdparty.syncio import get_provider
from supertokens_python.recipe.thirdparty.syncio import manually_create_or_update_user
from supertokens_python.syncio import get_user
from supertokens_python.syncio import list_users_by_account_info
from supertokens_python.types import LoginMethod
from supertokens_python.types import RecipeUserId
from supertokens_python.types.base import AccountInfoInput

import imbue.remote_service_connector.auth as auth_module
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.errors import MissingAuthWebsiteDomainError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()


AUTH_TENANT_ID = "public"

# The SuperTokens middleware's API base path (the SDK serves e.g. the browser
# session refresh at ``<base>/session/refresh``). Deliberately distinct from
# this module's deprecated hand-rolled ``/auth/*`` JSON endpoints so the
# SDK-served routes can never collide with them.
ACCOUNTS_AUTH_API_BASE_PATH: Final[str] = "/accounts/auth"


class EnsureAsgiRootPathMiddleware:
    """Guarantee ``scope["root_path"]`` exists before the SuperTokens middleware runs.

    Modal's ASGI shim omits ``root_path`` from the request scope (regular ASGI
    servers set it to ``""``), and the SuperTokens FastAPI middleware raises
    on EVERY request when the key is absent -- turning the whole deployed app
    into 500s, including ``/health/liveness``. Mounted by ``app.py`` outside
    (added after) the SuperTokens middleware so it normalizes the scope first.
    The ``async`` here is mandated by the ASGI protocol.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if isinstance(scope, dict) and scope.get("root_path") is None:
            scope = {**scope, "root_path": ""}
        await self.app(scope, receive, send)


class PartitionedCookieMiddleware:
    """Append ``; Partitioned`` to the SameSite=None session cookies on the way out.

    The SuperTokens SDK sets its browser-session cookies ``SameSite=None;
    Secure`` (so they ride inside the hosted chrome's cross-site workspace
    iframe) but cannot emit the CHIPS ``Partitioned`` attribute, which is what
    stops third-party-cookie blocking from withholding them while isolating
    them to the chrome's top-level partition. This rewrites the ``Set-Cookie``
    headers to add it. Scoped by attribute (SameSite=None + Secure), so it only
    touches cookies meant to cross sites; the ``async`` is mandated by ASGI.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not (isinstance(scope, dict) and scope.get("type") == "http"):
            await self.app(scope, receive, send)
            return

        async def _send_with_partitioned_cookies(message: Any) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                message = {**message, "headers": _add_partitioned_to_cross_site_cookies(message.get("headers", []))}
            await send(message)

        await self.app(scope, receive, _send_with_partitioned_cookies)


def _add_partitioned_to_cross_site_cookies(headers: Any) -> list[tuple[bytes, bytes]]:
    """Return the response headers with ``; Partitioned`` added to SameSite=None Set-Cookies."""
    rewritten: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        is_cross_site_cookie = (
            name.lower() == b"set-cookie"
            and b"samesite=none" in value.lower()
            and b"secure" in value.lower()
            and b"partitioned" not in value.lower()
        )
        rewritten.append((name, value + b"; Partitioned") if is_cross_site_cookie else (name, value))
    return rewritten


class SessionTokens(BaseModel):
    access_token: str = Field(description="SuperTokens JWT access token")
    refresh_token: str | None = Field(default=None, description="SuperTokens refresh token")


class AuthUser(BaseModel):
    user_id: str = Field(description="SuperTokens user ID (UUID v4)")
    email: str = Field(description="User email address")
    display_name: str | None = Field(default=None, description="Display name from OAuth provider, if any")


class SignUpRequest(BaseModel):
    email: str = Field(description="Email address to register")
    password: str = Field(description="Password for the new account")


class SignInRequest(BaseModel):
    email: str = Field(description="Email address")
    password: str = Field(description="Password")


class AuthResponse(BaseModel):
    status: str = Field(
        description=(
            "OK, WRONG_CREDENTIALS, EMAIL_ALREADY_EXISTS, ACCOUNT_EXISTS_WITH_OTHER_METHOD, FIELD_ERROR, or ERROR"
        )
    )
    message: str | None = Field(default=None, description="Human-readable message for non-OK statuses")
    user: AuthUser | None = Field(default=None, description="User info when status is OK")
    tokens: SessionTokens | None = Field(default=None, description="Session tokens when status is OK")
    needs_email_verification: bool = Field(
        default=False,
        description="True when the account's email has not yet been verified",
    )


class RefreshSessionRequest(BaseModel):
    refresh_token: str = Field(description="Existing refresh token")


class RefreshSessionResponse(BaseModel):
    status: str = Field(description="OK or ERROR")
    tokens: SessionTokens | None = Field(default=None, description="New tokens when status is OK")
    message: str | None = Field(default=None, description="Error detail if status is not OK")


class SendVerificationEmailRequest(BaseModel):
    email: str = Field(description="Email address to send verification to (must belong to the caller)")


class IsEmailVerifiedRequest(BaseModel):
    email: str = Field(description="Email address to check (must belong to the caller)")


class ForgotPasswordRequest(BaseModel):
    email: str = Field(description="Email address to send reset link to")


class ResetPasswordRequest(BaseModel):
    token: str = Field(description="Password reset token from email")
    new_password: str = Field(description="New password to set")


class OAuthAuthorizeRequest(BaseModel):
    provider_id: str = Field(description="Third-party provider ID (e.g. 'google', 'github')")
    callback_url: str = Field(description="Callback URL registered with the provider")


class OAuthAuthorizeResponse(BaseModel):
    status: str = Field(description="OK or ERROR")
    url: str | None = Field(default=None, description="URL to redirect the user to when status is OK")
    message: str | None = Field(default=None, description="Error detail if status is not OK")


class OAuthCallbackRequest(BaseModel):
    provider_id: str = Field(description="Third-party provider ID")
    callback_url: str = Field(description="Same callback URL used when starting the flow")
    query_params: dict[str, str] = Field(description="Query params the provider sent back to the callback URL")


class UserProviderInfo(BaseModel):
    user_id: str = Field(description="SuperTokens user ID")
    email: str | None = Field(default=None, description="Primary email if known")
    provider: str = Field(description="Login method: 'email' or a third-party provider ID")


def build_session_tokens(user_id: str) -> SessionTokens:
    """Create a new SuperTokens session for the given user and return the tokens."""
    session = create_new_session_without_request_response(
        tenant_id=AUTH_TENANT_ID,
        recipe_user_id=RecipeUserId(user_id),
    )
    raw = session.get_all_session_tokens_dangerously()
    return SessionTokens(
        access_token=raw["accessToken"],
        refresh_token=raw["refreshToken"] or None,
    )


# Minimum gap between verification emails for one user, applied to the explicit
# resend endpoint and the automatic send on signup/unverified-signin alike, so
# neither path can be used to spam a mailbox. The state is in-memory per
# container: a freshly-started or scaled-out connector may allow one extra
# send, which is acceptable for a spam-reduction measure.
_VERIFICATION_EMAIL_COOLDOWN_SECONDS: Final[float] = 60.0
_verification_email_sent_at_monotonic_by_user_id: dict[str, float] = {}
_verification_email_cooldown_lock = threading.Lock()


def send_verification_email_with_cooldown(user_id: str, recipe_user_id: RecipeUserId, email: str) -> bool:
    """Send a verification email unless one went out to this user moments ago.

    Returns True when an email was sent, False when the cooldown suppressed it.
    """
    now = time.monotonic()
    # The cooldown slot is reserved before sending so two concurrent requests
    # cannot both send; if the send then fails, the reservation is released so
    # a retry is not silently suppressed for a mail that never went out.
    with _verification_email_cooldown_lock:
        # Expired entries are dead weight (they read the same as absent ones);
        # dropping them here bounds the dict to users active within the window
        # instead of growing by one entry per user forever.
        expired_user_ids = [
            expired_user_id
            for expired_user_id, sent_at_monotonic in _verification_email_sent_at_monotonic_by_user_id.items()
            if (now - sent_at_monotonic) >= _VERIFICATION_EMAIL_COOLDOWN_SECONDS
        ]
        for expired_user_id in expired_user_ids:
            del _verification_email_sent_at_monotonic_by_user_id[expired_user_id]
        sent_at = _verification_email_sent_at_monotonic_by_user_id.get(user_id)
        if sent_at is not None and (now - sent_at) < _VERIFICATION_EMAIL_COOLDOWN_SECONDS:
            return False
        _verification_email_sent_at_monotonic_by_user_id[user_id] = now
    is_sent = False
    try:
        send_email_verification_email(
            tenant_id=AUTH_TENANT_ID,
            user_id=user_id,
            recipe_user_id=recipe_user_id,
            email=email,
        )
        is_sent = True
    finally:
        if not is_sent:
            with _verification_email_cooldown_lock:
                # Only release our own reservation; a concurrent request may
                # have re-reserved the slot after our failure.
                if _verification_email_sent_at_monotonic_by_user_id.get(user_id) == now:
                    del _verification_email_sent_at_monotonic_by_user_id[user_id]
    return is_sent


def _mark_email_verified(recipe_user_id: RecipeUserId, email: str) -> None:
    """Force-verify an email without the user clicking a link.

    Mints a verification token and immediately consumes it. A no-op when the
    email is already verified (the SDK then returns an already-verified result
    that carries no token to consume).
    """
    token_result = create_email_verification_token(
        tenant_id=AUTH_TENANT_ID,
        recipe_user_id=recipe_user_id,
        email=email,
    )
    if isinstance(token_result, CreateEmailVerificationTokenOkResult):
        verify_email_using_token(tenant_id=AUTH_TENANT_ID, token=token_result.token)


def require_supertokens_configured() -> None:
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        raise HTTPException(status_code=503, detail="SuperTokens not configured on the server")


# Status returned when a signup is refused because the email already has an
# account under a different login method (e.g. a Google account exists and the
# caller tries a password signup, or vice versa). Without this guard each
# method would happily create its own SuperTokens user for the same email --
# duplicate accounts with disjoint workspaces, keys, and entitlements.
ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS: Final[str] = "ACCOUNT_EXISTS_WITH_OTHER_METHOD"

_EMAIL_PASSWORD_LOGIN_METHOD_ID: Final[str] = "emailpassword"

_DISPLAY_NAME_BY_LOGIN_METHOD_ID: Final[dict[str, str]] = {
    _EMAIL_PASSWORD_LOGIN_METHOD_ID: "email and password",
    "google": "Google sign-in",
    "github": "GitHub sign-in",
}


def _login_method_id(login_method: LoginMethod) -> str:
    """The stable identifier of a login method: the third-party provider id, or the recipe id.

    Only the 'emailpassword' and 'thirdparty' recipes are enabled on this app, but the SDK
    type also allows 'passwordless'/'webauthn' methods; falling back to ``recipe_id`` keeps
    the guard accurate (rather than misreporting such a method as email-and-password) if
    another recipe is ever enabled.
    """
    if login_method.third_party is not None:
        return login_method.third_party.id
    return login_method.recipe_id


def _existing_login_method_ids_for_email(email: str) -> set[str]:
    """The login-method ids ('emailpassword' / third-party provider ids) already registered for ``email``."""
    email_lower = email.strip().lower()
    users = list_users_by_account_info(
        tenant_id=AUTH_TENANT_ID,
        account_info=AccountInfoInput(email=email_lower),
    )
    method_ids: set[str] = set()
    for user in users:
        for login_method in user.login_methods:
            if login_method.has_same_email_as(email_lower):
                method_ids.add(_login_method_id(login_method))
    return method_ids


def cross_method_signup_rejection(email: str, attempted_method_id: str) -> AuthResponse | None:
    """The one-account-per-email guard shared by password signup and the OAuth callback.

    Returns the rejection response when ``email`` is already registered under a
    *different* login method than ``attempted_method_id``, and None when the
    signup/sign-in may proceed. An email whose ``attempted_method_id`` account
    already exists is deliberately allowed through: for OAuth that is a normal
    returning sign-in, and for password signup the recipe's own
    EMAIL_ALREADY_EXISTS answer is the accurate one. Pre-existing cross-method
    duplicates therefore also keep both of their sign-ins working.
    """
    existing_method_ids = _existing_login_method_ids_for_email(email)
    if not existing_method_ids or attempted_method_id in existing_method_ids:
        return None
    existing_method_names = " or ".join(
        sorted(_DISPLAY_NAME_BY_LOGIN_METHOD_ID.get(method_id, method_id) for method_id in existing_method_ids)
    )
    logger.info(
        "Refused %s signup for %s: the email is already registered via %s",
        attempted_method_id,
        email,
        existing_method_names,
    )
    return AuthResponse(
        status=ACCOUNT_EXISTS_WITH_OTHER_METHOD_STATUS,
        message=(
            f"An account for this email already exists using {existing_method_names}. "
            "Sign in with that method instead."
        ),
    )


def signup_field_rejection(email: str, password: str) -> AuthResponse | None:
    """Server-side sign-up form validation, shared by the JSON and browser signup endpoints.

    The SDK only runs its email-format / password-policy form-field validators
    in its HTTP API layer, which this app disables
    (``disable_emailpassword_default_apis``); the recipe function ``sign_up``
    skips them entirely. Running the SDK's own default validators here keeps
    the enforced policy identical to what its API layer would have applied
    (>= 8 characters including a letter and a number; the email regex), so a
    caller bypassing the frontend cannot create an account with a malformed
    email or a trivially weak password.
    """
    email_error = _supertokens_sync_run(default_email_validator(email, AUTH_TENANT_ID))
    if email_error is not None:
        return AuthResponse(status="FIELD_ERROR", message=email_error)
    password_error = _supertokens_sync_run(default_password_validator(password, AUTH_TENANT_ID))
    if password_error is not None:
        return AuthResponse(status="FIELD_ERROR", message=password_error)
    return None


@router.post("/auth/signup", response_model=AuthResponse)
def auth_signup(body: SignUpRequest) -> AuthResponse:
    """Create a new email/password account and return a session + user info.

    Deprecated: the browser-based accounts surface (`/accounts/*`, consumed via
    ``mngr imbue_cloud auth login``) is the primary signup path. This JSON
    endpoint remains for released desktop clients and the headless CLI
    (``mngr imbue_cloud auth signup``); once every client ships the browser
    flow it will be restricted to dev/CI tiers. See the README's
    "Deprecated JSON auth endpoints" section.

    Email verification is non-blocking: no verification email is sent at
    signup (the first verification-gated action triggers a contextual send),
    and the account counts as signed in immediately.

    Any exception from the SuperTokens SDK (core unreachable, schema mismatch,
    etc.) is caught and surfaced as a structured ``AuthResponse(status="ERROR")``
    so the desktop client receives a stable JSON shape rather than a FastAPI
    default 500 body that its typed client cannot parse.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        email = body.email.strip()
        if not email or not body.password:
            return AuthResponse(status="FIELD_ERROR", message="Email and password are required")
        field_rejection = signup_field_rejection(email, body.password)
        if field_rejection is not None:
            return field_rejection

        try:
            # One-account-per-email: refuse a password signup when the email
            # already has an account under another login method (e.g. Google).
            rejection = cross_method_signup_rejection(email, _EMAIL_PASSWORD_LOGIN_METHOD_ID)
            if rejection is not None:
                return rejection

            result = ep_sign_up(tenant_id=AUTH_TENANT_ID, email=email, password=body.password)

            if isinstance(result, EmailAlreadyExistsError):
                return AuthResponse(status="EMAIL_ALREADY_EXISTS", message="An account with this email already exists")

            if not isinstance(result, EPSignUpOkResult):
                return AuthResponse(status="ERROR", message="Sign-up failed")

            user = result.user
            tokens = build_session_tokens(user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during signup", exc_info=exc)
            return AuthResponse(status="ERROR", message="Auth backend unavailable")
        # ``needs_email_verification`` is pinned False for wire compat:
        # released clients treat True as "blocked pending verification" (a
        # check-email page polling for a mail that no longer gets sent), while
        # verification is now non-blocking server-side. Live verification
        # state is served by /auth/email/is-verified.
        return AuthResponse(
            status="OK",
            user=AuthUser(user_id=user.id, email=email),
            tokens=tokens,
            needs_email_verification=False,
        )


@router.post("/auth/signin", response_model=AuthResponse)
def auth_signin(body: SignInRequest) -> AuthResponse:
    """Authenticate with email/password and return a session + user info.

    Deprecated: the browser-based accounts surface is the primary sign-in
    path; this JSON endpoint remains for released desktop clients and the
    headless CLI (``mngr imbue_cloud auth signin``). See the README's
    "Deprecated JSON auth endpoints" section.

    Email verification is non-blocking: an unverified signin succeeds and
    triggers no verification email (the first verification-gated action
    does, contextually).

    Any exception from the SuperTokens SDK is caught and returned as
    ``AuthResponse(status="ERROR")`` -- see the ``auth_signup`` docstring for
    the rationale.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        email = body.email.strip()
        if not email or not body.password:
            return AuthResponse(status="FIELD_ERROR", message="Email and password are required")

        try:
            result = ep_sign_in(tenant_id=AUTH_TENANT_ID, email=email, password=body.password)

            if isinstance(result, WrongCredentialsError):
                return AuthResponse(status="WRONG_CREDENTIALS", message="Incorrect email or password")

            if not isinstance(result, EPSignInOkResult):
                return AuthResponse(status="ERROR", message="Sign-in failed")

            user = result.user
            tokens = build_session_tokens(user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during signin", exc_info=exc)
            return AuthResponse(status="ERROR", message="Auth backend unavailable")
        # Pinned False for wire compat -- see the matching comment in
        # ``auth_signup``.
        return AuthResponse(
            status="OK",
            user=AuthUser(user_id=user.id, email=email),
            tokens=tokens,
            needs_email_verification=False,
        )


@router.post("/auth/session/refresh", response_model=RefreshSessionResponse)
def auth_refresh_session(body: RefreshSessionRequest) -> RefreshSessionResponse:
    """Exchange a refresh token for a fresh access/refresh token pair."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        try:
            new_session = refresh_session_without_request_response(refresh_token=body.refresh_token)
        except (SuperTokensSessionError, SuperTokensGeneralError, ValueError, TypeError) as exc:
            return RefreshSessionResponse(status="ERROR", message=str(exc))
        raw = new_session.get_all_session_tokens_dangerously()
        return RefreshSessionResponse(
            status="OK",
            tokens=SessionTokens(
                access_token=raw["accessToken"],
                refresh_token=raw["refreshToken"] or None,
            ),
        )


@router.post("/auth/session/revoke")
def auth_revoke_sessions(request: Request) -> dict[str, object]:
    """Revoke every SuperTokens session for the caller's user.

    Authentication: the caller must send their own SuperTokens access token as
    ``Authorization: Bearer <access_token>``. The user_id is derived from that
    session, not trusted from the request body -- otherwise an anonymous
    attacker could terminate arbitrary users' sessions just by guessing /
    learning their user_id UUID.

    Called by the minds client on sign-out so the access/refresh tokens stored
    on the user's machine become useless even if copied off-box. Idempotent --
    no-op when the caller has no other active sessions.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        user_id = auth_module.get_user_id_from_bearer_header(request)
        revoked = revoke_all_sessions_for_user(user_id=user_id)
        logger.info("Revoked %d sessions for user %s...", len(revoked), user_id[:8])
        return {"status": "OK", "revoked_count": len(revoked)}


@router.post("/auth/session/revoke-current")
def auth_revoke_current_session(request: Request) -> dict[str, object]:
    """Revoke only the session presented in the Authorization header.

    The device-scoped counterpart of ``/auth/session/revoke``: signing out of
    one desktop install must not kill the user's browser session or their
    other devices. The session handle is derived from the caller's own access
    token, so an anonymous caller cannot revoke anyone else's session.
    Idempotent from the caller's perspective -- an already-revoked token gets
    the same 401 every other stale token gets.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer credentials")
        try:
            session = get_session_without_request_response(
                access_token=auth_header[7:],
                anti_csrf_check=False,
                override_global_claim_validators=lambda *_args, **_kwargs: [],
            )
        except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
        if session is None:
            raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")
        session_handle = session.get_handle()
        is_revoked = revoke_session(session_handle)
        logger.info("Revoked current session for user %s...", session.get_user_id()[:8])
        return {"status": "OK", "revoked": bool(is_revoked)}


class TestSignupRequest(BaseModel):
    email: str = Field(description="Email address for the test account")
    password: str = Field(description="Password for the test account")
    verified: bool = Field(
        default=False,
        description="Whether to mark the email verified immediately (for tests that need verified accounts)",
    )


@router.post("/admin/test-signup", response_model=AuthResponse)
def admin_test_signup(request: Request, body: TestSignupRequest) -> AuthResponse:
    """Create an email/password account for automated tests (admin-key authenticated).

    Exists so the deployment tests can mint accounts on tiers where the open
    JSON signup is (or will be) restricted to the browser flow, and can cover
    both verified and unverified paths without a real mailbox. This is the
    ONLY path that may mark an email verified without the user clicking a
    link, and it is gated by the operator admin key -- the same trust root as
    the account admin API.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        require_supertokens_configured()
        email = body.email.strip()
        if not email or not body.password:
            return AuthResponse(status="FIELD_ERROR", message="Email and password are required")
        try:
            result = ep_sign_up(tenant_id=AUTH_TENANT_ID, email=email, password=body.password)
            if isinstance(result, EmailAlreadyExistsError):
                return AuthResponse(status="EMAIL_ALREADY_EXISTS", message="An account with this email already exists")
            if not isinstance(result, EPSignUpOkResult):
                return AuthResponse(status="ERROR", message="Sign-up failed")
            user = result.user
            recipe_user_id = user.login_methods[0].recipe_user_id if user.login_methods else RecipeUserId(user.id)
            if body.verified:
                _mark_email_verified(recipe_user_id=recipe_user_id, email=email)
            tokens = build_session_tokens(user.id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.error("SuperTokens SDK error during test signup", exc_info=exc)
            return AuthResponse(status="ERROR", message="Auth backend unavailable")
        return AuthResponse(
            status="OK",
            user=AuthUser(user_id=user.id, email=email),
            tokens=tokens,
            needs_email_verification=not body.verified,
        )


def recipe_user_id_for_callers_email(user_id: str, email: str) -> RecipeUserId:
    """Resolve the caller's login method for ``email``.

    Raises 403 when the email does not belong to the authenticated user --
    without this check, any valid session could probe or trigger emails for
    arbitrary addresses.
    """
    user = get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    email_lower = email.strip().lower()
    for login_method in user.login_methods:
        if login_method.has_same_email_as(email_lower):
            return login_method.recipe_user_id
    raise HTTPException(status_code=403, detail="Email does not belong to the authenticated user")


@router.post("/auth/email/send-verification")
def auth_send_verification_email(body: SendVerificationEmailRequest, request: Request) -> dict[str, object]:
    """(Re)send the caller's verification email.

    Authenticated by the caller's own access token (which deliberately does
    not require the email to be verified -- an unverified user is exactly who
    needs this endpoint). ``sent`` is False when the per-user cooldown
    suppressed the send.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        user_id = auth_module.get_user_id_from_bearer_header(request)
        recipe_user_id = recipe_user_id_for_callers_email(user_id, body.email)
        is_sent = send_verification_email_with_cooldown(
            user_id=user_id,
            recipe_user_id=recipe_user_id,
            email=body.email,
        )
        return {"status": "OK", "sent": is_sent}


@router.post("/auth/email/is-verified")
def auth_is_email_verified(body: IsEmailVerifiedRequest, request: Request) -> dict[str, bool]:
    """Return whether the caller's email is verified.

    Authenticated by the caller's own access token; like send-verification,
    an unverified session is accepted (it is the desktop client's
    verification poll that calls this).
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        user_id = auth_module.get_user_id_from_bearer_header(request)
        recipe_user_id = recipe_user_id_for_callers_email(user_id, body.email)
        verified = is_email_verified(recipe_user_id=recipe_user_id, email=body.email)
        return {"verified": verified}


@router.post("/auth/password/forgot")
def auth_forgot_password(body: ForgotPasswordRequest) -> dict[str, str]:
    """Send a password reset email for the given address (always succeeds).

    Swallows any backend error (SuperTokens core unreachable, schema mismatch,
    etc.) so that this endpoint's response is byte-identical whether or not an
    account exists for the given address -- a non-200 response for "unknown
    email" vs a 200 for "known email" would leak enumeration signal, and a
    500 on intermittent SuperTokens outages would violate the docstring's
    "always succeeds" contract.
    """
    with handle_endpoint_errors():
        require_supertokens_configured()
        email = body.email.strip()
        success = {"status": "OK", "message": "If an account exists, a reset email has been sent"}
        if not email:
            return success
        try:
            users = list_users_by_account_info(
                tenant_id=AUTH_TENANT_ID,
                account_info=AccountInfoInput(email=email),
            )
            if not users:
                return success
            user_id = users[0].id
            result = send_reset_password_email(tenant_id=AUTH_TENANT_ID, user_id=user_id, email=email)
            if result == "UNKNOWN_USER_ID_ERROR":
                logger.warning("Failed to send password reset email for user %s", user_id)
        except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
            logger.warning("Auth backend error during forgot-password; returning generic success: %s", exc)
        return success


@router.post("/auth/password/reset")
def auth_reset_password(body: ResetPasswordRequest) -> dict[str, str]:
    """Consume a password reset token and set a new password."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        if not body.token or not body.new_password:
            raise HTTPException(status_code=400, detail="Token and new password are required")

        consume_result = consume_password_reset_token(tenant_id=AUTH_TENANT_ID, token=body.token)
        if not isinstance(consume_result, ConsumePasswordResetTokenOkResult):
            return {"status": "INVALID_TOKEN", "message": "Invalid or expired reset token"}

        update_result = update_email_or_password(
            recipe_user_id=RecipeUserId(consume_result.user_id),
            password=body.new_password,
        )
        if isinstance(update_result, PasswordPolicyViolationError):
            return {"status": "FIELD_ERROR", "message": update_result.failure_reason}
        if not isinstance(update_result, UpdateEmailOrPasswordOkResult):
            raise HTTPException(status_code=500, detail="Failed to update password")
        return {"status": "OK", "message": "Password has been reset"}


@router.post("/auth/oauth/authorize", response_model=OAuthAuthorizeResponse)
def auth_oauth_authorize(body: OAuthAuthorizeRequest) -> OAuthAuthorizeResponse:
    """Return the URL to which the user should be redirected to begin OAuth."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_provider(tenant_id=AUTH_TENANT_ID, third_party_id=body.provider_id)
        if provider is None:
            return OAuthAuthorizeResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")
        # ``Provider.get_authorisation_redirect_url`` is async-only on the
        # SuperTokens SDK (the ``syncio`` module exposes a sync ``get_provider``
        # but the Provider object's methods are coroutines). We're inside a
        # sync def endpoint that FastAPI runs in a threadpool worker -- the
        # worker has no running event loop, so the SDK's own async-to-sync
        # wrapper can spin up a fresh loop safely. Same pattern SuperTokens'
        # own ``syncio`` helpers use internally.
        redirect = _supertokens_sync_run(
            provider.get_authorisation_redirect_url(
                redirect_uri_on_provider_dashboard=body.callback_url,
                user_context={},
            )
        )
        return OAuthAuthorizeResponse(status="OK", url=redirect.url_with_query_params)


def complete_oauth_code_exchange(
    provider: Provider,
    provider_id: str,
    callback_url: str,
    query_params: Mapping[str, str],
    *,
    is_bearer_session_minted: bool = True,
) -> AuthResponse:
    """Exchange a provider callback's params for a SuperTokens session.

    Shared by the desktop CLI's ``/auth/oauth/callback`` and the accounts
    surface's browser callback, so the one-account-per-email guard, the
    account create/update, and the error shapes cannot drift between the two
    flows. The browser callback passes ``is_bearer_session_minted=False``: it
    creates its own cookie session on the response, so minting bearer tokens
    here would leave an orphaned session in the core that nothing ever
    delivers or revokes.
    """
    try:
        # ``Provider.exchange_auth_code_for_oauth_tokens`` and
        # ``Provider.get_user_info`` are async-only on the SuperTokens SDK
        # (see ``auth_oauth_authorize`` for the rationale). FastAPI runs
        # these sync endpoints in threadpool workers with no running event
        # loop, so the SDK's async-to-sync wrapper is safe here.
        oauth_tokens = _supertokens_sync_run(
            provider.exchange_auth_code_for_oauth_tokens(
                redirect_uri_info=RedirectUriInfo(
                    redirect_uri_on_provider_dashboard=callback_url,
                    redirect_uri_query_params=dict(query_params),
                    pkce_code_verifier=None,
                ),
                user_context={},
            )
        )
        oauth_user = _supertokens_sync_run(provider.get_user_info(oauth_tokens=oauth_tokens, user_context={}))
    except Exception as exc:
        # Deliberately broad: this is the boundary around the SuperTokens
        # provider layer, whose realistic failures raise types no narrower
        # catch covers -- httpx transport errors (not OSError), pyjwt errors
        # from id-token verification, and plain ``Exception`` for a
        # consumed/expired authorization code ("third party user id is
        # missing"). Escaping here would turn a routine mid-sign-in hiccup
        # into a raw 500 in the user's browser instead of the structured
        # ERROR both callers render cleanly.
        logger.error("OAuth callback failed for %s", provider_id, exc_info=exc)
        return AuthResponse(status="ERROR", message=str(exc))

    if oauth_user.email is None or oauth_user.email.id is None:
        return AuthResponse(status="ERROR", message="No email provided by the OAuth provider")

    email = oauth_user.email.id

    # One-account-per-email: refuse the OAuth callback when the email
    # already has an account under another login method (e.g. a password
    # account). The provider dialog has already run by this point, but
    # nothing has been written to SuperTokens yet -- returning here leaves
    # no user, no session, and no partial state. A core outage during the
    # lookup is surfaced as the same structured ERROR the other /auth/*
    # endpoints return, so the typed desktop client gets a stable JSON
    # shape rather than a FastAPI 500 body.
    try:
        rejection = cross_method_signup_rejection(email, provider_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        logger.error("SuperTokens SDK error during OAuth callback", exc_info=exc)
        return AuthResponse(status="ERROR", message="Auth backend unavailable")
    if rejection is not None:
        return rejection

    result = manually_create_or_update_user(
        tenant_id=AUTH_TENANT_ID,
        third_party_id=provider_id,
        third_party_user_id=oauth_user.third_party_user_id,
        email=email,
        is_verified=oauth_user.email.is_verified,
    )
    if not isinstance(result, ManuallyCreateOrUpdateUserOkResult):
        return AuthResponse(status="ERROR", message="Could not create or update account")

    display_name: str | None = None
    if oauth_user.raw_user_info_from_provider and oauth_user.raw_user_info_from_provider.from_user_info_api:
        raw = oauth_user.raw_user_info_from_provider.from_user_info_api
        display_name = raw.get("name") or raw.get("login") or raw.get("displayName")

    tokens = build_session_tokens(result.user.id) if is_bearer_session_minted else None
    return AuthResponse(
        status="OK",
        user=AuthUser(user_id=result.user.id, email=email, display_name=display_name),
        tokens=tokens,
        needs_email_verification=not oauth_user.email.is_verified,
    )


@router.post("/auth/oauth/callback", response_model=AuthResponse)
def auth_oauth_callback(body: OAuthCallbackRequest) -> AuthResponse:
    """Exchange an OAuth callback's query params for a supertokens session."""
    with handle_endpoint_errors():
        require_supertokens_configured()
        provider = get_provider(tenant_id=AUTH_TENANT_ID, third_party_id=body.provider_id)
        if provider is None:
            return AuthResponse(status="ERROR", message=f"Unknown provider: {body.provider_id}")
        return complete_oauth_code_exchange(
            provider=provider,
            provider_id=body.provider_id,
            callback_url=body.callback_url,
            query_params=body.query_params,
        )


@router.get("/auth/users/{user_id}", response_model=UserProviderInfo)
def auth_get_user(user_id: str) -> UserProviderInfo:
    """Return basic info about a user, including the provider used to sign in."""
    require_supertokens_configured()
    user = get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    provider = "email"
    email: str | None = None
    for login_method in user.login_methods:
        if login_method.third_party is not None and provider == "email":
            provider = login_method.third_party.id
        if email is None and login_method.email:
            email = login_method.email
    return UserProviderInfo(user_id=user_id, email=email, provider=provider)


def _get_auth_website_domain() -> str:
    """Return the public URL used in outbound email links (verification, reset).

    Reads ``AUTH_WEBSITE_DOMAIN`` from the per-tier ``supertokens-<env>``
    Modal secret. The value is **required**: it is the URL embedded into
    password-reset and email-verification links, and it must match the
    workspace this app is actually deployed under. Raises
    :class:`RuntimeError` if the secret forgot to set it -- silently
    falling back to a hardcoded workspace would be wrong for every
    non-default tier.
    """
    value = os.environ.get("AUTH_WEBSITE_DOMAIN")
    if not value:
        raise MissingAuthWebsiteDomainError(
            "AUTH_WEBSITE_DOMAIN is not set. Populate it in the "
            f"`supertokens-{read_deploy_env()}-{read_deploy_id()}` Modal secret (the deploy script "
            "pushes it from the tier's Vault entry)."
        )
    return value


def _build_oauth_providers() -> list[ProviderInput]:
    """Build the OAuth provider list from env vars."""
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    github_client_id = os.environ.get("GITHUB_CLIENT_ID")
    github_client_secret = os.environ.get("GITHUB_CLIENT_SECRET")

    providers: list[ProviderInput] = []
    if google_client_id and google_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="google",
                    clients=[
                        ProviderClientConfig(
                            client_id=google_client_id,
                            client_secret=google_client_secret,
                        )
                    ],
                ),
            )
        )
    if github_client_id and github_client_secret:
        providers.append(
            ProviderInput(
                config=ProviderConfig(
                    third_party_id="github",
                    clients=[
                        ProviderClientConfig(
                            client_id=github_client_id,
                            client_secret=github_client_secret,
                        )
                    ],
                ),
            )
        )
    return providers


def disable_emailpassword_default_apis(original: EmailPasswordAPIInterface) -> EmailPasswordAPIInterface:
    """Turn off every HTTP API the emailpassword recipe would serve via the SDK middleware.

    The middleware is mounted only for the SDK's cookie/session machinery
    (the browser session refresh under ``ACCOUNTS_AUTH_API_BASE_PATH``); the
    recipe's own signup/signin/reset routes would bypass the hand-rolled
    endpoints' Turnstile gate, cross-method signup rejection, and
    verification-email cooldown. Only the HTTP routes are disabled -- the
    ``syncio`` recipe functions the hand-rolled endpoints call are unaffected.
    """
    original.disable_sign_up_post = True
    original.disable_sign_in_post = True
    original.disable_generate_password_reset_token_post = True
    original.disable_password_reset_post = True
    original.disable_email_exists_get = True
    return original


def disable_thirdparty_default_apis(original: ThirdPartyAPIInterface) -> ThirdPartyAPIInterface:
    """Turn off every HTTP API the thirdparty recipe would serve via the SDK middleware.

    Same reasoning as :func:`disable_emailpassword_default_apis`: the
    ``/signinup`` route would mint sessions without the hand-rolled flows'
    checks; the browser OAuth flow uses its own endpoints in accounts_web.
    """
    original.disable_sign_in_up_post = True
    original.disable_authorisation_url_get = True
    original.disable_apple_redirect_handler_post = True
    return original


def disable_emailverification_default_apis(original: EmailVerificationAPIInterface) -> EmailVerificationAPIInterface:
    """Turn off every HTTP API the emailverification recipe would serve via the SDK middleware.

    The recipe's generate-token route would send verification emails without
    the hand-rolled endpoints' per-user cooldown.
    """
    original.disable_email_verify_post = True
    original.disable_is_email_verified_get = True
    original.disable_generate_email_verify_token_post = True
    return original


def init_supertokens() -> None:
    """Initialize SuperTokens SDK with all recipes used by the minds auth flow.

    Includes emailpassword, thirdparty (OAuth), emailverification, and session.
    The SDK keeps its API key (``SUPERTOKENS_API_KEY``) server-side so clients
    never see it. OAuth client credentials (``GOOGLE_CLIENT_ID``/``SECRET``,
    ``GITHUB_CLIENT_ID``/``SECRET``) likewise live only on the server.
    """
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        return

    api_key = os.environ.get("SUPERTOKENS_API_KEY")
    website_domain = _get_auth_website_domain()
    providers = _build_oauth_providers()

    thirdparty_override = st_thirdparty_recipe.InputOverrideConfig(apis=disable_thirdparty_default_apis)
    thirdparty_recipe_init = (
        st_thirdparty_recipe.init(
            sign_in_and_up_feature=st_thirdparty_recipe.SignInAndUpFeature(providers=providers),
            override=thirdparty_override,
        )
        if providers
        else st_thirdparty_recipe.init(override=thirdparty_override)
    )

    supertokens_init(
        supertokens_config=SupertokensConfig(
            connection_uri=connection_uri,
            api_key=api_key,
        ),
        app_info=InputAppInfo(
            app_name="Minds",
            api_domain=website_domain,
            website_domain=website_domain,
            # The SDK middleware serves its routes (browser session refresh)
            # under api_base_path; website_base_path stays /auth so the links
            # in verification/reset emails keep hitting the hand-rolled pages.
            api_base_path=ACCOUNTS_AUTH_API_BASE_PATH,
            website_base_path="/auth",
        ),
        framework="fastapi",
        recipe_list=[
            # Cookie config for the hosted accounts surface's browser
            # sessions. ACCOUNTS_COOKIE_DOMAIN (sharing secret) widens the
            # cookie to the accounts SSO domain (e.g. ".imbue.com") on tiers
            # with a real accounts hostname; unset means host-only, which is
            # also the only thing that works on *.modal.run (a public suffix).
            st_session_recipe.init(
                cookie_domain=os.environ.get("ACCOUNTS_COOKIE_DOMAIN") or None,
                cookie_secure=True,
                # SameSite=None (paired with the Partitioned attribute appended
                # by PartitionedCookieMiddleware) so the browser session is
                # readable inside the hosted web chrome's cross-site workspace
                # iframe -- specifically on the broker's ``/share/authorize``
                # leg, which runs in that iframe and must recognize the owner.
                # A Lax cookie is withheld there, so the owner was bounced
                # through the "Choose an account" interstitial. CHIPS partitions
                # by top-level site (the chrome, on the connector origin), which
                # is also where it is set at ``/web`` login, so the partitions
                # match. CSRF is NOT weakened by this: every state-changing
                # route already refuses a cross-site Origin (see
                # ``_reject_cross_site_post``), and every session read here runs
                # with ``anti_csrf_check=False``, so the SDK's own anti-CSRF was
                # never the gate -- hence ``anti_csrf="NONE"`` (the SDK would
                # otherwise default to VIA_CUSTOM_HEADER when SameSite=None,
                # which our non-SDK frontends cannot satisfy, breaking refresh).
                cookie_same_site="none",
                anti_csrf="NONE",
                # Always transfer browser-session tokens as cookies. The SDK's
                # default resolves the method from the request's st-auth-mode
                # header (sent only by SuperTokens' own frontend SDK) and falls
                # back to header tokens, so the hand-rolled accounts pages
                # would get st-access-token response headers instead of the
                # cookie session this surface is built on.
                get_token_transfer_method=lambda _request, _is_create, _user_context: "cookie",
            ),
            st_emailpassword_recipe.init(
                override=st_emailpassword_recipe.InputOverrideConfig(apis=disable_emailpassword_default_apis)
            ),
            thirdparty_recipe_init,
            # OPTIONAL: verification is tracked (and required per-action via
            # ``require_verified_email``) but must not gate session validity --
            # REQUIRED would stamp a rejecting claim validator into every
            # session, which the connector deliberately does not enforce.
            st_emailverification_recipe.init(
                mode="OPTIONAL",
                override=st_emailverification_recipe.InputOverrideConfig(apis=disable_emailverification_default_apis),
            ),
        ],
        mode="asgi",
    )
    logger.info("SuperTokens SDK initialized (providers=%d)", len(providers))
