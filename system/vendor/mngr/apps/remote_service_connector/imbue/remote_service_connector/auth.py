"""Request authentication: SuperTokens sessions, paid lists, and the admin key."""

import hmac
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg2
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from supertokens_python.exceptions import GeneralError as SuperTokensGeneralError
from supertokens_python.recipe.session.exceptions import SuperTokensSessionError
from supertokens_python.recipe.session.syncio import get_session_without_request_response
from supertokens_python.syncio import get_user

from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.errors import EmailNotVerifiedError

logger = logging.getLogger(__name__)


class UserAuth(BaseModel):
    user_id_prefix: str
    # Email associated with the SuperTokens user, looked up live at auth time.
    # A verified login-method email is preferred; when the user has none, this
    # falls back to their (unverified) primary email so display and
    # account-keyed operations still work. ``None`` only when the SuperTokens
    # user record has no email at all or the lookup failed.
    email: str | None = None
    # Whether ``email`` belongs to a *verified* login method. Endpoints where
    # the email is an authorization identity (share visits, ally eligibility)
    # must check this via ``require_verified_email``; everything else accepts
    # unverified accounts.
    is_email_verified: bool = False
    # The full SuperTokens user id, when the authentication path resolved one
    # (the SuperTokens session paths always do). None only for callers that
    # construct a UserAuth from a bare prefix.
    user_id: str | None = None

    @property
    def verified_email(self) -> str | None:
        """The email only when it is verified -- the value safe to authorize by."""
        return self.email if self.is_email_verified else None


def stash_authenticated_user_for_access_log(request: Request, user_id: str) -> None:
    """Expose the resolved identity to the outermost access-log middleware.

    ``request.state`` is backed by ASGI scope state, which the shared
    ``RequestLoggingMiddleware`` (outermost) reads back after the response --
    so authenticated requests carry their full user id on the JSON access-log
    line while unauthenticated ones omit the field.
    """
    request.state.authenticated_user_id = user_id


def authenticate_request(request: Request, check_database: bool = False) -> UserAuth:
    """Authenticate a request via its SuperTokens JWT Bearer token.

    Raises ``HTTPException(401)`` when the Bearer credentials are missing or
    the token is not a valid SuperTokens session. Email verification is NOT
    required here -- endpoints that authorize by email ownership must
    additionally call :func:`require_verified_email`.

    ``check_database=True`` verifies the session against the SuperTokens core
    rather than by signature alone, so a revoked-but-unexpired access token is
    rejected immediately. State-modifying routes pass it (via
    ``resolve_web_user_identity``'s method inference); read routes keep the
    cheap stateless validation and let revoked tokens drain out over their
    remaining lifetime.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    user = _authenticate_supertokens(auth_header[7:], check_database=check_database)
    if user.user_id is not None:
        stash_authenticated_user_for_access_log(request, user.user_id)
    return user


def require_verified_email(user: UserAuth) -> None:
    """Refuse the request unless the caller's email is verified.

    Applied only where the email is an authorization identity (satisfying a
    share grant, ally-plan eligibility). Raises the structured
    ``email_not_verified`` 403 so clients can prompt verification contextually.
    """
    if not user.is_email_verified:
        raise EmailNotVerifiedError(email=user.email, is_verification_email_sent=None, message=None)


_USER_ID_PREFIX_LENGTH = 16


def derive_user_id_prefix(user_id: str) -> str:
    """The 16-hex prefix of a SuperTokens user id, used to namespace leases/buckets.

    Also the ``account_entitlements.user_id_prefix`` lookup key, so every
    caller must derive it identically -- always go through this helper.
    """
    return user_id.replace("-", "")[:_USER_ID_PREFIX_LENGTH]


def resolve_account_email(
    user_id: str,
    # Resolved at call time (not bound as a default) so tests that patch the
    # module-level ``get_user`` take effect.
    user_getter: Callable[[str], Any] | None = None,
) -> tuple[str | None, bool]:
    """Return ``(email, is_verified)`` for the given SuperTokens user_id.

    A SuperTokens user may have several login methods (email/password, OAuth
    providers) with independent ``verified`` flags. A verified login-method
    email is preferred; when none is verified, the first email is returned
    with ``is_verified=False`` so callers can still display/key the account.
    ``(None, False)`` means the user has no email at all (or does not exist).

    Only the SuperTokens SDK's typed errors (``SuperTokensSessionError``,
    ``SuperTokensGeneralError``) are caught and turned into ``(None, False)``
    (with a warning log); any other exception (e.g. transport-level network
    errors that escape the SDK) is allowed to propagate, so that truly
    unexpected failures surface loudly rather than silently denying access.

    ``user_getter`` is exposed for tests so they can drive each branch
    (``None`` user, missing emails, SDK exception) without monkeypatching the
    SuperTokens SDK; production callers should rely on the default.
    """
    resolved_getter = user_getter if user_getter is not None else get_user
    try:
        user = resolved_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        emit_metric("supertokens_user_fetch_failed", 1, {"caller": "resolve_account_email"})
        logger.warning("Failed to fetch SuperTokens user %s", user_id[:8], exc_info=exc)
        return None, False
    if user is None:
        return None, False
    fallback_email: str | None = None
    for login_method in user.login_methods:
        if login_method.email and login_method.verified:
            return login_method.email, True
        if login_method.email and fallback_email is None:
            fallback_email = login_method.email
    return fallback_email, False


def get_backfill_email(
    user_id: str,
    # Resolved at call time (not bound as a default) so tests that patch the
    # module-level ``get_user`` take effect.
    user_getter: Callable[[str], Any] | None = None,
) -> str | None:
    """Return the email to feed an entitlements-row backfill for ``user_id``.

    The backfill's paid-list check may only consume verified emails, but a
    user who merely lacks verification must still get a (free) row -- so
    an existing-but-unverified user maps to ``""`` (create the row, skip the
    paid check) while a missing/unresolvable user maps to ``None`` (do not
    create anything).
    """
    resolved_getter = user_getter if user_getter is not None else get_user
    try:
        user = resolved_getter(user_id)
    except (SuperTokensSessionError, SuperTokensGeneralError) as exc:
        emit_metric("supertokens_user_fetch_failed", 1, {"caller": "get_backfill_email"})
        logger.warning("Failed to fetch SuperTokens user %s", user_id[:8], exc_info=exc)
        return None
    if user is None:
        return None
    for login_method in user.login_methods:
        if login_method.email and login_method.verified:
            return login_method.email
    return ""


def _authenticate_supertokens(
    token: str,
    session_getter: Callable[..., Any] = get_session_without_request_response,
    email_resolver: Callable[[str], tuple[str | None, bool]] = resolve_account_email,
    check_database: bool = False,
) -> UserAuth:
    """Validate a SuperTokens JWT access token. Returns UserAuth carrying the derived user-id prefix and email."""
    connection_uri = os.environ.get("SUPERTOKENS_CONNECTION_URI")
    if not connection_uri:
        raise HTTPException(status_code=401, detail="SuperTokens not configured")

    try:
        # Pass ``override_global_claim_validators=lambda *_: []`` so the
        # session getter does NOT auto-reject based on the token's
        # email-verification claim: verification is non-blocking here, and the
        # endpoints that do require it check the live core state via
        # ``require_verified_email`` instead of the claim baked into the token
        # at login time.
        session = session_getter(
            access_token=token,
            anti_csrf_check=False,
            check_database=check_database,
            override_global_claim_validators=lambda *_args, **_kwargs: [],
        )
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")

    user_id = session.get_user_id()
    user_id_prefix = derive_user_id_prefix(user_id)

    # Resolve the email (and its verification state) live from the core rather
    # than trusting the token's cached email-verification claim -- the claim is
    # baked in at login and cannot reflect a verification that happened
    # afterwards. An account with no email at all is rejected: every login
    # method we enable carries one, so its absence means a broken account
    # record rather than a legitimate caller.
    email, is_email_verified = email_resolver(user_id)
    if email is None:
        raise HTTPException(status_code=401, detail="Account has no email address")

    return UserAuth(user_id_prefix=user_id_prefix, email=email, is_email_verified=is_email_verified, user_id=user_id)


def get_user_id_from_bearer_header(request: Request) -> str:
    """Extract and validate the caller's access token from the Authorization header.

    Returns the full SuperTokens user_id. Like
    :func:`get_user_id_from_access_token`, this does NOT require the email to
    be verified -- it exists for the endpoints an unverified user must be able
    to reach (checking verification status, resending the verification email,
    revoking their own session).
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    user_id = get_user_id_from_access_token(auth_header[7:])
    stash_authenticated_user_for_access_log(request, user_id)
    return user_id


def get_user_id_from_access_token(token: str) -> str:
    """Validate a SuperTokens JWT and return the full user_id (not just the prefix).

    Raises ``HTTPException(401)`` on any validation failure. Used by auth-proxy
    endpoints that need the full user_id to drive an API call (e.g. revoke).

    Does NOT enforce email-verification at this layer -- callers like
    ``/auth/session/revoke`` legitimately need to work for unverified
    users (signing out a session you never finished verifying should
    still succeed). The endpoints that DO want email-verified callers
    authenticate via :func:`authenticate_request` and then call
    :func:`require_verified_email` on the result.
    """
    if not os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        raise HTTPException(status_code=401, detail="SuperTokens not configured")
    try:
        session = get_session_without_request_response(
            access_token=token,
            anti_csrf_check=False,
            override_global_claim_validators=lambda *_args, **_kwargs: [],
        )
    except (ValueError, TypeError, SuperTokensSessionError, SuperTokensGeneralError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SuperTokens session")
    return session.get_user_id()


# Env var holding the cache TTL (in seconds) for paid-status lookups. The
# paid gate consults two Neon tables (``paid_emails`` / ``paid_domains``)
# on every gated request; this in-memory cache bounds how often that DB
# round-trip happens per container. Set to ``0`` to disable caching
# entirely (every gated request hits the DB) -- useful in tests. Unset
# falls back to ``_DEFAULT_PAID_LIST_CACHE_TTL_SECONDS``. Each Modal
# container caches independently, so a CRUD change to the lists takes up
# to the TTL to be reflected everywhere.
_PAID_LIST_CACHE_TTL_ENV = "MINDS_PAID_LIST_CACHE_TTL_SECONDS"
_DEFAULT_PAID_LIST_CACHE_TTL_SECONDS = 60.0

# Process-local cache mapping a lowercased email -> (expiry_monotonic, is_paid).
# Guarded by a lock since uvicorn serves requests from a thread pool.
_paid_status_cache: dict[str, tuple[float, bool]] = {}
_paid_status_cache_lock = threading.Lock()


def clear_paid_status_cache() -> None:
    """Drop every cached paid-status entry (called after a CRUD write, and in tests)."""
    with _paid_status_cache_lock:
        _paid_status_cache.clear()


def _paid_list_cache_ttl_seconds() -> float:
    """Resolve the paid-status cache TTL from the environment.

    Falls back to the default on an unset/empty value and on an
    unparseable one (logging a warning in the latter case) so a typo'd
    Modal secret degrades to "cache normally" rather than crashing the
    gate.
    """
    raw = os.environ.get(_PAID_LIST_CACHE_TTL_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "Invalid %s=%r; falling back to %.0fs",
            _PAID_LIST_CACHE_TTL_ENV,
            raw,
            _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS,
        )
        return _DEFAULT_PAID_LIST_CACHE_TTL_SECONDS


def _email_domain(email: str) -> str:
    """Return the lowercased domain (part after the last ``@``) of an email, or ``""``."""
    return email.strip().lower().rpartition("@")[2]


def is_email_paid_in_db(
    email: str,
    connection_factory: Callable[[], Any] | None = None,
) -> bool:
    """Return whether ``email`` is paid per the ``paid_emails`` / ``paid_domains`` tables.

    Paid when either an exact (lowercased) full-email match exists in
    ``paid_emails`` with ``is_paid = true``, OR the email's exact domain
    matches a ``paid_domains`` row with ``is_paid = true``. ``connection_factory``
    is injected so unit tests can supply an in-memory backend; it defaults
    to ``db.get_pool_db_connection`` (resolved lazily so installed fakes on
    the ``db`` module take effect).

    Raises ``psycopg2.Error`` on any database failure; gate-style callers
    (:func:`require_ally_eligible`) convert that into a fail-closed 403.
    """
    factory = connection_factory if connection_factory is not None else db.get_pool_db_connection
    email_lower = email.strip().lower()
    domain = _email_domain(email_lower)
    conn = factory()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM paid_emails WHERE email = %s AND is_paid = TRUE", (email_lower,))
            if cur.fetchone() is not None:
                return True
            if domain:
                cur.execute("SELECT 1 FROM paid_domains WHERE domain = %s AND is_paid = TRUE", (domain,))
                if cur.fetchone() is not None:
                    return True
        return False
    finally:
        conn.close()


def is_email_paid(
    email: str,
    db_lookup: Callable[[str], bool] = is_email_paid_in_db,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Cached wrapper around :func:`is_email_paid_in_db`.

    Honors the ``MINDS_PAID_LIST_CACHE_TTL_SECONDS`` TTL: a non-positive
    TTL bypasses the cache entirely, otherwise both positive and negative
    results are cached for the TTL window. ``db_lookup`` / ``monotonic``
    are injected for tests.
    """
    email_lower = email.strip().lower()
    ttl_seconds = _paid_list_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return db_lookup(email_lower)
    now = monotonic()
    with _paid_status_cache_lock:
        cached = _paid_status_cache.get(email_lower)
        if cached is not None and cached[0] > now:
            return cached[1]
    is_paid = db_lookup(email_lower)
    with _paid_status_cache_lock:
        _paid_status_cache[email_lower] = (now + ttl_seconds, is_paid)
    return is_paid


def require_ally_eligible(
    email: str | None,
    paid_checker: Callable[[str], bool] = is_email_paid,
) -> None:
    """Gate ally-plan selection on the caller's email appearing in the paid lists.

    Raises ``HTTPException(403)`` when the caller has no verified email, when
    their email is not in the ``paid_emails`` / ``paid_domains`` tables, or
    when the database lookup fails (fail closed). This is the only remaining
    consumer of the paid lists as a *gate* -- resource access itself is now
    governed by per-account entitlements. ``paid_checker`` is injected for
    tests; production callers use the cached, table-backed default.
    """
    if not email:
        raise HTTPException(
            status_code=403,
            detail="Account email unavailable; cannot check ally-plan eligibility",
        )
    try:
        is_paid = paid_checker(email)
    except psycopg2.Error as exc:
        logger.warning("Paid-status lookup failed for %s", email, exc_info=exc)
        raise HTTPException(
            status_code=403,
            detail="Could not verify ally-plan eligibility (database error); please try again",
        ) from exc
    if not is_paid:
        raise HTTPException(
            status_code=403,
            detail="The 'ally' plan requires partner access (a paid-listed email)",
        )


# Env var holding the single fixed API key that authenticates the operator
# admin endpoints: the paid-list CRUD (``/paid/*``), the account admin API
# (``/admin/accounts/*``), and the on-demand sweeps (``/admin/sweep/*``).
# Distinct from the SuperTokens auth used by every other route: those
# routes reject this key, and the admin routes reject SuperTokens JWTs.
# Folded into the ``supertokens-<env>`` Modal secret (see
# .minds/template/supertokens.sh).
_ADMIN_KEY_ENV = "MINDS_ADMIN_KEY"

# Deprecated spelling of ``_ADMIN_KEY_ENV`` from when the key only guarded the
# paid-list CRUD. Still accepted (with a warning) while existing Vault entries
# and operator environments migrate to ``MINDS_ADMIN_KEY``.
_LEGACY_ADMIN_KEY_ENV = "MINDS_PAID_ADMIN_KEY"


def _configured_admin_key() -> str:
    """The configured admin API key, preferring ``MINDS_ADMIN_KEY``.

    Falls back to the deprecated ``MINDS_PAID_ADMIN_KEY`` spelling (warning
    once per lookup) so deployments migrate without a flag day. Returns ""
    when neither is set (the admin API is disabled).
    """
    expected = os.environ.get(_ADMIN_KEY_ENV, "")
    if expected:
        return expected
    legacy = os.environ.get(_LEGACY_ADMIN_KEY_ENV, "")
    if legacy:
        logger.warning(
            "Admin API key found under deprecated env var %s; rename it to %s",
            _LEGACY_ADMIN_KEY_ENV,
            _ADMIN_KEY_ENV,
        )
    return legacy


def require_admin_key(request: Request) -> None:
    """Authenticate an operator admin request against the fixed admin API key.

    Expects ``Authorization: Bearer <MINDS_ADMIN_KEY>`` and compares
    in constant time. Raises ``HTTPException(403)`` when the server has no
    key configured (the admin API is disabled), and ``HTTPException(401)``
    when credentials are missing or wrong.
    """
    expected = _configured_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="Admin API is not enabled on this server")
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    provided = auth_header[len("bearer ") :]
    # Compare over UTF-8 bytes: hmac.compare_digest raises TypeError on str
    # operands containing non-ASCII characters, and HTTP header values can
    # legitimately carry non-ASCII bytes. Encoding keeps the comparison both
    # total (a malformed key cleanly yields 401, not a 500) and constant-time.
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin API key")
