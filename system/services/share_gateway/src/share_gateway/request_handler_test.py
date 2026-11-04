import logging
import urllib.request
from uuid import uuid4

import pytest
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

from share_gateway.request_handler import SilentRequestHandler
from share_gateway.testing import serve_wsgi


@Request.application
def _ok_app(request: Request) -> Response:
    return Response("ok")


def test_a_request_leaves_no_access_line_so_the_login_callbacks_token_never_reaches_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = f"eyJ{uuid4().hex}.{uuid4().hex}.{uuid4().hex}"
    with caplog.at_level(logging.INFO, logger="werkzeug"):
        with serve_wsgi(_ok_app, request_handler=SilentRequestHandler) as base_url:
            with urllib.request.urlopen(f"{base_url}/_auth/callback?token={token}", timeout=10) as response:
                assert response.read() == b"ok"

    assert [record.getMessage() for record in caplog.records if record.name == "werkzeug"] == []
