"""Fixtures for the scripts' tests: a registry file and a fake shell over loopback for
layout.py, and a fake chat app and a fake ``mngr`` for message_chat.py."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import tomlkit
from layout_testing import desktop_answer


def _load_script_module(module_name: str, filename: str) -> Any:
    """Import one of the scripts beside this file under ``module_name`` (they are not a package)."""
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).parent / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


layout = _load_script_module("layout_for_fixtures", "layout.py")
message_chat = _load_script_module("message_chat_for_fixtures", "message_chat.py")
seed_welcome_chat = _load_script_module("seed_welcome_chat_for_fixtures", "seed_welcome_chat.py")
welcome_count = _load_script_module("welcome_count_for_fixtures", "welcome_count.py")


def _write_apps_toml(path: Path, rows: dict[str, tuple[str, ...]]) -> None:
    """A registry with one row per name, shaped as ``forward_port.py`` writes it; the value is the
    app's declared launch path ids (none for an app that opens at its root). An app declaring
    more than one gets a ``default_shortcut`` on its last one."""
    doc = tomlkit.document()
    apps = tomlkit.aot()
    for name, launch_ids in rows.items():
        entry = tomlkit.table()
        entry["name"] = name
        entry["url"] = f"http://localhost:9000/{name}"
        if launch_ids:
            launch_paths = tomlkit.aot()
            for launch_id in launch_ids:
                launch_path = tomlkit.table()
                launch_path["id"] = launch_id
                launch_path["label"] = f"{launch_id.capitalize()} {name}"
                launch_path["path"] = f"/{launch_id}"
                launch_paths.append(launch_path)
            entry["launch_paths"] = launch_paths
        if len(launch_ids) > 1:
            default_shortcut = tomlkit.inline_table()
            default_shortcut["launch"] = launch_ids[-1]
            default_shortcut["mode"] = "focus"
            entry["default_shortcut"] = default_shortcut
        apps.append(entry)
    doc["apps"] = apps
    path.write_text(tomlkit.dumps(doc))


@pytest.fixture(autouse=True)
def _isolate_own_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the chat id the chat app stamps on its agents, so a test that asserts on the
    requester layout.py derives from MNGR_AGENT_ID is not steered by the developer's own."""
    monkeypatch.delenv(layout.ENV_MINDS_CHAT_ID, raising=False)


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "apps.toml"
    _write_apps_toml(path, {"files": (), "terminal": ("new",), "chat": ("subagent", "new"), "browser": ("new",)})
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(path))
    return path


class _FakeShellHandler(BaseHTTPRequestHandler):
    """The shell's op route and inventory document, answering what the fixture holds."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, body: dict[str, Any] | str) -> None:
        """A dict is the shell's JSON answer; a str is a page a proxy in front of it might answer with instead."""
        is_json = isinstance(body, dict)
        payload = (json.dumps(body) if is_json else str(body)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if is_json else "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server: Any = self.server
        if self.path == "/api/inventory":
            self._respond(
                200,
                {
                    "apps": server.inventory_apps,
                    "desktops": server.inventory_desktops,
                    "clients": server.inventory_clients,
                },
            )
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        server: Any = self.server
        body_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(body_length) or b"{}")
        server.posted.append((self.path, body))
        server.posted_content_types.append(self.headers.get("Content-Type"))
        if self.path == "/api/layout/broadcast":
            if body.get("op") == "context":
                self._respond(200, {"ok": True, "clients": server.context_clients})
            elif body.get("op") == "refresh":
                self._respond(200, {"ok": True, "target_client_id": server.refresh_target})
            elif server.op_refusal is not None:
                self._respond(*server.op_refusal)
            else:
                self._respond(200, server.op_answer)
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})


@pytest.fixture
def fake_shell(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A shell over loopback: ``server.posted`` is every ``(path, body)`` it received (``posted_content_types``
    the matching ``Content-Type`` headers); ``server.op_answer`` is what a desktop op answers
    (``server.op_refusal`` a ``(status, body)`` refusal instead, the body a dict or a page's text), and the ``inventory_*`` lists are the
    inventory document."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeShellHandler)
    server.posted = []
    server.posted_content_types = []
    server.context_clients = []
    server.inventory_apps = []
    server.inventory_desktops = []
    server.inventory_clients = []
    server.op_answer = desktop_answer()
    server.op_refusal = None
    server.refresh_target = "c1"
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


@pytest.fixture(autouse=True)
def _clear_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the runner's GitHub Actions environment out of these tests.

    ``check_changelog_entries`` reads the branch and the diff base from the
    process environment -- ``resolve_diff_base`` takes ``CHANGELOG_BASE_REF``
    then ``GITHUB_BASE_REF``, and ``detect_branch`` takes ``GITHUB_HEAD_REF``
    then ``GITHUB_REF_NAME`` -- while its tests run it against throwaway repos
    built in ``tmp_path``. Under CI those variables describe the *real* PR, so
    they answer questions about a repo the test never created.

    The base bites hardest: a stacked PR's base branch does not exist in a
    throwaway repo, and ``resolve_diff_base`` deliberately raises rather than
    falling back to ``main`` for an unresolvable named base. All four are
    cleared regardless, since the branch pair is read by the same module.

    The tests that exercise a named base set their own value, which still wins
    because that happens inside the test body.
    """
    for var in (
        "CHANGELOG_BASE_REF",
        "GITHUB_BASE_REF",
        "GITHUB_HEAD_REF",
        "GITHUB_REF_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


class _FakeChatAppHandler(BaseHTTPRequestHandler):
    """The chat app's send route, answering a scripted sequence of verdicts."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        server: Any = self.server
        body_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(body_length) or b"{}")
        server.posted.append((self.path, body))
        if server.drop_connections:
            # The request was taken; the socket closes with no answer at all.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        # The last scripted answer repeats, so a test scripts only the transitions it is about.
        status, answer_body = (
            server.answers.pop(0) if len(server.answers) > 1 else server.answers[0]
        )
        payload = json.dumps(answer_body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_chat_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A chat app over loopback, registered under the ``chat`` row of a registry the script reads.

    ``server.answers`` is the sequence of ``(status, body)`` the send route gives, the last one
    repeating; ``server.posted`` is every ``(path, body)`` it received; ``server.drop_connections``
    makes it read each request and then close the connection without answering.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeChatAppHandler)
    server.answers = [(200, {"status": "ok"})]
    server.posted = []
    server.drop_connections = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry = tmp_path / "apps.toml"
    registry.write_text(
        f'[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:{server.server_address[1]}"\n'
    )
    monkeypatch.setenv(message_chat.ENV_APPS_FILE, str(registry))
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fake_mngr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``mngr`` on PATH that records its argv and the message file's contents, then exits with
    the code in ``$FAKE_MNGR_EXIT`` (default 0). Returns the file the record is written to."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    record = tmp_path / "mngr-calls.json"
    fake = bin_dir / "mngr"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "text = open(argv[argv.index('--message-file') + 1]).read() if '--message-file' in argv else None\n"
        f"with open({str(record)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': argv, 'text': text}) + '\\n')\n"
        "raise SystemExit(int(os.environ.get('FAKE_MNGR_EXIT', '0')))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return record
