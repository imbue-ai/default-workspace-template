import logging
import socket
import urllib.request
from uuid import uuid4

import pytest
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from share_gateway.access_log import QueryOmittingRequestHandler
from share_gateway.testing import serve_wsgi


@Request.application
def _ok_app(request: Request) -> Response:
    return Response("ok")


def _get_through_the_handler(path_and_query: str) -> None:
    with serve_wsgi(_ok_app, request_handler=QueryOmittingRequestHandler) as base_url:
        with urllib.request.urlopen(f"{base_url}{path_and_query}", timeout=10) as response:
            response.read()


def _logged_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "werkzeug"]


def test_the_login_callbacks_token_never_reaches_the_access_log(caplog: pytest.LogCaptureFixture) -> None:
    token = f"eyJ{uuid4().hex}.{uuid4().hex}.{uuid4().hex}"
    with caplog.at_level(logging.INFO, logger="werkzeug"):
        _get_through_the_handler(f"/_auth/callback?token={token}&state={uuid4().hex}")

    lines = _logged_lines(caplog)
    assert len(lines) == 1
    assert '"GET /_auth/callback?<query omitted> HTTP/1.1" 200' in lines[0]
    assert token not in lines[0]


def test_a_request_without_a_query_is_logged_by_its_whole_path(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="werkzeug"):
        _get_through_the_handler("/_auth/verify")

    (line,) = _logged_lines(caplog)
    assert '"GET /_auth/verify HTTP/1.1" 200' in line


def test_control_characters_in_the_request_line_are_escaped_in_the_access_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="werkzeug"):
        with serve_wsgi(_ok_app, request_handler=QueryOmittingRequestHandler) as base_url:
            host, port = base_url.removeprefix("http://").rsplit(":", 1)
            # urllib refuses a control character in a URL, so the request line goes out over a raw socket.
            with socket.create_connection((host, int(port)), timeout=10) as connection:
                connection.sendall(b"GET /a\x1b[31mb\x7f\\c HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                while connection.recv(4096):
                    pass

    (line,) = _logged_lines(caplog)
    assert '"GET /a\\x1b[31mb\\x7f\\\\c HTTP/1.1" 200' in line
    assert "\x1b" not in line
