"""Fixtures for the scripts' tests: a fake chat app and a fake ``mngr`` for message_chat.py and
run_in_background.py, a marker root of its own for every test, so no test marks a chat busy
in the real checkout, and the exited pid and worktree the marker tests build on."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from script_modules_testing import background_tasks, message_chat


@pytest.fixture(autouse=True)
def _isolate_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the ambient agent identity from every test for these scripts.

    A script that names the caller's own chat reads ``MINDS_CHAT_ID``, falling back to
    ``MNGR_AGENT_ID``, so a test asserting on an address it derives is reading its own inputs
    only if BOTH halves are cleared -- clearing the chat id alone leaves the fallback steered by
    whatever agent is running the suite. Cleared here rather than per test, so a test that needs
    an identity has to say so explicitly.
    """
    monkeypatch.delenv("MINDS_CHAT_ID", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)


@pytest.fixture(autouse=True)
def background_task_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The background-task marker root every script (and every subprocess a test starts) writes
    and reads, in place of the checkout's ``data/.apps/chat/background_tasks``."""
    root = tmp_path / "background_tasks"
    monkeypatch.setenv(background_tasks.MARKER_ROOT_ENV, str(root))
    return root


@pytest.fixture
def exited_pid() -> int:
    """The pid of a process that has already exited, which a marker naming it is stale by."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def main_checkout_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A git main checkout with one commit, and a worker's worktree of it, as ``(main, worktree)``."""
    main = tmp_path / "workspace"
    main.mkdir()
    worktree = tmp_path / "worktrees" / "worker"
    for args in (
        ("init", "-q", "-b", "main"),
        (
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "root",
        ),
        ("worktree", "add", "-q", "-b", "worker", str(worktree)),
    ):
        subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)
    return main, worktree


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
    """The chat app's send route, answering a scripted sequence of verdicts, and its GET routes,
    answering what ``server.get_answers`` holds per path (404 for any other)."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        server: Any = self.server
        status, answer_body = server.get_answers.get(
            self.path, (404, {"detail": "not found"})
        )
        payload = json.dumps(answer_body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
    repeating; ``server.get_answers`` maps a GET path to its ``(status, body)``; ``server.posted``
    is every ``(path, body)`` it received; ``server.drop_connections``
    makes it read each request and then close the connection without answering.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeChatAppHandler)
    server.answers = [(200, {"status": "ok"})]
    server.get_answers = {}
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
    """A ``mngr`` on PATH that records its argv and the message file's contents, prints
    ``$FAKE_MNGR_STDOUT`` (default nothing), then exits with the code in ``$FAKE_MNGR_EXIT``
    (default 0). Returns the file the record is written to."""
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
        "sys.stdout.write(os.environ.get('FAKE_MNGR_STDOUT', ''))\n"
        "raise SystemExit(int(os.environ.get('FAKE_MNGR_EXIT', '0')))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return record
