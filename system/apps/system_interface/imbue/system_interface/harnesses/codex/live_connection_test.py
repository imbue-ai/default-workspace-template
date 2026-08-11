"""Unit tests for :class:`CodexLiveConnection` -- the persistent client + ledger + reader thread.

Driven over an in-memory transport (no daemon): the ``open_client`` seam injects an already-bound
:class:`CodexAppServerClient`, and the test pushes frames the background reader pumps into the
ledger. Covers the reachable-daemon build, the reader delivering a notification to the ledger's
activity callback, a clean stop, the not-reachable ``None`` return, and the reader marking the
connection not-alive when the transport closes.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection


class _LocalTransport:
    """An in-memory transport: responds to requests, lets a test push notifications, and can close."""

    def __init__(self) -> None:
        self._inbound: deque[str] = deque()
        self._responders: dict[str, Any] = {}
        self.closed = False

    def respond_result(self, method: str, result: Mapping[str, Any]) -> None:
        self._responders[method] = result

    def send(self, message: str) -> None:
        request = json.loads(message)
        result = self._responders.get(request.get("method"))
        if result is not None and "id" in request:
            self._inbound.append(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))

    def receive(self, timeout: float | None) -> str:
        if self.closed:
            raise TransportClosedError("transport closed")
        if not self._inbound:
            raise TimeoutError("no frame")
        return self._inbound.popleft()

    def push(self, frame: Mapping[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def close(self) -> None:
        self.closed = True


def _bound_client(transport: _LocalTransport) -> CodexAppServerClient:
    """A client bound to a thread, with ``thread/read`` scripted for the status seed."""
    transport.respond_result("thread/read", {"thread": {"status": {"type": "idle"}, "turns": []}})
    client = CodexAppServerClient(transport=transport)
    client.thread_id = "thread-1"
    return client


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_build_pumps_a_notification_into_the_ledger_and_stops(tmp_path: Path) -> None:
    transport = _LocalTransport()
    client = _bound_client(transport)
    activity: list[ActivityState] = []
    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=activity.append,
        model_state_path=tmp_path / "model.json",
        open_client=lambda _: client,
    )
    assert connection is not None
    assert connection.is_alive

    # A turn opens: the reader dispatches it, the client tracks the active turn, and the ledger's
    # activity callback fires THINKING (contract A6).
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "t1"}}})
    assert _wait_until(lambda: ActivityState.THINKING in activity)
    assert connection.ledger.activity_state() == ActivityState.THINKING

    connection.stop()
    assert not connection.is_alive


def test_build_returns_none_when_the_daemon_is_not_reachable(tmp_path: Path) -> None:
    def _boom(_: Path) -> CodexAppServerClient:
        raise OSError("no socket")

    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        model_state_path=tmp_path / "model.json",
        open_client=_boom,
    )
    assert connection is None


def test_reader_marks_not_alive_when_the_transport_closes(tmp_path: Path) -> None:
    transport = _LocalTransport()
    client = _bound_client(transport)
    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        model_state_path=tmp_path / "model.json",
        open_client=lambda _: client,
    )
    assert connection is not None
    # The daemon dies: the next reader poll sees a closed transport and the connection goes not-alive.
    transport.closed = True
    assert _wait_until(lambda: not connection.is_alive)
    connection.stop()
