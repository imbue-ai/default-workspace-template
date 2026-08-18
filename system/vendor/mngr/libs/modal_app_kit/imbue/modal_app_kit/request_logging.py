"""Structured per-request access logging for our Modal ASGI apps.

Modal's function logs carry only what the app prints, so without this there
is no per-request record at all -- nothing tying abusive traffic to a client
IP. The middleware here emits one structured line per HTTP request with the
client IP and basic request metadata. It deliberately excludes the query
string (several routes carry one-time tokens there) and every header except
the user agent.
"""

import logging
import time
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import Final

logger = logging.getLogger(__name__)

_USER_AGENT_MAX_LENGTH: Final[int] = 300

# Status logged when the wrapped app raised before starting a response: the
# ASGI server turns that into a 500 for the client, so the log line matches
# what the client saw.
_UNHANDLED_ERROR_STATUS: Final[int] = 500


def _first_header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str:
    # ASGI header values are latin-1 per the spec.
    for header_name, header_value in headers:
        if header_name.lower() == name:
            return header_value.decode("latin-1")
    return ""


def _quoted_log_value(value: str) -> str:
    """Quote a client-controlled field so it cannot forge other fields or lines.

    Embedded double quotes become single quotes and non-printable characters
    (newlines, carriage returns, other control characters -- space is
    printable) become ``?``, so a crafted value can neither break out of the
    quotes nor inject a fake log line.
    """
    cleaned = "".join(character if character.isprintable() else "?" for character in value.replace('"', "'"))
    return f'"{cleaned}"'


def client_ip_from_asgi_scope(scope: dict[str, Any]) -> str:
    """The end-client IP of an HTTP request, or ``"-"`` when unknown.

    Behind Modal's ingress the direct peer is the proxy, so the first
    ``x-forwarded-for`` hop is the real client; the socket peer is the
    fallback for direct (local/test) connections. The header is
    client-controlled free text and this value is logged unquoted, so it is
    reduced to a single printable whitespace-free token (an IP contains no
    whitespace) -- anything else in the hop is discarded rather than logged.
    """
    forwarded_for = _first_header_value(scope.get("headers") or [], b"x-forwarded-for")
    first_hop_tokens = forwarded_for.split(",")[0].split()
    first_hop = first_hop_tokens[0] if first_hop_tokens else ""
    first_hop = "".join(character for character in first_hop if character.isprintable())
    if first_hop:
        return first_hop
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client and client[0]:
        return str(client[0])
    return "-"


def format_request_log_line(scope: dict[str, Any], status_code: int | None, duration_ms: float) -> str:
    """One structured access-log line: method, path, status, duration, client IP, user agent.

    The query string is deliberately omitted (it can carry one-time tokens,
    e.g. ``/auth/verify-email?token=...``). ``status_code`` None means the app
    raised before starting a response; that is logged as 500, matching what
    the ASGI server sends the client. The path and user agent are
    client-controlled (the ASGI path arrives percent-DECODED, so it can carry
    spaces, quotes, even newlines), so both are quoted with embedded quotes
    and control characters replaced -- otherwise a crafted request could forge
    fields or entire lines in the very log abuse investigations rely on.
    """
    user_agent = _first_header_value(scope.get("headers") or [], b"user-agent")[:_USER_AGENT_MAX_LENGTH]
    # The canonical client self-identification header (e.g.
    # "minds/0.3.16 imbue-cloud-plugin/0.1.6" or "web/<deploy-id>"): browsers
    # own User-Agent, so the fleet-version picture -- the input for
    # support-window and deprecation decisions -- reads from this field.
    imbue_client = _first_header_value(scope.get("headers") or [], b"x-imbue-client")[:_USER_AGENT_MAX_LENGTH]
    return (
        "Handled request"
        f" method={scope.get('method', '-')}"
        f" path={_quoted_log_value(str(scope.get('path', '-')))}"
        f" status={status_code if status_code is not None else _UNHANDLED_ERROR_STATUS}"
        f" duration_ms={duration_ms:.1f}"
        f" client_ip={client_ip_from_asgi_scope(scope)}"
        f" user_agent={_quoted_log_value(user_agent)}"
        f" imbue_client={_quoted_log_value(imbue_client)}"
    )


def _ensure_request_log_handler() -> None:
    """Make this module's INFO lines reach the container's stderr.

    Python's root logger defaults to WARNING with no configured handler, so
    without this the request lines would be silently dropped in a container
    whose app never calls ``logging.basicConfig``. Attaching a handler to this
    module's own logger (with ``propagate=False``) keeps the lines flowing
    without touching the host app's logging configuration -- and without
    duplicating lines if the host app later configures the root logger.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _log_request_line(line: str) -> None:
    logger.info("%s", line)


class RequestLoggingMiddleware:
    """Pure-ASGI middleware that logs one structured line per HTTP request.

    Added outermost so it observes the final response status (after every
    inner middleware). Non-HTTP scopes (lifespan, websocket) pass through
    unlogged. ``line_sink`` is injectable for tests; production uses the
    module logger. The ``async`` is mandated by the ASGI protocol.
    """

    def __init__(self, app: Any, line_sink: Callable[[str], None] | None = None) -> None:
        self.app = app
        self.line_sink = line_sink if line_sink is not None else _log_request_line
        if line_sink is None:
            _ensure_request_log_handler()

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
            duration_ms = (time.monotonic() - start_monotonic) * 1000.0
            self.line_sink(format_request_log_line(scope, status_holder["status"], duration_ms))
