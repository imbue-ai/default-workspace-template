"""Tests for smoketest_app.py."""

import http.server
import json
import socketserver
import threading
import time
from pathlib import Path

import pytest
import smoketest_app

_SCRIPT = Path(__file__).parent / "smoketest_app.py"


def test_resolve_target_from_port(tmp_path: Path) -> None:
    port, name, runner = smoketest_app._resolve_target("8085", tmp_path)
    assert port == 8085
    assert name is None
    assert runner is None


def test_resolve_target_from_url(tmp_path: Path) -> None:
    port, name, runner = smoketest_app._resolve_target("http://127.0.0.1:9090/test", tmp_path)
    assert port == 9090
    assert name is None
    assert runner is None


def test_resolve_target_from_apps_toml(tmp_path: Path) -> None:
    state_dir = tmp_path / "data/.state"
    state_dir.mkdir(parents=True)
    (state_dir / "apps.toml").write_text(
        '[[apps]]\nname = "my-app"\nurl = "http://localhost:8123"\n'
    )
    port, name, runner = smoketest_app._resolve_target("my-app", tmp_path)
    assert port == 8123
    assert name == "my-app"


def test_resolve_target_from_supervisord_conf(tmp_path: Path) -> None:
    conf = tmp_path / "system/supervisord.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        '[program:custom-app]\n'
        'command=python3 system/services/oom_priority/bin/oom_tag_service.py user '
        'bash -c "python3 system/scripts/forward_port.py --url http://localhost:8456 && custom-app"\n'
    )
    port, name, runner = smoketest_app._resolve_target("custom-app", tmp_path)
    assert port == 8456
    assert name == "custom-app"


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "pid": 1234, "started_at": time.time()}).encode())
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!doctype html><html><body><h1>Welcome to My App</h1></body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_poll_http_success_and_marker() -> None:
    server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        code, body = smoketest_app._poll_http(url, marker="Welcome to My App", timeout=2.0, interval=0.05)
        assert code == 200
        assert "Welcome to My App" in body
    finally:
        server.shutdown()
        server.server_close()


def test_poll_http_missing_marker_raises() -> None:
    server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/"
        with pytest.raises(SystemExit) as exc_info:
            smoketest_app._poll_http(url, marker="NonExistentText", timeout=0.2, interval=0.05)
        assert "marker 'NonExistentText' was not found" in str(exc_info.value)
    finally:
        server.shutdown()
        server.server_close()
