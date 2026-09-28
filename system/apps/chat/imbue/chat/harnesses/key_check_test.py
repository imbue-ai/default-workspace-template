import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from imbue.chat.harnesses.key_check import CheckedProvider
from imbue.chat.harnesses.key_check import KeyCheck
from imbue.chat.harnesses.key_check import check_key

_STATUS_BY_KEY = {"good": 200, "bad": 401, "no-access": 403, "overloaded": 529}


class _ModelsEndpoint(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        is_asked_properly = self.path == "/v1/models" and self.headers.get("anthropic-version") == "2023-06-01"
        status = _STATUS_BY_KEY.get(self.headers.get("x-api-key", ""), 500) if is_asked_properly else 404
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def proxy_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("api_key", "verdict"),
    [
        ("good", KeyCheck.ACCEPTED),
        ("bad", KeyCheck.REJECTED),
        ("no-access", KeyCheck.UNCHECKED),
        ("overloaded", KeyCheck.UNCHECKED),
    ],
)
def test_the_providers_answer_decides(proxy_url: str, api_key: str, verdict: KeyCheck) -> None:
    assert check_key(CheckedProvider.ANTHROPIC, api_key, proxy_url) is verdict


def test_a_provider_that_cannot_be_reached_leaves_the_key_unchecked() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), _ModelsEndpoint) as server:
        closed = f"http://127.0.0.1:{server.server_address[1]}"

    assert check_key(CheckedProvider.ANTHROPIC, "good", closed) is KeyCheck.UNCHECKED
