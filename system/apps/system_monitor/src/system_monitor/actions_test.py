"""Tests for what the Stop buttons do: a chat through the chat app, a chat's code checkers by signal, and one
of the user's apps through supervisord."""

import json
import subprocess
import sys
import threading
import xmlrpc.client
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from oom_priority.testing import fake_supervisord

from system_monitor import actions
from system_monitor.programs import Process


class _ChatAppHandler(BaseHTTPRequestHandler):
    """A stand-in for the chat app's stop route: 200 for one chat, a refusal with a reason for another."""

    asked: list[str] = []

    def do_POST(self) -> None:
        _ChatAppHandler.asked.append(self.path)
        if self.path == "/api/chats/chat-ok/stop":
            body, status = {"status": "ok"}, 200
        else:
            body, status = {"detail": "Chat 'chat-missing' not found"}, 404
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def chat_app() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _ChatAppHandler)
    serving = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}
    )
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()


def _registry_with_chat_app(tmp_path: Path, url: str) -> Path:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        f'[[apps]]\nname = "chat"\nprogram = "chat"\nurl = "{url}"\n\n'
        '[[apps]]\nname = "budget"\nprogram = "budget"\nurl = "http://127.0.0.1:1"\n'
    )
    return registry


def test_the_app_url_is_read_from_the_registry_row_of_its_program(
    tmp_path: Path,
) -> None:
    registry = _registry_with_chat_app(tmp_path, "http://127.0.0.1:8010")

    assert actions.app_url("chat", registry) == "http://127.0.0.1:8010"
    assert actions.app_url("notes", registry) is None
    assert actions.app_url("chat", tmp_path / "missing.toml") is None


def test_a_chat_is_stopped_through_the_chat_app_and_its_refusal_is_relayed(
    tmp_path: Path, chat_app: str
) -> None:
    registry = _registry_with_chat_app(tmp_path, chat_app)
    _ChatAppHandler.asked = []

    actions.stop_chat("chat-ok", registry)
    with pytest.raises(actions.StopRefusedError) as refused:
        actions.stop_chat("chat-missing", registry)
    # An id is one path segment however it is spelled.
    with pytest.raises(actions.StopRefusedError):
        actions.stop_chat("odd/id?", registry)

    assert (refused.value.status, refused.value.detail) == (
        404,
        "Chat 'chat-missing' not found",
    )
    assert _ChatAppHandler.asked == [
        "/api/chats/chat-ok/stop",
        "/api/chats/chat-missing/stop",
        "/api/chats/odd%2Fid%3F/stop",
    ]


def test_a_chat_cannot_be_stopped_without_a_reachable_chat_app(tmp_path: Path) -> None:
    with pytest.raises(actions.StopRefusedError) as unregistered:
        actions.stop_chat("chat-ok", tmp_path / "missing.toml")
    assert unregistered.value.status == 503

    # Registered, but nothing answers there.
    registry = _registry_with_chat_app(tmp_path, "http://127.0.0.1:1")
    with pytest.raises(actions.StopRefusedError) as unreachable:
        actions.stop_chat("chat-ok", registry)
    assert unreachable.value.status == 502


def test_a_chats_code_checkers_are_told_to_stop_and_a_chat_without_any_is_refused() -> (
    None
):
    # A real process to signal, under a made-up chat whose pid exists only in the walk.
    checker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal; signal.pause()  # fake pyright-langserver",
        ]
    )
    try:
        procs = {
            99901: Process(
                99901,
                1,
                0,
                ["claude", "--settings", "/home/user/.mngr/agents/agent-abc/x"],
            ),
            checker.pid: Process(
                checker.pid, 99901, 0, ["node", "pyright-langserver", "--stdio"]
            ),
        }

        stopped = actions.stop_checkers("agent-abc", procs, {})

        assert stopped == 1
        assert checker.wait(timeout=10) == -15
    finally:
        if checker.poll() is None:
            checker.kill()
    with pytest.raises(actions.StopRefusedError) as refused:
        actions.stop_checkers("agent-nobody", procs, {})
    assert refused.value.status == 404


def test_only_the_users_own_apps_are_quit_and_through_supervisord() -> None:
    asked: list[object] = []

    def stop_process(name: object, wait: object) -> bool:
        asked.append(name)
        if name == "notes":
            raise xmlrpc.client.Fault(10, "BAD_NAME: notes")
        return True

    with fake_supervisord(stopProcess=stop_process) as sock:
        apps = {"budget": "Budget", "notes": "Notes"}
        actions.stop_app("budget", apps, sock)
        with pytest.raises(actions.StopRefusedError) as unknown:
            actions.stop_app("notes", apps, sock)
        with pytest.raises(actions.StopRefusedError) as built_in:
            actions.stop_app("chat", apps, sock)
    assert unknown.value.status == 502 and "Notes" in unknown.value.detail
    assert built_in.value.status == 403
    # The built-in program was never asked about.
    assert asked == ["budget", "notes"]


def test_an_unknown_kind_is_refused_before_anything_runs() -> None:
    with pytest.raises(actions.StopRefusedError) as refused:
        actions.stop("service", "sshd")
    assert refused.value.status == 400
