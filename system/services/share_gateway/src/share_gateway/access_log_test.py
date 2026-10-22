import logging
import threading
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from share_gateway.access_log import QueryOmittingRequestHandler


@contextmanager
def _serve_through_the_handler() -> Generator[str, None, None]:
    @Request.application
    def app(request: Request) -> Response:
        return Response("ok")

    server = make_server("127.0.0.1", 0, app, request_handler=QueryOmittingRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _logged_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "werkzeug"]


def test_the_login_callbacks_token_never_reaches_the_access_log(caplog: pytest.LogCaptureFixture) -> None:
    token = f"eyJ{uuid4().hex}.{uuid4().hex}.{uuid4().hex}"
    with caplog.at_level(logging.INFO, logger="werkzeug"), _serve_through_the_handler() as base_url:
        urllib.request.urlopen(f"{base_url}/_auth/callback?token={token}&state={uuid4().hex}", timeout=10).read()

    lines = _logged_lines(caplog)
    assert len(lines) == 1
    assert '"GET /_auth/callback?<query omitted> HTTP/1.1" 200' in lines[0]
    assert token not in lines[0]


def test_a_request_without_a_query_is_logged_by_its_whole_path(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="werkzeug"), _serve_through_the_handler() as base_url:
        urllib.request.urlopen(f"{base_url}/_auth/verify", timeout=10).read()

    (line,) = _logged_lines(caplog)
    assert '"GET /_auth/verify HTTP/1.1" 200' in line
