"""Fixtures for the layout.py tests: the wait-stable bypass, a registry file, and a fake shell over loopback."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import tomlkit

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout_for_fixtures", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


@pytest.fixture(autouse=True)
def _skip_wait_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the wait-stable poll; tests that want it remove the variable again."""
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")


def _write_apps_toml(path: Path, rows: dict[str, bool]) -> None:
    """A registry with one row per name; the value says whether the app has instances."""
    doc = tomlkit.document()
    apps = tomlkit.aot()
    for name, has_instances in rows.items():
        entry = tomlkit.table()
        entry["name"] = name
        entry["url"] = f"http://localhost:9000/{name}"
        entry["instances"] = has_instances
        if has_instances:
            actions = tomlkit.aot()
            action = tomlkit.table()
            action["id"] = "new"
            action["label"] = f"New {name}"
            actions.append(action)
            entry["actions"] = actions
        apps.append(entry)
    doc["apps"] = apps
    path.write_text(tomlkit.dumps(doc))


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "apps.toml"
    _write_apps_toml(path, {"files": False, "terminal": True, "chat": True})
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(path))
    return path


class _FakeShellHandler(BaseHTTPRequestHandler):
    """The shell's REST routes the relay verbs and the shortcut commands ride."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server: Any = self.server
        if self.path == "/api/projects":
            self._respond(200, {"projects": server.projects})
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        server: Any = self.server
        body_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(body_length) or b"{}")
        server.posted.append((self.path, body))
        if self.path == "/api/layout/broadcast":
            self._respond(200, {"ok": True, "clients": server.context_clients})
            return
        if self.path.startswith("/api/projects/") and "/shortcuts" in self.path:
            self._respond(
                200,
                {"id": self.path.split("/")[3], "shortcuts": server.shortcuts_answer},
            )
            return
        if self.path.startswith("/api/apps/"):
            if server.relay_refuses:
                self._respond(404, {"detail": "no such instance"})
            elif self.path.endswith("/delete"):
                self._respond(204, {})
            else:
                self._respond(
                    200,
                    {
                        "instance": {
                            "key": self.path.split("/")[5],
                            "title": body.get("title", ""),
                        }
                    },
                )
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})


@pytest.fixture
def fake_shell(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeShellHandler)
    server.projects = []
    server.posted = []
    server.shortcuts_answer = []
    server.context_clients = []
    server.relay_refuses = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        layout.ENV_WORKSPACE_URL, f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
