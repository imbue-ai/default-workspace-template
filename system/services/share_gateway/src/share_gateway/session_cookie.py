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

Safari builds between 18.5 and 26.1 reject a cookie carrying ``Partitioned``
outright instead of storing it unpartitioned, and browsers that do implement
CHIPS key a cookie set during the broker's login redirect by the broker's
site rather than the workspace's, so a lone partitioned cookie leaves a
phone visitor with no session at all. Verification accepts whichever copy
the browser sends.

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
    """Attach both copies of the workspace session cookie (see the module docstring).

    Werkzeug's ``set_cookie`` cannot emit ``Partitioned``, so the attribute is
    appended to the partitioned copy's rendered Set-Cookie header.
    """
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
        )
    _append_partitioned_attribute(response)


def _append_partitioned_attribute(response: Response) -> None:
    """Append ``; Partitioned`` to the partitioned copy's Set-Cookie header Werkzeug just wrote."""
    rewritten_headers: list[tuple[str, str]] = []
    for header_name, header_value in response.headers.items():
        is_partitioned_cookie = header_name.lower() == "set-cookie" and header_value.startswith(
            f"{PARTITIONED_SESSION_COOKIE_NAME}="
        )
        # The attribute list excludes the leading name=value pair (the name
        # itself contains "partitioned").
        attributes = {part.strip().lower() for part in header_value.split(";")[1:]}
        if is_partitioned_cookie and "partitioned" not in attributes:
            rewritten_headers.append((header_name, f"{header_value}; Partitioned"))
        else:
            rewritten_headers.append((header_name, header_value))
    response.headers.clear()
    for header_name, header_value in rewritten_headers:
        response.headers.add(header_name, header_value)


def strip_session_cookie(cookie_header: str) -> str:
    """The Cookie header minus both session cookie copies -- what gets forwarded to the service."""
    kept_parts = []
    for part in cookie_header.split(";"):
        name, _, _value = part.strip().partition("=")
        if name.strip() not in _SESSION_COOKIE_NAMES:
            kept_parts.append(part.strip())
    return "; ".join(kept_parts)
