"""The owner gate (gate mode): an auth sandbox's Screen is reached over Modal's TLS from outside the sandbox, with no
share gateway in front of it to vouch for the requester, so the service admits a request only when it is loopback (the
boot script, the watcher, the commands) or carries the owner session that the owner token opened
(``system/libs/auth_sandbox/README.md``).

Gate mode is on exactly when ``SCREEN_OWNER_TOKEN_SHA256_FILE`` is set. The file it names holds the owner token's
SHA-256 and is read on every request that is not loopback: a pooled sandbox's Screen starts before anyone has claimed
it, and the sandbox writes the digest only once the connector claims it for a user. Until then (no file, or an empty
one) and whenever the file does not hold a digest, the gate admits loopback alone. The connector mints the token and
hands the plaintext to the hosted chrome once; the chrome frames the Screen at ``/?owner_token=<token>``, and the gate
trades the token for the session cookies and redirects to the same URL without it. Every other request needs one of
those cookies, WebSocket upgrades and the neko proxy included.

Every ``*.modal.host`` page is same-site with the Screen, so its cookies ride that page's requests too. The gate
therefore also refuses a request its browser marks (``Sec-Fetch-Site``) as coming from a page of another origin,
other than a GET or HEAD navigation: the chrome's frame of the Screen and the tab it opens are such navigations, and
the Screen's own pages call it from its own origin. This holds for loopback too, since a page the sandbox's sign-in
browser shows can reach the Screen there. A browser sends no fetch metadata to a plain http URL, so loopback is the
owner only under a loopback ``Host``: a page that reaches loopback through a name of its own (a rebound or wildcard DNS
name) names that host instead.

The session is set twice, under two names, for the reasons the share gateway's ``session_cookie.py`` gives: a
``SameSite=Lax`` copy for a top-level visit and a ``SameSite=None; Partitioned`` copy for the chrome's cross-site
iframe. Its value is an HMAC under a secret generated at start and held in memory only, so a restart ends every
session (the token opens a new one). Every value the request carries under either name is tried: ``modal.host`` is
not on the Public Suffix List, so another Modal customer's page can set same-named cookies for the parent domain,
which the browser sends beside this host's own.
"""

import hashlib
import hmac
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field
from pydantic import SecretBytes
from werkzeug.datastructures import MultiDict

from app_schema.routes import error_response
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from screen.errors import ScreenConfigurationError

OWNER_TOKEN_SHA256_FILE_ENV_VAR: Final[str] = "SCREEN_OWNER_TOKEN_SHA256_FILE"
FRAME_ANCESTORS_ENV_VAR: Final[str] = "SCREEN_FRAME_ANCESTORS"

OWNER_TOKEN_QUERY_PARAMETER: Final[str] = "owner_token"
OWNER_SESSION_COOKIE_NAME: Final[str] = "screen_owner_session"
PARTITIONED_OWNER_SESSION_COOKIE_NAME: Final[str] = "screen_owner_session_partitioned"
OWNER_SESSION_COOKIE_NAMES: Final[tuple[str, ...]] = (OWNER_SESSION_COOKIE_NAME, PARTITIONED_OWNER_SESSION_COOKIE_NAME)

OWNER_SESSION_REQUIRED: Final[str] = "an owner session is required"
OWNER_TOKEN_REFUSED: Final[str] = "the owner token is not this Screen's"
OTHER_ORIGIN_REFUSED: Final[str] = "only the Screen's own pages may call it"

# Where the gate is kept on the Flask app, so the identity checks know the identity header is to be ignored.
OWNER_GATE_EXTENSION: Final[str] = "screen_owner_gate"

HTTP_FOUND: Final[int] = 302
HTTP_UNAUTHORIZED: Final[int] = 401
HTTP_FORBIDDEN: Final[int] = 403

_LOOPBACK_ADDRESSES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1"})
_LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
_FETCH_SITE_HEADER: Final[str] = "Sec-Fetch-Site"
_FETCH_MODE_HEADER: Final[str] = "Sec-Fetch-Mode"
_OWN_ORIGIN_FETCH_SITES: Final[frozenset[str]] = frozenset({"same-origin", "none"})
_NAVIGATE_FETCH_MODE: Final[str] = "navigate"
_NAVIGATION_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
_SHA256_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# A source list is one directive's value: a separator would end it and start another directive or policy.
_FRAME_ANCESTORS_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset({";", ",", "\r", "\n"})
_SESSION_SECRET_BYTES: Final[int] = 32
_SESSION_MESSAGE: Final[bytes] = b"screen owner session"
_CONTENT_SECURITY_POLICY_HEADER: Final[str] = "Content-Security-Policy"
_FRAME_ANCESTORS_DIRECTIVE: Final[str] = "frame-ancestors"
# Every character a path may carry unescaped (RFC 3986 pchar and the segment separator).
_PATH_SAFE_CHARACTERS: Final[str] = "/:@!$&'()*+,;=-._~"
_REDACTED_OWNER_TOKEN: Final[str] = "REDACTED"


class OwnerGate(FrozenModel):
    """What gate mode admits a request with: the owner token's digest, the session secret, and who may frame the
    Screen."""

    owner_token_sha256_path: Path = Field(
        description="The file holding the lower-case hex SHA-256 of the owner token, absent until the sandbox is claimed"
    )
    frame_ancestors: str = Field(description="The frame-ancestors source list every response carries")
    session_secret: SecretBytes = Field(description="The owner session's HMAC key, generated at start")


def owner_gate_from_environ(environ: Mapping[str, str]) -> OwnerGate | None:
    """The gate the environment asks for with a fresh session secret, or None when it names no owner token file."""
    token_sha256_path = environ.get(OWNER_TOKEN_SHA256_FILE_ENV_VAR, "")
    if not token_sha256_path:
        return None
    frame_ancestors = environ.get(FRAME_ANCESTORS_ENV_VAR, "").strip()
    if not frame_ancestors:
        raise ScreenConfigurationError(f"{FRAME_ANCESTORS_ENV_VAR} must name who may frame a gated Screen")
    if any(character in _FRAME_ANCESTORS_FORBIDDEN_CHARACTERS for character in frame_ancestors):
        raise ScreenConfigurationError(f"{FRAME_ANCESTORS_ENV_VAR} must be one source list, with no ';' or ','")
    return OwnerGate(
        owner_token_sha256_path=Path(token_sha256_path),
        frame_ancestors=frame_ancestors,
        session_secret=SecretBytes(secrets.token_bytes(_SESSION_SECRET_BYTES)),
    )


def read_owner_token_sha256(gate: OwnerGate) -> str | None:
    """The owner token's digest as the file holds it now, or None when no one but loopback may be admitted: no file
    or an empty one (the sandbox is not claimed yet), or one that cannot be read or is not a digest (logged)."""
    try:
        text = gate.owner_token_sha256_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        logger.error("The owner token digest at {} cannot be read: {}", gate.owner_token_sha256_path, e)
        return None
    if not text:
        return None
    if _SHA256_HEX_PATTERN.fullmatch(text) is None:
        logger.error("The owner token digest at {} is not 64 lower-case hex digits", gate.owner_token_sha256_path)
        return None
    return text


@pure
def is_loopback_address(remote_address: str | None) -> bool:
    return remote_address in _LOOPBACK_ADDRESSES


@pure
def is_loopback_host(host: str) -> bool:
    """Whether a request's ``Host`` (with or without its port) names loopback by address or as ``localhost``."""
    try:
        hostname = urllib.parse.urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    return hostname in _LOOPBACK_HOSTNAMES


@pure
def is_from_another_origin(method: str, fetch_site: str | None, fetch_mode: str | None) -> bool:
    """Whether the browser marks the request as made by a page of another origin, other than a GET or HEAD
    navigation. A request without fetch metadata (a client that is not a browser) is not judged here."""
    if fetch_site is None or fetch_site in _OWN_ORIGIN_FETCH_SITES:
        return False
    return not (fetch_mode == _NAVIGATE_FETCH_MODE and method in _NAVIGATION_METHODS)


@pure
def is_owner_token(owner_token_sha256: str, token: str) -> bool:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, owner_token_sha256)


@pure
def owner_session_cookie_value(gate: OwnerGate) -> str:
    return hmac.new(gate.session_secret.get_secret_value(), _SESSION_MESSAGE, hashlib.sha256).hexdigest()


@pure
def has_owner_session(gate: OwnerGate, cookies: MultiDict[str, str]) -> bool:
    """Whether any value the request carries under either session cookie name is the owner session.

    Compared as bytes: ``hmac.compare_digest`` raises on a non-ASCII ``str``, which a cookie another ``modal.host``
    page set can carry, and the session is hex, so no replaced character can match it.
    """
    expected = owner_session_cookie_value(gate).encode("ascii")
    return any(
        hmac.compare_digest(value.encode("utf-8", errors="replace"), expected)
        for name in OWNER_SESSION_COOKIE_NAMES
        for value in cookies.getlist(name)
    )


@pure
def merge_frame_ancestors(existing_policy: str | None, frame_ancestors: str) -> str:
    """The response's policy with its ``frame-ancestors`` directive replaced by (or extended with) the gate's."""
    directive = f"{_FRAME_ANCESTORS_DIRECTIVE} {frame_ancestors}"
    if existing_policy is None or not existing_policy.strip():
        return directive
    kept = [
        part.strip()
        for part in existing_policy.split(";")
        if part.strip() and part.split()[0].lower() != _FRAME_ANCESTORS_DIRECTIVE
    ]
    return "; ".join([*kept, directive])


@pure
def location_without_owner_token(path: str, query_pairs: tuple[tuple[str, str], ...]) -> str:
    """The same URL, relative, with every ``owner_token`` dropped from its query; a path that starts with ``//`` is
    collapsed to one slash so the redirect cannot name another host."""
    location = urllib.parse.quote("/" + path.lstrip("/"), safe=_PATH_SAFE_CHARACTERS)
    kept = [(name, value) for name, value in query_pairs if name != OWNER_TOKEN_QUERY_PARAMETER]
    return f"{location}?{urllib.parse.urlencode(kept)}" if kept else location


@pure
def request_target_with_owner_token_redacted(target: str) -> str:
    """A request target as the access log may show it: every ``owner_token`` value replaced, since the token admits
    its bearer for the sandbox's whole life. A target without one is returned unchanged."""
    split_target = urllib.parse.urlsplit(target)
    query_pairs = urllib.parse.parse_qsl(split_target.query, keep_blank_values=True)
    if all(name != OWNER_TOKEN_QUERY_PARAMETER for name, _ in query_pairs):
        return target
    redacted_pairs = [
        (name, _REDACTED_OWNER_TOKEN if name == OWNER_TOKEN_QUERY_PARAMETER else value) for name, value in query_pairs
    ]
    return urllib.parse.urlunsplit(split_target._replace(query=urllib.parse.urlencode(redacted_pairs)))


def _set_owner_session_cookies(response: Response, gate: OwnerGate) -> None:
    value = owner_session_cookie_value(gate)
    for name in OWNER_SESSION_COOKIE_NAMES:
        is_partitioned = name == PARTITIONED_OWNER_SESSION_COOKIE_NAME
        response.set_cookie(
            name,
            value,
            path="/",
            secure=True,
            httponly=True,
            samesite="None" if is_partitioned else "Lax",
            partitioned=is_partitioned,
        )


def _trade_owner_token_for_session(gate: OwnerGate) -> Response:
    owner_token_sha256 = read_owner_token_sha256(gate)
    tokens = request.args.getlist(OWNER_TOKEN_QUERY_PARAMETER)
    if owner_token_sha256 is None or not all(is_owner_token(owner_token_sha256, token) for token in tokens):
        return error_response(OWNER_TOKEN_REFUSED, HTTP_UNAUTHORIZED)
    location = location_without_owner_token(request.path, tuple(request.args.items(multi=True)))
    response = Response(status=HTTP_FOUND, headers={"Location": location, "Cache-Control": "no-store"})
    _set_owner_session_cookies(response, gate)
    return response


def is_owner_gate_installed(app: Flask) -> bool:
    return OWNER_GATE_EXTENSION in app.extensions


def install_owner_gate(app: Flask, gate: OwnerGate) -> None:
    """Admit only the owner to every route of ``app`` and restrict who may frame it. Install it before any other
    request hook, so nothing runs for a request the gate refuses."""
    app.extensions[OWNER_GATE_EXTENSION] = gate

    @app.before_request
    def admit_only_the_owner() -> ResponseReturnValue | None:
        if is_from_another_origin(
            request.method, request.headers.get(_FETCH_SITE_HEADER), request.headers.get(_FETCH_MODE_HEADER)
        ):
            return error_response(OTHER_ORIGIN_REFUSED, HTTP_FORBIDDEN)
        if request.method == "GET" and OWNER_TOKEN_QUERY_PARAMETER in request.args:
            return _trade_owner_token_for_session(gate)
        if is_loopback_address(request.remote_addr) and is_loopback_host(request.host):
            return None
        if read_owner_token_sha256(gate) is not None and has_owner_session(gate, request.cookies):
            return None
        return error_response(OWNER_SESSION_REQUIRED, HTTP_UNAUTHORIZED)

    @app.after_request
    def restrict_framing(response: Response) -> Response:
        existing_policy = response.headers.get(_CONTENT_SECURITY_POLICY_HEADER)
        response.headers[_CONTENT_SECURITY_POLICY_HEADER] = merge_frame_ancestors(
            existing_policy, gate.frame_ancestors
        )
        return response
