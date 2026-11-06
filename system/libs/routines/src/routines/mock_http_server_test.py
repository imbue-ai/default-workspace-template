"""A local HTTP server the chat app and notifier adapters are pointed at in tests."""

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class RecordedRequest(FrozenModel):
    """One request the fake server took."""

    method: str = Field(description="The HTTP method")
    path: str = Field(description="The path with its query")
    headers: dict[str, str] = Field(description="The headers, lower-cased")
    body: dict[str, object] | None = Field(description="The JSON body; None when there was none")


class FakeHttpServer(MutableModel):
    """A local server answering each (method, path) from a script, in order, and keeping what it was asked."""

    url: str = Field(frozen=True, description="Where it listens")
    answers: dict[tuple[str, str], list[tuple[int, object]]] = Field(
        default_factory=dict, description="The (status, JSON body) answers per route; the last repeats"
    )
    requests: list[RecordedRequest] = Field(default_factory=list, description="Every request, in order")

    def answer(self, method: str, path: str, *responses: tuple[int, object]) -> None:
        self.answers[(method, path)] = list(responses)

    def take(
        self, method: str, path: str, headers: dict[str, str], body: dict[str, object] | None
    ) -> tuple[int, object]:
        self.requests.append(RecordedRequest(method=method, path=path, headers=headers, body=body))
        scripted = self.answers.get((method, path))
        if not scripted:
            return 404, {"detail": f"no answer scripted for {method} {path}"}
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]


@contextmanager
def serving_fake_http_server() -> Generator[FakeHttpServer, None, None]:
    """Serve a FakeHttpServer on a loopback port for the length of the block."""
    holder: list[FakeHttpServer] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            status, answer = holder[0].take(
                self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, body
            )
            payload = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder.append(FakeHttpServer(url=f"http://127.0.0.1:{server.server_port}"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()
