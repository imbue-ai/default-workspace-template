import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Final

from flask import Flask
from flask import Response
from werkzeug.serving import make_server

from skills_app.data_types import IndexedSkill


def indexed_skill(source: str, name: str, installs: int) -> IndexedSkill:
    """A match as skills.sh answers it, its page at owner/repo/name."""
    return IndexedSkill(name=name, source=source, installs=installs, page_id=f"{source}/{name}")


class LocalSkillIndex:
    """A stand-in for skills.sh on a free local port: answers its search path with ``answer`` and ``status``."""

    def __init__(self) -> None:
        self.answer: object = {"skills": []}
        self.status = 200
        self.requested_paths: list[str] = []
        index = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index.requested_paths.append(self.path)
                body = json.dumps(index.answer).encode()
                self.send_response(index.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"


# Stands in for the workspace shell in browser tests: frames the Skills page at ``?path=`` from the origin it is given,
# sends it the handshake each time the frame loads (as the shell does), and keeps every message the page posts in
# ``window.messages``. Served from its own port, so the page is framed across origins as it is in the workspace.
_SHELL_STAND_IN_PAGE: Final[str] = """<!doctype html>
<html><body style="margin:0">
<iframe id="app" style="display:block;border:0;width:100vw;height:100vh"></iframe>
<script>
  window.messages = [];
  const frame = document.getElementById("app");
  window.addEventListener("message", (event) => {
    if (event.source === frame.contentWindow) window.messages.push(event.data);
  });
  frame.addEventListener("load", () => {
    frame.contentWindow.postMessage({ type: "shell:handshake", clientId: "test-client", windowId: "test-window", app: "skills" }, "*");
  });
  frame.src = "{origin}" + new URLSearchParams(location.search).get("path");
</script>
</body></html>"""


def shell_stand_in_app(skills_origin: str) -> Flask:
    """A one-page app that frames the Skills page as the workspace shell would; see _SHELL_STAND_IN_PAGE."""
    app = Flask("shell_stand_in")

    @app.get("/")
    def page() -> Response:
        return Response(_SHELL_STAND_IN_PAGE.replace("{origin}", skills_origin), mimetype="text/html")

    return app


@contextmanager
def serve_in_background(app: Flask) -> Generator[str]:
    """Serve ``app`` on a free loopback port on a daemon thread, yielding its origin."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
