"""Test helpers for the scaffold: a stand-in for the workspace shell a scaffolded app fetches from."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from threading import Thread

from flask import Flask, Response, abort, request
from werkzeug.serving import make_server


class ShellStandIn:
    """A local HTTP server standing in for the workspace shell: what it answers, and what it was asked."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.body_by_path: dict[str, bytes] = {}
        self.requested_paths: list[str] = []


@contextmanager
def running_shell_stand_in() -> Generator[ShellStandIn, None, None]:
    """Serve a stand-in for the workspace shell on a free loopback port for the length of the block."""
    app = Flask("shell-stand-in", static_folder=None)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    stand_in = ShellStandIn(f"http://127.0.0.1:{server.server_port}")

    def serve(requested_path: str) -> Response:
        stand_in.requested_paths.append(request.path)
        body = stand_in.body_by_path.get(request.path)
        if body is None:
            abort(404)
        return Response(body, mimetype="application/octet-stream")

    app.add_url_rule("/<path:requested_path>", view_func=serve)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stand_in
    finally:
        server.shutdown()
        thread.join()
