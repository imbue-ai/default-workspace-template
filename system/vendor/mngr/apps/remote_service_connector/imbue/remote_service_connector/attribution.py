"""Marketing attribution: the imbue_attribution cookie, signup stamping, and download events.

The marketing site's edge function sets a first-party ``imbue_attribution``
cookie on ``.imbue.com`` recording how the visitor arrived (first touch and
last non-direct touch, plus an anonymous visitor id). Because the hosted
accounts surface is served under the same registrable domain, that cookie is
presented at signup -- including when the desktop app opens the system browser
to sign in, which is what bridges attribution across the app download. The
cookie's exact contract lives in ``docs/attribution-cookie-contract.md``.

This module owns everything connector-side: tolerant cookie parsing, the
touch-merge rule (the signup page's own campaign params overwrite the
cookie's last touch, and synthesize the sole touch when the cookie is
absent), signup-context derivation, and the fail-open Neon writers. Capture
must never break signup or the download redirect, so every recorder swallows
storage errors with a warning.
"""

import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import Protocol
from urllib.parse import parse_qsl
from urllib.parse import unquote

import psycopg2
import psycopg2.extras
from pydantic import BaseModel
from pydantic import Field

from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db

logger = logging.getLogger(__name__)

ATTRIBUTION_COOKIE_NAME: Final[str] = "imbue_attribution"
ATTRIBUTION_COOKIE_SCHEMA_VERSION: Final[int] = 1

# Campaign query params copied into a touch; anything else only survives
# inside the touch's raw query string. `src` is the marketing site's
# per-button spot tag (e.g. src=modal-pr-review), carried on /download and
# signup links so per-spot funnel queries need no raw-query parsing.
CAMPAIGN_PARAM_ALLOWLIST: Final[tuple[str, ...]] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "src",
)

# Non-campaign-param fields a touch may carry: referrer, landing path, the
# raw query string, and the touch timestamp.
_TOUCH_EXTRA_FIELDS: Final[tuple[str, ...]] = ("ref", "path", "q", "at")

# Length caps: the cookie and every field in it are client-supplied, so
# bound what a crafted value can put in the database.
_MAX_COOKIE_CHARS: Final[int] = 8192
_MAX_FIELD_CHARS: Final[int] = 512
_MAX_RAW_QUERY_CHARS: Final[int] = 1024
_MAX_USER_AGENT_CHARS: Final[int] = 512
_MAX_VISITOR_ID_CHARS: Final[int] = 64

SIGNUP_CONTEXT_DESKTOP_APP: Final[str] = "desktop_app"
SIGNUP_CONTEXT_SHARE_VISIT: Final[str] = "share_visit"
SIGNUP_CONTEXT_WEB_CHROME: Final[str] = "web_chrome"
SIGNUP_CONTEXT_WEB: Final[str] = "web"


class AttributionCookie(BaseModel):
    """The sanitized contents of a parsed imbue_attribution cookie."""

    visitor_id: str | None = Field(default=None, description="Anonymous visitor id minted by the edge function")
    first_touch: dict[str, str] | None = Field(default=None, description="First touch, written once")
    last_touch: dict[str, str] | None = Field(default=None, description="Last non-direct touch")


class ResolvedAttribution(BaseModel):
    """The merged attribution to persist for one signup or download."""

    visitor_id: str | None = Field(default=None, description="Anonymous visitor id, when the cookie carried one")
    first_touch: dict[str, str] | None = Field(default=None, description="Merged first touch")
    last_touch: dict[str, str] | None = Field(default=None, description="Merged last touch")


def _clamped_string(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:max_chars]


def sanitize_touch(raw_touch: Any) -> dict[str, str] | None:
    """Filter one raw touch object down to the known string fields (None when nothing survives)."""
    if not isinstance(raw_touch, dict):
        return None
    touch: dict[str, str] = {}
    for field_name in CAMPAIGN_PARAM_ALLOWLIST + _TOUCH_EXTRA_FIELDS:
        max_chars = _MAX_RAW_QUERY_CHARS if field_name == "q" else _MAX_FIELD_CHARS
        clamped = _clamped_string(raw_touch.get(field_name), max_chars)
        if clamped is not None:
            touch[field_name] = clamped
    return touch or None


def parse_attribution_cookie(cookie_value: str | None) -> AttributionCookie | None:
    """Parse the marketing cookie; malformed or oversized values count as absent (and as a metric).

    The cookie is percent-encoded JSON (see the contract doc). It is pure
    client input, so parsing is tolerant: any structural surprise degrades to
    "no cookie" rather than failing the request that carried it. Rejections
    are expected internet junk -- counted via the attribution_cookie_rejected
    metric (so a rate change, e.g. a schema rollout gone wrong, is visible)
    and logged at info, not reported as errors.
    """
    if not cookie_value:
        return None
    if len(cookie_value) > _MAX_COOKIE_CHARS:
        emit_metric("attribution_cookie_rejected", 1, {"reason": "oversized"})
        logger.info("Ignoring an oversized imbue_attribution cookie (%d chars)", len(cookie_value))
        return None
    try:
        payload = json.loads(unquote(cookie_value))
    except ValueError as exc:
        emit_metric("attribution_cookie_rejected", 1, {"reason": "unparseable"})
        logger.info("Ignoring an unparseable imbue_attribution cookie: %s", exc)
        return None
    if not isinstance(payload, dict):
        emit_metric("attribution_cookie_rejected", 1, {"reason": "non_object"})
        logger.info("Ignoring a non-object imbue_attribution cookie")
        return None
    if payload.get("v") != ATTRIBUTION_COOKIE_SCHEMA_VERSION:
        emit_metric("attribution_cookie_rejected", 1, {"reason": "unknown_schema_version"})
        logger.info("Ignoring an imbue_attribution cookie with unknown schema version %r", payload.get("v"))
        return None
    return AttributionCookie(
        visitor_id=_clamped_string(payload.get("id"), _MAX_VISITOR_ID_CHARS),
        first_touch=sanitize_touch(payload.get("first")),
        last_touch=sanitize_touch(payload.get("last")),
    )


def synthesize_touch_from_page(page_query: str, page_path: str) -> dict[str, str] | None:
    """Build a touch from a page's own query string; None when it carries no campaign params."""
    raw_query = page_query[:_MAX_RAW_QUERY_CHARS]
    params_by_name = dict(parse_qsl(raw_query, keep_blank_values=False))
    touch = {
        name: params_by_name[name][:_MAX_FIELD_CHARS] for name in CAMPAIGN_PARAM_ALLOWLIST if params_by_name.get(name)
    }
    if not touch:
        return None
    touch["q"] = raw_query
    landing_path = _clamped_string(page_path, _MAX_FIELD_CHARS)
    if landing_path is not None:
        touch["path"] = landing_path
    # Z-suffixed millisecond precision, exactly JavaScript's toISOString
    # output, so every `at` string in the JSONB touches is uniform whether the
    # edge or the connector synthesized it.
    touch["at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return touch


def resolve_attribution(cookie: AttributionCookie | None, page_touch: dict[str, str] | None) -> ResolvedAttribution:
    """Merge the cookie's touches with the current page's own campaign params.

    The cookie supplies first/last; the page's params overwrite last (the
    page URL is by definition the latest touch) and synthesize the sole touch
    when the cookie is absent.
    """
    if cookie is None:
        return ResolvedAttribution(visitor_id=None, first_touch=page_touch, last_touch=page_touch)
    return ResolvedAttribution(
        visitor_id=cookie.visitor_id,
        first_touch=cookie.first_touch or page_touch,
        last_touch=page_touch or cookie.last_touch,
    )


def derive_signup_context(next_path: str) -> str:
    """Classify a signup by the login page's next= target (which surface sent the user here)."""
    if next_path.startswith("/accounts/authorize"):
        return SIGNUP_CONTEXT_DESKTOP_APP
    if next_path.startswith("/share/authorize"):
        return SIGNUP_CONTEXT_SHARE_VISIT
    if next_path == "/web" or next_path.startswith("/web/"):
        return SIGNUP_CONTEXT_WEB_CHROME
    return SIGNUP_CONTEXT_WEB


class AttributionStore(Protocol):
    """Persistence for attribution rows and download events."""

    def insert_account_attribution(
        self,
        *,
        user_id: str,
        email: str,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        signup_context: str,
        signup_method: str,
    ) -> None:
        """Write one account's attribution row; a pre-existing row wins (write-once)."""
        ...

    def insert_download_event(
        self,
        *,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        platform: str,
        user_agent: str | None,
    ) -> None:
        """Append one download event."""
        ...


class PostgresAttributionStore:
    """Neon-backed store; the account row is write-once via ON CONFLICT DO NOTHING."""

    def insert_account_attribution(
        self,
        *,
        user_id: str,
        email: str,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        signup_context: str,
        signup_method: str,
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO account_attribution "
                        "(user_id, email, visitor_id, first_touch, last_touch, signup_context, signup_method) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
                        (
                            user_id,
                            email,
                            visitor_id,
                            _jsonb_or_null(first_touch),
                            _jsonb_or_null(last_touch),
                            signup_context,
                            signup_method,
                        ),
                    )
        finally:
            conn.close()

    def insert_download_event(
        self,
        *,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        platform: str,
        user_agent: str | None,
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO download_events (visitor_id, first_touch, last_touch, platform, user_agent) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (visitor_id, _jsonb_or_null(first_touch), _jsonb_or_null(last_touch), platform, user_agent),
                    )
        finally:
            conn.close()


def _jsonb_or_null(touch: dict[str, str] | None) -> Any:
    return psycopg2.extras.Json(touch) if touch is not None else None


_attribution_store: AttributionStore | None = None


def get_attribution_store() -> AttributionStore:
    global _attribution_store
    if _attribution_store is None:
        _attribution_store = PostgresAttributionStore()
    return _attribution_store


def record_account_attribution(
    *,
    user_id: str,
    email: str,
    cookie_value: str | None,
    page_query: str,
    page_path: str,
    next_path: str,
    signup_method: str,
) -> None:
    """Stamp attribution for a just-created account.

    Fails open: a failed write logs a warning and the account creation
    proceeds unattributed. KeyError covers a missing DATABASE_URL (a
    connector without the Neon secret must still sign users up).
    """
    resolved = resolve_attribution(
        parse_attribution_cookie(cookie_value), synthesize_touch_from_page(page_query, page_path)
    )
    try:
        get_attribution_store().insert_account_attribution(
            user_id=user_id,
            email=email,
            visitor_id=resolved.visitor_id,
            first_touch=resolved.first_touch,
            last_touch=resolved.last_touch,
            signup_context=derive_signup_context(next_path),
            signup_method=signup_method,
        )
    except (psycopg2.Error, KeyError) as exc:
        logger.warning("Could not record account attribution for user %s", user_id, exc_info=exc)


def record_download_event(
    *,
    cookie_value: str | None,
    request_query: str,
    platform: str,
    user_agent: str | None,
) -> None:
    """Append a download event; the /download URL's own campaign params tag cookie-less downloads.

    Fails open like :func:`record_account_attribution`: the redirect must
    always happen, so a failed write only costs this one row.
    """
    resolved = resolve_attribution(
        parse_attribution_cookie(cookie_value), synthesize_touch_from_page(request_query, "/download")
    )
    clamped_user_agent = _clamped_string(user_agent, _MAX_USER_AGENT_CHARS)
    try:
        get_attribution_store().insert_download_event(
            visitor_id=resolved.visitor_id,
            first_touch=resolved.first_touch,
            last_touch=resolved.last_touch,
            platform=platform,
            user_agent=clamped_user_agent,
        )
    except (psycopg2.Error, KeyError) as exc:
        logger.warning("Could not record a download event for platform %s", platform, exc_info=exc)
