"""The workspace session cookie: one HS256 JWT scoped ``Domain=<workspace-domain>``.

Set once by the login callback, verified (and its email re-checked against the
grants) on every request. 24 hours, fixed. The signing secret is generated in
the workspace and never leaves it, so a relay or connector compromise cannot
mint sessions.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt

SESSION_COOKIE_NAME = "imbue_machine_session"
SESSION_LIFETIME_SECONDS = 24 * 3600

_SESSION_ALGORITHM = "HS256"


def mint_session_cookie_value(signing_secret: str, email: str, workspace_domain: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "email": email,
        "aud": workspace_domain,
        "iat": now,
        "exp": now + timedelta(seconds=SESSION_LIFETIME_SECONDS),
    }
    return jwt.encode(payload, signing_secret, algorithm=_SESSION_ALGORITHM)


def verify_session_cookie_value(signing_secret: str, cookie_value: str, workspace_domain: str) -> str | None:
    """Return the session's email, or None when the cookie is missing/expired/forged."""
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
    return email


def strip_session_cookie(cookie_header: str) -> str:
    """The Cookie header minus our session cookie -- what gets forwarded to the service."""
    kept_parts = []
    for part in cookie_header.split(";"):
        name, _, _value = part.strip().partition("=")
        if name.strip() != SESSION_COOKIE_NAME:
            kept_parts.append(part.strip())
    return "; ".join(kept_parts)
