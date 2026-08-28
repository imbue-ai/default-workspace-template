"""Structured per-request access logging for our Modal ASGI apps.

Modal's function logs carry only what the app prints, so without this there
is no per-request record at all -- nothing tying abusive traffic to a client
IP. The middleware here emits one single-line JSON object per HTTP request
(``{"type": "http_request", ...}``) with the client IP and basic request
metadata. JSON is the machine-readable shape: the lines flow through Modal's
OTEL integration into the tier's log store, where both abuse investigations
and the analytics aggregation parse them with plain JSON extraction. The
encoding also makes forgery moot -- ``json.dumps`` escapes quotes, newlines,
and control characters, so client-controlled fields (the percent-decoded
path, the user agent) can never break out of their value or fake a second
line.

The line deliberately excludes the query string (several routes carry
one-time tokens there) and every header except the user agent and the
``X-Imbue-Client`` client id. When a route
resolved an authenticated identity, it can expose it to the log line by
stashing the user id in ASGI scope state under
``AUTHENTICATED_USER_STATE_KEY`` (e.g. via Starlette's ``request.state``);
the middleware reads it back after the response, so the line carries the
full user id on authenticated requests and omits the field otherwise. Two
more scope-state keys let a route shape its own line:
``ACCESS_LOG_SUPPRESS_SUCCESS_STATE_KEY`` drops the line for 2xx responses
only (high-frequency machine traffic counted by metric records instead),
and ``ACCESS_LOG_PATH_OVERRIDE_STATE_KEY`` replaces the logged path when
the real one carries a credential in a path segment.
"""

import json
import logging
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any
from typing import Final

from imbue.modal_app_kit.log_format import StructuredRecordJsonLogFormatter
from imbue.modal_app_kit.log_format import deployed_minds_env_name

logger = logging.getLogger(__name__)

_USER_AGENT_MAX_LENGTH: Final[int] = 300

# Status logged when the wrapped app raised before starting a response: the
# ASGI server turns that into a 500 for the client, so the log line matches
# what the client saw.
_UNHANDLED_ERROR_STATUS: Final[int] = 500

# Scope-state key a route (or auth layer) sets to attach the authenticated
# user's id to the request's access-log line. Starlette-based apps set it via
# ``request.state``, which is backed by ``scope["state"]``.
AUTHENTICATED_USER_STATE_KEY: Final[str] = "authenticated_user_id"

# Scope-state key a route sets (to a truthy value) to suppress the request's
# access-log line -- honored ONLY when the response status is 2xx, so an
# error outcome always logs in full no matter what the route declared. For
# high-frequency machine traffic (the connector's frps heartbeats) whose
# successful requests are counted by periodic metric records instead of
# per-request lines.
ACCESS_LOG_SUPPRESS_SUCCESS_STATE_KEY: Final[str] = "access_log_suppress_success"

# Scope-state key a route sets to replace the logged request path with a
# sanitized form -- for routes whose real path carries a credential in a path
# segment (the frps plugin-auth shared secret), which must not land in the
# tier's log store.
ACCESS_LOG_PATH_OVERRIDE_STATE_KEY: Final[str] = "access_log_path_override"


def _first_header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str:
    # ASGI header values are latin-1 per the spec.
    for header_name, header_value in headers:
        if header_name.lower() == name:
            return header_value.decode("latin-1")
    return ""


def client_ip_from_asgi_scope(scope: Mapping[str, Any]) -> str:
    """The end-client IP of an HTTP request, or ``"-"`` when unknown.

    The ASGI socket peer is the ONLY trustworthy source behind Modal's
    ingress: Modal delivers the real end-client IP as the connection peer
    (their documented way to read the client IP) and strips any
    client-supplied ``X-Forwarded-For`` before the request reaches the app
    (verified empirically 2026-08 against a deployed echo endpoint, over
    both HTTP/1.1 and HTTP/2 and every header-case spelling). Other
    forwarding-style headers (``X-Real-IP``, ``Forwarded``,
    ``CF-Connecting-IP``) pass through Modal UNSANITIZED and are therefore
    attacker-controlled -- no caller may ever consult them. This value
    feeds abuse enforcement (per-IP signup limits), not just logs, so the
    header-less derivation here is load-bearing.
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client and client[0]:
        return str(client[0])
    return "-"


def _scope_state_string(scope: dict[str, Any], key: str) -> str:
    state = scope.get("state")
    if not isinstance(state, dict):
        return ""
    value = state.get(key)
    return value if isinstance(value, str) else ""


def _is_success_line_suppressed(scope: dict[str, Any], status_code: int | None) -> bool:
    if status_code is None or not (200 <= status_code < 300):
        return False
    state = scope.get("state")
    return isinstance(state, dict) and bool(state.get(ACCESS_LOG_SUPPRESS_SUCCESS_STATE_KEY))


def format_request_log_line(scope: dict[str, Any], status_code: int | None, duration_ms: float) -> str:
    """One single-line JSON access record: method, path, status, duration, client IP, user agent, client id, user.

    The query string is deliberately omitted (it can carry one-time tokens,
    e.g. ``/auth/verify-email?token=...``). ``status_code`` None means the app
    raised before starting a response; that is logged as 500, matching what
    the ASGI server sends the client. The ``user`` field appears only when a
    route stashed an authenticated identity in scope state. ``json.dumps``
    with ``ensure_ascii`` keeps every client-controlled value (the
    percent-decoded path can carry spaces, quotes, even newlines) escaped
    inside its JSON string, so the output is always exactly one line.
    """
    user_agent = _first_header_value(scope.get("headers") or [], b"user-agent")[:_USER_AGENT_MAX_LENGTH]
    # The canonical client self-identification header (e.g.
    # "minds/0.3.16 imbue-cloud-plugin/0.1.6" or "web/<deploy-id>"): browsers
    # own User-Agent, so the fleet-version picture -- the input for
    # support-window and deprecation decisions -- reads from this field.
    imbue_client = _first_header_value(scope.get("headers") or [], b"x-imbue-client")[:_USER_AGENT_MAX_LENGTH]
    path_override = _scope_state_string(scope, ACCESS_LOG_PATH_OVERRIDE_STATE_KEY)
    record: dict[str, Any] = {
        "type": "http_request",
        "method": scope.get("method", "-"),
        "path": path_override if path_override else str(scope.get("path", "-")),
        "status": status_code if status_code is not None else _UNHANDLED_ERROR_STATUS,
        "duration_ms": round(duration_ms, 1),
        "client_ip": client_ip_from_asgi_scope(scope),
        "user_agent": user_agent,
        "imbue_client": imbue_client,
    }
    authenticated_user = _scope_state_string(scope, AUTHENTICATED_USER_STATE_KEY)
    if authenticated_user:
        record["user"] = authenticated_user
    env_name = deployed_minds_env_name()
    if env_name:
        record["minds_env"] = env_name
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"))


def ensure_info_log_handler(target_logger: logging.Logger) -> None:
    """Make a logger's INFO lines reach the container's stderr as JSON, regardless of the root logger.

    The structured record lines must flow even in a process that never
    configured the root logger (unit tests, a container before
    ``configure_logging`` ran), and must not double up once it has: a
    dedicated handler on the target logger with ``propagate=False`` gives
    both. The handler renders with ``StructuredRecordJsonLogFormatter``, which
    flattens the JSON-object message into the JSON envelope (level, timestamp,
    logger) -- so every message logged through the target logger MUST be a
    structured record. Idempotent. Also used by app-side structured event
    lines (e.g. the connector's share-visit records) that must reach the log
    store at INFO.
    """
    if target_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredRecordJsonLogFormatter())
    target_logger.addHandler(handler)
    target_logger.setLevel(logging.INFO)
    target_logger.propagate = False


def _log_request_line(line: str) -> None:
    logger.info("%s", line)


class RequestLoggingMiddleware:
    """Pure-ASGI middleware that logs one JSON line per HTTP request.

    Added outermost so it observes the final response status (after every
    inner middleware) and any scope state the routed handler stashed (the
    scope dict is shared down the stack, so mutations made during routing are
    visible here after the app returns). Non-HTTP scopes (lifespan,
    websocket) pass through unlogged. ``line_sink`` is injectable for tests;
    production uses the module logger. The ``async`` is mandated by the ASGI
    protocol.
    """

    def __init__(self, app: Any, line_sink: Callable[[str], None] | None = None) -> None:
        self.app = app
        self.line_sink = line_sink if line_sink is not None else _log_request_line
        if line_sink is None:
            ensure_info_log_handler(logger)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not (isinstance(scope, dict) and scope.get("type") == "http"):
            await self.app(scope, receive, send)
            return
        start_monotonic = time.monotonic()
        status_holder: dict[str, int | None] = {"status": None}

        async def _send_recording_status(message: Any) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                raw_status = message.get("status")
                status_holder["status"] = raw_status if isinstance(raw_status, int) else None
            await send(message)

        # The line is emitted in the finally so an exception escaping the app
        # (no response ever started) is still recorded, as a 500.
        try:
            await self.app(scope, receive, _send_recording_status)
        finally:
            if not _is_success_line_suppressed(scope, status_holder["status"]):
                duration_ms = (time.monotonic() - start_monotonic) * 1000.0
                self.line_sink(format_request_log_line(scope, status_holder["status"], duration_ms))
