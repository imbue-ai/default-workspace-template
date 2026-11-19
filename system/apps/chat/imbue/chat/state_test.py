"""Tests for what the app state is built from."""

import httpx

from imbue.chat.state import build_local_services_http_client


def test_the_local_services_client_keeps_no_cookie_a_server_sets() -> None:
    """A cookie some web server on a port set is not sent to the next port of that host, and a cookie that is
    not ASCII, which httpx cannot put in a request, does not stop the next request from being built."""
    cookies_received: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        cookies_received.append(request.headers.get("cookie"))
        return httpx.Response(404, headers=[(b"Set-Cookie", "lang=fran\u00e7ais; Path=/".encode())])

    client = build_local_services_http_client(httpx.MockTransport(handle))
    client.get("http://127.0.0.1:8080/v1/models")
    client.get("http://127.0.0.1:11434/v1/models")

    assert cookies_received == [None, None]
