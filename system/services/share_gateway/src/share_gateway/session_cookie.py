"""The workspace session cookie: one HS256 JWT scoped ``Domain=<workspace-domain>``.

Set once by the login callback, verified (and its email re-checked against the
grants) on every request. 24 hours, fixed. The signing secret is generated in
the workspace and never leaves it, so a relay or connector compromise cannot
mint sessions.

The cookie carries an ``owner`` flag (the visitor is the workspace owner, per
the broker's handoff) so the hosted minds chrome can embed the workspace in a
cross-site iframe: it is set ``SameSite=None; Secure; Partitioned`` (see
``set_session_cookie``) and the owner flag rides along for the owner-only
in-workspace exec service.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import jwt
from flask import Response

SESSION_COOKIE_NAME = "imbue_machine_session"
SESSION_LIFETIME_SECONDS = 24 * 3600

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


def set_session_cookie(response: Response, cookie_value: str, workspace_domain: str) -> None:
    """Attach the workspace session cookie so it is sent from a cross-site iframe.

    Browsers only send a third-party (cross-site) cookie from inside an iframe
    when it is ``SameSite=None; Secure``, and only isolate it per top-level
    site (CHIPS) when it is also ``Partitioned``. Werkzeug's ``set_cookie``
    cannot emit ``Partitioned``, so the attribute is appended to the rendered
    Set-Cookie header.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        max_age=SESSION_LIFETIME_SECONDS,
        domain=workspace_domain,
        path="/",
        secure=True,
        httponly=True,
        samesite="None",
    )
    _append_partitioned_attribute(response)


def _append_partitioned_attribute(response: Response) -> None:
    """Append ``; Partitioned`` to the session Set-Cookie header Werkzeug just wrote."""
    rewritten_headers: list[tuple[str, str]] = []
    for header_name, header_value in response.headers.items():
        is_session_cookie = header_name.lower() == "set-cookie" and header_value.startswith(
            f"{SESSION_COOKIE_NAME}="
        )
        if is_session_cookie and "partitioned" not in header_value.lower():
            rewritten_headers.append((header_name, f"{header_value}; Partitioned"))
        else:
            rewritten_headers.append((header_name, header_value))
    response.headers.clear()
    for header_name, header_value in rewritten_headers:
        response.headers.add(header_name, header_value)


def strip_session_cookie(cookie_header: str) -> str:
    """The Cookie header minus our session cookie -- what gets forwarded to the service."""
    kept_parts = []
    for part in cookie_header.split(";"):
        name, _, _value = part.strip().partition("=")
        if name.strip() != SESSION_COOKIE_NAME:
            kept_parts.append(part.strip())
    return "; ".join(kept_parts)
