import os
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

_HTTP_FORBIDDEN = 403
_FAILING_SUPERVISORCTL = """\
#!/bin/sh
echo "no supervisord runs beside a test's tree" >&2
exit 2
"""


class RepositoryHostStandIn(ThreadingHTTPServer):
    """A local HTTP server standing in for a repository host: it answers every request with ``status`` and keeps
    the path of each one."""

    status: int
    requested_paths: list[str]

    @property
    def repository_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/someone/notes"


class _RepositoryHostHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        assert isinstance(self.server, RepositoryHostStandIn)
        self.server.requested_paths.append(self.path)
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture(autouse=True)
def no_registry_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test scans a tree of its own: without this, a workspace that points ``MINDS_APPS_FILE`` at its live
    registry would have the port scan read that registry's ports."""
    monkeypatch.delenv("MINDS_APPS_FILE", raising=False)


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads a repository through the gateway of the workspace it happens to run in."""
    for name in ("LATCHKEY_GATEWAY", "LATCHKEY_GATEWAY_PASSWORD", "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_supervisord(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No test reaches the supervisord of the workspace it happens to run in: ``supervisorctl`` finds that one
    from any directory, and would have it apply whatever program files are waiting there."""
    directory = tmp_path_factory.mktemp("no-supervisord")
    stub = directory / "supervisorctl"
    stub.write_text(_FAILING_SUPERVISORCTL)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def repository_host() -> Generator[RepositoryHostStandIn, None, None]:
    """A running stand-in for a repository host, which refuses every read (403) until a test sets its status."""
    server = RepositoryHostStandIn(("127.0.0.1", 0), _RepositoryHostHandler)
    server.status = _HTTP_FORBIDDEN
    server.requested_paths = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()
