from typing import Any

import pytest

from imbue.modal_app_kit.request_logging import RequestLoggingMiddleware
from imbue.modal_app_kit.request_logging import client_ip_from_asgi_scope
from imbue.modal_app_kit.request_logging import format_request_log_line


def _http_scope(
    method: str = "GET",
    path: str = "/hosts/lease",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 54321),
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"token=super-secret",
        "headers": headers if headers is not None else [],
        "client": client,
    }


def test_client_ip_prefers_the_first_x_forwarded_for_hop() -> None:
    scope = _http_scope(headers=[(b"x-forwarded-for", b"203.0.113.9, 10.0.0.2")])
    assert client_ip_from_asgi_scope(scope) == "203.0.113.9"


def test_client_ip_falls_back_to_the_socket_peer() -> None:
    assert client_ip_from_asgi_scope(_http_scope()) == "127.0.0.1"


def test_client_ip_is_a_dash_when_nothing_is_known() -> None:
    assert client_ip_from_asgi_scope(_http_scope(client=None)) == "-"


def test_client_ip_discards_injected_content_after_the_ip_token() -> None:
    # The header is client-controlled free text logged unquoted: whitespace
    # and control characters must never survive into the field value.
    scope = _http_scope(headers=[(b"x-forwarded-for", b"9.9.9.9 status=200\x01, 10.0.0.2")])
    assert client_ip_from_asgi_scope(scope) == "9.9.9.9"


def test_format_request_log_line_includes_metadata_and_excludes_the_query_string() -> None:
    scope = _http_scope(
        method="POST",
        path="/auth/verify-email",
        headers=[
            (b"x-forwarded-for", b"203.0.113.9"),
            (b"user-agent", b'Mozilla/5.0 ("weird" agent)'),
        ],
    )

    line = format_request_log_line(scope, 200, 12.34)

    assert "method=POST" in line
    assert 'path="/auth/verify-email"' in line
    assert "status=200" in line
    assert "duration_ms=12.3" in line
    assert "client_ip=203.0.113.9" in line
    # Embedded double quotes are replaced so the quoted field stays parseable.
    assert "user_agent=\"Mozilla/5.0 ('weird' agent)\"" in line
    assert "super-secret" not in line
    assert "token=" not in line


def test_format_request_log_line_defuses_a_forged_path() -> None:
    # The ASGI path arrives percent-DECODED, so a crafted request target like
    # /x%20client_ip=9.9.9.9%0Afake can carry spaces, quotes, and newlines.
    # Quoting plus control-character replacement keeps the forged content
    # inert: no injected field parses outside the quotes and no second line
    # appears in the log.
    scope = _http_scope(path='/x client_ip=9.9.9.9\nHandled request status=200 "')

    line = format_request_log_line(scope, 404, 1.0)

    assert "\n" not in line
    # Exactly two occurrences: the real prefix plus the quoted, inert copy.
    assert line.count("Handled request") == 2
    assert 'path="/x client_ip=9.9.9.9?Handled request status=200 \'"' in line
    assert "status=404" in line


def test_format_request_log_line_reports_500_when_no_response_started() -> None:
    line = format_request_log_line(_http_scope(), None, 1.0)
    assert "status=500" in line


def _run_coroutine_synchronously(coroutine: Any) -> None:
    """Step a coroutine that never suspends on anything unresolved (no event loop needed)."""
    with pytest.raises(StopIteration):
        coroutine.send(None)


def test_middleware_logs_one_line_with_the_response_status() -> None:
    lines: list[str] = []
    sent_messages: list[dict[str, Any]] = []

    async def _send(message: dict[str, Any]) -> None:
        sent_messages.append(message)

    class _RespondingApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    middleware = RequestLoggingMiddleware(_RespondingApp(), line_sink=lines.append)

    scope = _http_scope(headers=[(b"x-forwarded-for", b"198.51.100.7")])
    _run_coroutine_synchronously(middleware(scope, None, _send))

    # The response passed through untouched and exactly one line was logged.
    assert [message["type"] for message in sent_messages] == ["http.response.start", "http.response.body"]
    assert len(lines) == 1
    assert "status=403" in lines[0]
    assert "client_ip=198.51.100.7" in lines[0]


def test_middleware_logs_a_500_line_when_the_app_raises() -> None:
    lines: list[str] = []

    class _CrashingApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            raise RuntimeError("boom-4189")

    middleware = RequestLoggingMiddleware(_CrashingApp(), line_sink=lines.append)

    with pytest.raises(RuntimeError, match="boom-4189"):
        middleware(_http_scope(), None, None).send(None)
    assert len(lines) == 1
    assert "status=500" in lines[0]


def test_middleware_passes_non_http_scopes_through_unlogged() -> None:
    lines: list[str] = []
    seen_scopes: list[Any] = []

    class _PassthroughApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            seen_scopes.append(scope)

    middleware = RequestLoggingMiddleware(_PassthroughApp(), line_sink=lines.append)

    _run_coroutine_synchronously(middleware({"type": "lifespan"}, None, None))
    assert seen_scopes[0]["type"] == "lifespan"
    assert lines == []
