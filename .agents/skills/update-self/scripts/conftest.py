"""The skill's scripts import each other as siblings (the directory is ``sys.path[0]``
when ``update_self.py`` runs); put it there for the tests too."""

import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from messenger_testing import RecordingMessengers
from update_layout import APPS_REGISTRY_PATH

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True, scope="module")
def _isolate_git_config() -> Iterator[None]:
    """Keep the developer's git config out of the real-git tests.

    The ledger and recovery tests drive real ``git`` (in the test and in the
    scripts under test), and a global ``commit.gpgsign`` or ``core.hooksPath``
    would reach into every one of them. Identity is set per repo by the
    helpers, so nothing here needs the global file. Module-scoped so the repos
    a module's shared fixtures build are covered too.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(autouse=True)
def _isolate_tool_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the pinned tool home at a temporary directory for every test.

    The apply falls back to the home the build pins when it cannot resolve an
    installation from PATH, and its tests drive it with a fake PATH that
    resolves nothing -- so the fallback would otherwise read (and name in the
    argv it records) the real ``/root`` of whatever machine runs the suite.
    That is live workspace state, which is how a test came to delete the
    installation it was validating a release against.
    """
    monkeypatch.setenv("TOOL_ENV_HOME", str(tmp_path / "pinned-tool-home"))


@pytest.fixture
def recording_messengers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RecordingMessengers:
    bin_dir = tmp_path / "fake-bin"
    messengers = RecordingMessengers(bin_dir, tmp_path / "messenger-calls.jsonl")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return messengers


class _FakeChatListHandler(BaseHTTPRequestHandler):
    """The chat app's ``GET /api/chats``, answering a scripted sequence of chat lists."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        server: Any = self.server
        if self.path != "/api/chats":
            self.send_response(404)
            self.end_headers()
            return
        server.list_reads += 1
        # The last scripted answer repeats, so a test scripts only the transitions it is about.
        status, body = (
            server.answers.pop(0) if len(server.answers) > 1 else server.answers[0]
        )
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_chat_list(tmp_path: Path) -> Iterator[Any]:
    """A chat app over loopback, registered under the ``chat`` row of ``tmp_path/workspace``'s registry.

    ``server.answers`` is the sequence of ``(status, body)`` its chat list gives, the last one
    repeating; ``server.list_reads`` counts the reads; ``server.workspace`` is the workspace root.
    """
    server: Any = ThreadingHTTPServer(("127.0.0.1", 0), _FakeChatListHandler)
    server.answers = [(200, {"chats": []})]
    server.list_reads = 0
    server.workspace = tmp_path / "workspace"
    registry = server.workspace / APPS_REGISTRY_PATH
    registry.parent.mkdir(parents=True)
    registry.write_text(
        f'[[apps]]\nname = "chat"\nurl = "http://127.0.0.1:{server.server_address[1]}"\n'
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
