import json
from typing import Any

import pytest

from imbue.modal_app_kit.request_logging import AUTHENTICATED_USER_STATE_KEY
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


def test_client_ip_is_the_socket_peer() -> None:
    assert client_ip_from_asgi_scope(_http_scope()) == "127.0.0.1"


def test_client_ip_is_a_dash_when_nothing_is_known() -> None:
    assert client_ip_from_asgi_scope(_http_scope(client=None)) == "-"


def test_client_ip_never_trusts_forwarding_headers() -> None:
    # Modal strips X-Forwarded-For but passes other forwarding-style headers
    # through unsanitized, so any header consultation would be spoofable. The
    # socket peer must win even when every such header is present.
    scope = _http_scope(
        headers=[
            (b"x-forwarded-for", b"203.0.113.9, 10.0.0.2"),
            (b"x-real-ip", b"198.51.100.1"),
            (b"forwarded", b"for=198.51.100.2"),
            (b"cf-connecting-ip", b"198.51.100.3"),
        ]
    )
    assert client_ip_from_asgi_scope(scope) == "127.0.0.1"


def test_format_request_log_line_is_one_json_object_without_the_query_string() -> None:
    scope = _http_scope(
        method="POST",
        path="/auth/verify-email",
        headers=[
            (b"user-agent", b'Mozilla/5.0 ("weird" agent)'),
            (b"x-imbue-client", b"minds/0.3.16 imbue-cloud-plugin/0.1.6"),
        ],
    )

    line = format_request_log_line(scope, 200, 12.34)
    record = json.loads(line)

    assert "\n" not in line
    assert record == {
        "type": "http_request",
        "method": "POST",
        "path": "/auth/verify-email",
        "status": 200,
        "duration_ms": 12.3,
        "client_ip": "127.0.0.1",
        "user_agent": 'Mozilla/5.0 ("weird" agent)',
        "imbue_client": "minds/0.3.16 imbue-cloud-plugin/0.1.6",
    }
    assert "super-secret" not in line
    assert "token=" not in line


def test_format_request_log_line_keeps_a_forged_path_inside_its_json_string() -> None:
    # The ASGI path arrives percent-DECODED, so a crafted request target can
    # carry spaces, quotes, and newlines. JSON encoding keeps the forged
    # content escaped inside the path value: the output stays one line and
    # parses back to exactly the crafted string.
    forged_path = '/x client_ip=9.9.9.9\n{"type":"http_request","status":200} "'
    scope = _http_scope(path=forged_path)

    line = format_request_log_line(scope, 404, 1.0)
    record = json.loads(line)

    assert "\n" not in line
    assert record["path"] == forged_path
    assert record["status"] == 404


def test_format_request_log_line_reports_500_when_no_response_started() -> None:
    record = json.loads(format_request_log_line(_http_scope(), None, 1.0))
    assert record["status"] == 500


def test_format_request_log_line_includes_the_user_only_when_scope_state_carries_one() -> None:
    scope_without_user = _http_scope()
    scope_with_user = _http_scope()
    scope_with_user["state"] = {AUTHENTICATED_USER_STATE_KEY: "st-user-full-id-1234"}

    anonymous_record = json.loads(format_request_log_line(scope_without_user, 200, 1.0))
    authenticated_record = json.loads(format_request_log_line(scope_with_user, 200, 1.0))

    assert "user" not in anonymous_record
    assert authenticated_record["user"] == "st-user-full-id-1234"


def test_format_request_log_line_ignores_a_non_string_user_value() -> None:
    scope = _http_scope()
    scope["state"] = {AUTHENTICATED_USER_STATE_KEY: 12345}

    record = json.loads(format_request_log_line(scope, 200, 1.0))

    assert "user" not in record


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
            # Stash an identity mid-routing: the middleware runs outermost and
            # the scope dict is shared down the stack, so the user id set here
            # must land on the logged line.
            scope.setdefault("state", {})[AUTHENTICATED_USER_STATE_KEY] = "st-user-abcdef"
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    middleware = RequestLoggingMiddleware(_RespondingApp(), line_sink=lines.append)

    scope = _http_scope(client=("198.51.100.7", 41000))
    _run_coroutine_synchronously(middleware(scope, None, _send))

    # The response passed through untouched and exactly one line was logged.
    assert [message["type"] for message in sent_messages] == ["http.response.start", "http.response.body"]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == 403
    assert record["client_ip"] == "198.51.100.7"
    assert record["user"] == "st-user-abcdef"


def test_middleware_logs_a_500_line_when_the_app_raises() -> None:
    lines: list[str] = []

    class _CrashingApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            raise RuntimeError("boom-4189")

    middleware = RequestLoggingMiddleware(_CrashingApp(), line_sink=lines.append)

    with pytest.raises(RuntimeError, match="boom-4189"):
        middleware(_http_scope(), None, None).send(None)
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == 500


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


def test_format_request_log_line_stamps_the_minds_env_when_deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = {"type": "http", "method": "GET", "path": "/health/liveness", "headers": []}

    monkeypatch.setenv("MINDS_ENV_NAME", "dev-alice")
    stamped = json.loads(format_request_log_line(scope, 200, 1.0))
    monkeypatch.delenv("MINDS_ENV_NAME")
    unstamped = json.loads(format_request_log_line(scope, 200, 1.0))

    assert stamped["minds_env"] == "dev-alice"
    assert "minds_env" not in unstamped
