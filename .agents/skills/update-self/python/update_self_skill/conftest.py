import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from update_self_skill.agents_testing import FakeAgents
from update_self_skill.messenger_testing import RecordingMessengers
from update_self_skill.update_layout import APPS_REGISTRY_PATH


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


class FakeChatAppServer(ThreadingHTTPServer):
    """The fake chat app's server and the scripts and records its handler shares with a test."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(("127.0.0.1", 0), _FakeChatAppHandler)
        self.workspace = workspace
        self.answers: list[tuple[int, object]] = [(200, {"chats": []})]
        self.list_reads = 0
        self.interrupt_answers: dict[str, list[tuple[int, object]]] = {}
        self.interrupts: list[tuple[str, object]] = []


class _FakeChatAppHandler(BaseHTTPRequestHandler):
    """The chat app's ``GET /api/chats`` and ``POST /api/chats/<id>/interrupt``, answering scripts."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, FakeChatAppServer)
        if self.path != "/api/chats":
            self._respond(404, {"detail": f"unknown path {self.path}"})
            return
        server.list_reads += 1
        # The last scripted answer repeats, so a test scripts only the transitions it is about.
        self._respond(
            *(server.answers.pop(0) if len(server.answers) > 1 else server.answers[0])
        )

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, FakeChatAppServer)
        prefix, suffix = "/api/chats/", "/interrupt"
        if not (self.path.startswith(prefix) and self.path.endswith(suffix)):
            self._respond(404, {"detail": f"unknown path {self.path}"})
            return
        chat_id = self.path[len(prefix) : -len(suffix)]
        body_length = int(self.headers.get("Content-Length", "0"))
        server.interrupts.append(
            (chat_id, json.loads(self.rfile.read(body_length) or b"{}"))
        )
        answers = server.interrupt_answers.get(chat_id, [(200, {"status": "ok"})])
        self._respond(*(answers.pop(0) if len(answers) > 1 else answers[0]))


@pytest.fixture
def fake_chat_app(tmp_path: Path) -> Iterator[FakeChatAppServer]:
    """A chat app over loopback, registered under the ``chat`` row of ``tmp_path``'s registry.

    ``server.answers`` is the sequence of ``(status, body)`` its chat list gives, the last one
    repeating; ``server.list_reads`` counts the reads. ``server.interrupt_answers`` maps a chat id
    to the sequence its interrupt route gives (default a 200), and ``server.interrupts`` records
    each ``(chat_id, body)`` posted there. ``server.workspace`` is the workspace root, the same
    ``tmp_path`` the other fixtures here use.
    """
    server = FakeChatAppServer(tmp_path)
    registry = server.workspace / APPS_REGISTRY_PATH
    registry.parent.mkdir(parents=True, exist_ok=True)
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


@pytest.fixture
def fake_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAgents:
    """The workspace's agents behind a fake ``mngr`` on PATH, with a recording chat messenger
    in the ``tmp_path`` tree; the lead is ``update-to-minds-v0-9-0`` (``agent-lead``)."""
    bin_dir = tmp_path / "fake-agents-bin"
    agents = FakeAgents(bin_dir, tmp_path, tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-lead")
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-to-minds-v0-9-0")
    monkeypatch.setenv("MINDS_CHAT_ID", "agent-lead")
    return agents
