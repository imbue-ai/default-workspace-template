"""Unit tests for :class:`CodexHarnessSession.send`'s outcome mapping.

The endpoint (and through it the frontend) trusts OK to mean the daemon ACCEPTED the
message, so the mapping is pinned over a REAL ledger and live connection driven by a
scripted transport: an accepted submit is OK; a transport that died under the submit --
the aliveness check is a background poll, so it can lag the closure -- is NOT_READY (the
endpoint's revive-retry loop rebuilds the connection and re-submits with the same
``message_id``); any other daemon refusal raises ``SendFailedError``, whose error
response is what restores the text to the composer.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.system_interface.agent_discovery import SendFailedError
from imbue.system_interface.harnesses.codex.ledger import MessageState
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection
from imbue.system_interface.harnesses.codex.session import CodexHarnessSession
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps


class _ScriptedTransport:
    """An in-memory transport: per-method scripted results/errors, a per-method closed-write
    failure, and ``TimeoutError`` on an empty inbound queue (which keeps the connection's
    background reader idling without ever marking the connection dead)."""

    def __init__(self) -> None:
        self._inbound: deque[str] = deque()
        self._results: dict[str, Mapping[str, Any]] = {}
        self._errors: dict[str, tuple[int, str]] = {}
        self.closed_write_methods: set[str] = set()

    def respond_result(self, method: str, result: Mapping[str, Any]) -> None:
        self._results[method] = result

    def respond_error(self, method: str, code: int, message: str) -> None:
        self._errors[method] = (code, message)

    def send(self, message: str) -> None:
        request = json.loads(message)
        method = request.get("method")
        if method in self.closed_write_methods:
            raise TransportClosedError("scripted: connection closed under the write")
        if "id" not in request:
            return
        error = self._errors.get(method)
        if error is not None:
            code, text = error
            self._inbound.append(
                json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": text}})
            )
            return
        self._inbound.append(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": self._results[method]}))

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError("no frame")
        return self._inbound.popleft()

    def close(self) -> None:
        pass


def _session_over(transport: _ScriptedTransport, state_dir: Path) -> tuple[CodexHarnessSession, CodexLiveConnection]:
    """A codex session whose live connection runs a REAL ledger over ``transport``."""
    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "status": {"type": "idle"}, "turns": []}})
    transport.respond_result("model/list", {"data": []})

    def open_client(_state_dir: Path) -> CodexAppServerClient:
        client = CodexAppServerClient(transport=transport)
        client.thread_id = "thread-1"
        return client

    unused: Any = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    deps = SessionDeps(
        harness=HarnessType.CODEX,
        state_dir=state_dir,
        model_state_path=state_dir / "model_state.json",
        send_to_harness=lambda text: True,
        notify_agents_changed=lambda: None,
        is_tracked=lambda: True,
        on_queue_snapshot=lambda snapshot: None,
        on_user_turn=lambda event: None,
        recompute_activity=lambda: None,
        clear_queue_state=lambda: None,
        catalog_options=lambda: (),
        build_interrupter=unused,
        build_shoulder_tap=lambda agent_info: None,
    )
    connection = CodexLiveConnection.build(
        state_dir,
        on_queue_snapshot=deps.on_queue_snapshot,
        on_user_turn=deps.on_user_turn,
        model_state_path=deps.model_state_path,
        open_client=open_client,
    )
    assert connection is not None
    session = CodexHarnessSession.build(deps)
    session._connection = connection
    return session, connection


def test_send_is_ok_when_the_daemon_accepts(tmp_path: Path) -> None:
    transport = _ScriptedTransport()
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    session, connection = _session_over(transport, tmp_path)
    try:
        assert session.send("hello", "m1") is SendOutcome.OK
        assert connection.ledger.state_of("m1") == MessageState.SENDING
    finally:
        connection.stop()


def test_send_is_not_ready_when_the_transport_died_under_the_submit(tmp_path: Path) -> None:
    """The stuck-Sending race: the connection's transport closes, the aliveness poll has not
    noticed yet, and the submit itself hits the dead socket. That is the same daemon-unreachable
    condition a failed connection build reports, so it must be the same retryable NOT_READY --
    never an OK that leaves the frontend waiting on a message the daemon never saw -- and the
    unaccepted message must leave no ledger entry behind."""
    transport = _ScriptedTransport()
    transport.closed_write_methods.add("turn/start")
    session, connection = _session_over(transport, tmp_path)
    try:
        assert session.send("hello", "m2") is SendOutcome.NOT_READY
        assert connection.ledger.state_of("m2") is None
        assert connection.ledger.reconcile_returned() == ""
    finally:
        connection.stop()


def test_send_raises_send_failed_on_a_daemon_refusal(tmp_path: Path) -> None:
    """A protocol-level refusal is not retryable-not-ready: it surfaces as ``SendFailedError``
    carrying the daemon's own words, which the endpoint's error response shows while restoring
    the text to the composer."""
    transport = _ScriptedTransport()
    transport.respond_error("turn/start", -32600, "model not available")
    session, connection = _session_over(transport, tmp_path)
    try:
        with pytest.raises(SendFailedError, match="model not available"):
            session.send("hello", "m3")
        assert connection.ledger.state_of("m3") is None
    finally:
        connection.stop()
