"""The workspace session: one HS256 JWT, delivered as two cookies scoped ``Domain=<workspace-domain>``.

Set once by the login callback, verified (and its identity re-checked against the
grants) on every request. 24 hours, fixed. The signing secret is generated in
the workspace and never leaves it, so a relay or connector compromise cannot
mint sessions.

The payload is the requester's whole identity record -- ``user_id``, ``email``,
``display_name``, ``avatar_url`` -- plus the ``owner`` flag, because the cookie
is the only per-request source the gateway has: nothing polls the connector
for profile data, and the record only changes when the user re-runs the
handoff (sign-in, or the ``/_auth/refresh`` route). A cookie minted before the
record carried a user id is treated as no session at all.

The same value is set twice, under two names, because no single cookie works
in both places a visitor reaches a shared workspace from:

- ``imbue_machine_session`` is a ``SameSite=Lax; Secure`` cookie: the one a
  browser sends when the workspace is the top-level site (a share link opened
  directly, including on Safari). Lax covers that whole flow -- the broker's
  post-login bounce to the callback is a top-level GET navigation, which may
  set a Lax cookie, and everything the page then loads is same-site -- while
  keeping the cookie off subresource requests and fetches that a foreign site
  aims at a workspace origin. The Origin policy exempts plain GETs, so this
  cookie is what stops a foreign page from making the browser attach the
  owner's session to such a GET.
- ``imbue_machine_session_partitioned`` is ``SameSite=None; Secure;
  Partitioned`` (CHIPS) so the hosted minds chrome can embed the workspace in
  a cross-site iframe: browsers only send a third-party cookie from an iframe
  when it is partitioned by the embedding site. A Lax cookie is not even
  stored from inside that iframe, so the two copies never overlap.

Safari 18.5 through 26.1 rejects a cookie carrying ``Partitioned`` outright
instead of storing it unpartitioned (WebKit bug 292975), so a lone
partitioned cookie leaves an iOS visitor with no session at all. Verification
accepts whichever copy the browser sends.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
from flask import Response

from share_gateway.identity import RequesterIdentity

SESSION_COOKIE_NAME = "imbue_machine_session"
PARTITIONED_SESSION_COOKIE_NAME = "imbue_machine_session_partitioned"
SESSION_LIFETIME_SECONDS = 24 * 3600

_SESSION_COOKIE_NAMES = (SESSION_COOKIE_NAME, PARTITIONED_SESSION_COOKIE_NAME)
_SESSION_ALGORITHM = "HS256"


def mint_session_cookie_value(signing_secret: str, identity: RequesterIdentity, workspace_domain: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "user_id": identity.user_id,
        "email": identity.email,
        "owner": identity.is_owner,
        "aud": workspace_domain,
        "iat": now,
        "exp": now + timedelta(seconds=SESSION_LIFETIME_SECONDS),
    }
    if identity.display_name:
        payload["display_name"] = identity.display_name
    if identity.avatar_url:
        payload["avatar_url"] = identity.avatar_url
    return jwt.encode(payload, signing_secret, algorithm=_SESSION_ALGORITHM)


def _optional_text_claim(claims: dict[str, object], name: str) -> str | None:
    value = claims.get(name)
    return value if isinstance(value, str) and value else None


def verify_session_cookie_value(
    signing_secret: str, cookie_value: str, workspace_domain: str
) -> "RequesterIdentity | None":
    """Return the session identity, or None when the cookie is missing, expired, forged, or pre-dates user ids."""
    if not cookie_value:
        return None
    try:
        claims = jwt.decode(
            cookie_value,
            signing_secret,
            algorithms=[_SESSION_ALGORITHM],
            audience=workspace_domain,
        )
    except jwt.PyJWTError:
        return None
    user_id = _optional_text_claim(claims, "user_id")
    email = _optional_text_claim(claims, "email")
    if user_id is None or email is None:
        return None
    return RequesterIdentity(
        user_id=user_id,
        email=email,
        is_owner=bool(claims.get("owner", False)),
        display_name=_optional_text_claim(claims, "display_name"),
        avatar_url=_optional_text_claim(claims, "avatar_url"),
    )


def verify_session_from_cookies(
    signing_secret: str, cookies: Mapping[str, str], workspace_domain: str
) -> "RequesterIdentity | None":
    """The identity of the first session cookie copy that verifies, or None when neither does."""
    for cookie_name in _SESSION_COOKIE_NAMES:
        identity = verify_session_cookie_value(signing_secret, cookies.get(cookie_name, ""), workspace_domain)
        if identity is not None:
            return identity
    return None


def set_session_cookie(response: Response, cookie_value: str, workspace_domain: str) -> None:
    """Attach both copies of the workspace session cookie (see the module docstring)."""
    for cookie_name in _SESSION_COOKIE_NAMES:
        is_partitioned = cookie_name == PARTITIONED_SESSION_COOKIE_NAME
        response.set_cookie(
            cookie_name,
            cookie_value,
            max_age=SESSION_LIFETIME_SECONDS,
            domain=workspace_domain,
            path="/",
            secure=True,
            httponly=True,
            samesite="None" if is_partitioned else "Lax",
            partitioned=is_partitioned,
        )


def strip_session_cookie(cookie_header: str) -> str:
    """The Cookie header minus both session cookie copies -- what gets forwarded to the service."""
    kept_parts = []
    for part in cookie_header.split(";"):
        name, _, _value = part.strip().partition("=")
        if name.strip() not in _SESSION_COOKIE_NAMES:
            kept_parts.append(part.strip())
    return "; ".join(kept_parts)
