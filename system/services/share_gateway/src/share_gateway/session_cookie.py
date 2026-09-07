"""The workspace session: one HS256 JWT, delivered as two cookies scoped ``Domain=<workspace-domain>``.

Set once by the login callback, verified (and its email re-checked against the
grants) on every request. 24 hours, fixed. The signing secret is generated in
the workspace and never leaves it, so a relay or connector compromise cannot
mint sessions.

The same value is set twice, under two names, because no single cookie works
in both places a visitor reaches a shared workspace from:

- ``imbue_machine_session`` is a plain ``SameSite=None; Secure`` cookie: the
  one a browser sends when the workspace is the top-level site (a share link
  opened directly, including on Safari).
- ``imbue_machine_session_partitioned`` adds ``Partitioned`` (CHIPS) so the
  hosted minds chrome can embed the workspace in a cross-site iframe: browsers
  only send a third-party cookie from an iframe when it is partitioned by the
  embedding site.

Safari 18.5 through 26.1 rejects a cookie carrying ``Partitioned`` outright
instead of storing it unpartitioned (WebKit bug 292975), so a lone
partitioned cookie leaves an iOS visitor with no session at all. Verification
accepts whichever copy the browser sends.

The plain copy is an ordinary ``SameSite=None`` cookie, so browsers that still
allow third-party cookies attach it to cross-site GET subresources too. That
is bounded by the existing controls: ``frame-ancestors`` limits embedding to
the workspace's own origins and the chrome, the Origin policy rejects non-GETs
with a foreign Origin, and nothing but ``/_health`` answers cross-origin reads.

The payload carries an ``owner`` flag (the visitor is the workspace owner, per
the broker's handoff), which rides along for the owner-only in-workspace exec
service.
"""

from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
from flask import Response

SESSION_COOKIE_NAME = "imbue_machine_session"
PARTITIONED_SESSION_COOKIE_NAME = "imbue_machine_session_partitioned"
SESSION_LIFETIME_SECONDS = 24 * 3600

_SESSION_COOKIE_NAMES = (SESSION_COOKIE_NAME, PARTITIONED_SESSION_COOKIE_NAME)
_SESSION_ALGORITHM = "HS256"


class SessionIdentity:
    """The verified contents of a workspace session cookie."""

    def __init__(self, email: str, is_owner: bool) -> None:
        self.email = email
        self.is_owner = is_owner

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionIdentity):
            return NotImplemented
        return self.email == other.email and self.is_owner == other.is_owner


def mint_session_cookie_value(signing_secret: str, email: str, workspace_domain: str, is_owner: bool) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "email": email,
        "owner": is_owner,
        "aud": workspace_domain,
        "iat": now,
        "exp": now + timedelta(seconds=SESSION_LIFETIME_SECONDS),
    }
    return jwt.encode(payload, signing_secret, algorithm=_SESSION_ALGORITHM)


def verify_session_cookie_value(signing_secret: str, cookie_value: str, workspace_domain: str) -> "SessionIdentity | None":
    """Return the session identity, or None when the cookie is missing/expired/forged."""
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
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        return None
    return SessionIdentity(email=email, is_owner=bool(claims.get("owner", False)))


def verify_session_from_cookies(
    signing_secret: str, cookies: Mapping[str, str], workspace_domain: str
) -> "SessionIdentity | None":
    """The identity of the first session cookie copy that verifies, or None when neither does."""
    for cookie_name in _SESSION_COOKIE_NAMES:
        identity = verify_session_cookie_value(signing_secret, cookies.get(cookie_name, ""), workspace_domain)
        if identity is not None:
            return identity
    return None


def set_session_cookie(response: Response, cookie_value: str, workspace_domain: str) -> None:
    """Attach both copies of the workspace session cookie (see the module docstring)."""
    for cookie_name in _SESSION_COOKIE_NAMES:
        response.set_cookie(
            cookie_name,
            cookie_value,
            max_age=SESSION_LIFETIME_SECONDS,
            domain=workspace_domain,
            path="/",
            secure=True,
            httponly=True,
            samesite="None",
            partitioned=cookie_name == PARTITIONED_SESSION_COOKIE_NAME,
        )


def strip_session_cookie(cookie_header: str) -> str:
    """The Cookie header minus both session cookie copies -- what gets forwarded to the service."""
    kept_parts = []
    for part in cookie_header.split(";"):
        name, _, _value = part.strip().partition("=")
        if name.strip() not in _SESSION_COOKIE_NAMES:
            kept_parts.append(part.strip())
    return "; ".join(kept_parts)
