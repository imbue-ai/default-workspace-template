"""Test utilities shared across the gateway's test modules."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response


def set_cookies_by_name(response: Response) -> dict[str, str]:
    """The response's rendered ``Set-Cookie`` headers, keyed by cookie name."""
    return {header.split("=", 1)[0]: header for header in response.headers.getlist("Set-Cookie")}


@contextmanager
def serve_json(body: object) -> Iterator[str]:
    """Serve ``body`` as JSON at every path of a loopback server; yields the server's URL."""

    @Request.application
    def app(request: Request) -> Response:
        return Response(json.dumps(body), mimetype="application/json")

    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/share/jwks.json"
    finally:
        server.shutdown()
        thread.join()
