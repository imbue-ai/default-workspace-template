"""A stand-in for the chat app, for the agent-restart tests.

It answers the chat list (``GET /api/chats``) and the interrupt route
(``POST /api/chats/<id>/interrupt``) over loopback from scripts a test sets, and records
each interrupt posted to it.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class FakeChatAppServer(ThreadingHTTPServer):
    """The fake chat app's server and the scripts and records its handler shares with a test."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(("127.0.0.1", 0), _FakeChatAppHandler)
        self.workspace = workspace
        self.answers: list[tuple[int, object]] = [(200, {"chats": []})]
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
