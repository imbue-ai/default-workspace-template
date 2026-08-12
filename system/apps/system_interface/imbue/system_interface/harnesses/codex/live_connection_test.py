"""Unit tests for :class:`CodexLiveConnection` -- the persistent client + ledger + reader thread.

Driven over an in-memory transport (no daemon): the ``open_client`` seam injects an already-bound
:class:`CodexAppServerClient`, and the test pushes frames the background reader pumps into the
ledger. Covers the reachable-daemon build, the reader delivering a notification to the ledger's
activity callback, the reader carrying a committed ``userMessage`` through to Delivered (the point
of subscribing the connection), the default opener being the subscribing one, a clean stop, the
not-reachable ``None`` return, and the reader marking the connection not-alive when the transport
closes.
"""

from __future__ import annotations

import inspect
import json
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import ThreadInfo
from imbue.mngr_codex.app_server_client import TransportClosedError
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.ledger import MessageState
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection
from imbue.system_interface.harnesses.codex.model import open_subscribed_codex_client
from imbue.system_interface.harnesses.model import read_model_identity


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
    """A client bound to a thread, with ``thread/read`` scripted for the status seed and
    ``model/list`` scripted for the connect-time model-set cache."""
    transport.respond_result("thread/read", {"thread": {"status": {"type": "idle"}, "turns": []}})
    transport.respond_result("model/list", {"data": []})
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
        on_user_turn=lambda event: None,
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


def test_build_defaults_to_the_subscribing_opener() -> None:
    # The whole point of this pass: the persistent connection must RESUME (subscribe), not bind.
    # A regression to the bind opener would silently make the ledger deaf to item/turn events.
    default_opener = inspect.signature(CodexLiveConnection.build).parameters["open_client"].default
    assert default_opener is open_subscribed_codex_client


def test_reader_carries_a_committed_user_message_through_to_delivered(tmp_path: Path) -> None:
    # The subscription payoff: a send opens a turn (Sending), and when the daemon commits the
    # userMessage the background reader pumps item/completed into the ledger, which delivers it --
    # exactly the item/completed frame a bound (unsubscribed) connection never used to receive.
    transport = _LocalTransport()
    client = _bound_client(transport)
    transport.respond_result("turn/start", {"turn": {"id": "t1"}})
    user_turns: list[dict[str, Any]] = []
    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        on_user_turn=user_turns.append,
        model_state_path=tmp_path / "model.json",
        open_client=lambda _: client,
    )
    assert connection is not None
    client_id = connection.ledger.send("hello")
    assert connection.ledger.state_of(client_id) == MessageState.SENDING

    transport.push(
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "userMessage",
                    "id": "u1",
                    "clientId": client_id,
                    "content": [{"type": "text", "text": "hello"}],
                }
            },
        }
    )
    assert _wait_until(lambda: connection.ledger.state_of(client_id) == MessageState.DELIVERED)
    # The subscribed ledger OWNS the live user-turn: on commit it emits it to the transcript stream
    # (Fix 1), keyed on the correlation token so the file reader's hydration copy dedups against it.
    assert _wait_until(lambda: len(user_turns) == 1)
    assert user_turns[0]["type"] == "user_message"
    assert user_turns[0]["content"] == "hello"
    assert user_turns[0]["event_id"] == f"codex-user-cid-{client_id}"
    connection.stop()


def test_build_returns_none_when_the_daemon_is_not_reachable(tmp_path: Path) -> None:
    def _boom(_: Path) -> CodexAppServerClient:
        raise OSError("no socket")

    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        on_user_turn=lambda event: None,
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
        on_user_turn=lambda event: None,
        model_state_path=tmp_path / "model.json",
        open_client=lambda _: client,
    )
    assert connection is not None
    # The daemon dies: the next reader poll sees a closed transport and the connection goes not-alive.
    transport.closed = True
    assert _wait_until(lambda: not connection.is_alive)
    connection.stop()


def test_build_caches_the_account_model_list(tmp_path: Path) -> None:
    # The connection fetches model/list once on connect and exposes it as the per-agent chip-match set.
    transport = _LocalTransport()
    transport.respond_result("thread/read", {"thread": {"status": {"type": "idle"}, "turns": []}})
    transport.respond_result(
        "model/list",
        {
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    "serviceTiers": [{"id": "priority"}],
                }
            ]
        },
    )
    client = CodexAppServerClient(transport=transport)
    client.thread_id = "thread-1"
    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        on_user_turn=lambda event: None,
        model_state_path=tmp_path / "model.json",
        open_client=lambda _: client,
    )
    assert connection is not None
    assert [model.model for model in connection.codex_models] == ["gpt-5.6-sol"]
    connection.stop()


def test_build_seeds_model_state_from_the_resume_thread_info(tmp_path: Path) -> None:
    # On connect the durable model-state file is seeded from the settings the thread resumed with
    # (captured on the client as last_thread_info), so the chip matches the daemon before any
    # thread/settings/updated fires.
    transport = _LocalTransport()
    client = _bound_client(transport)
    client.last_thread_info = ThreadInfo(
        thread_id="thread-1", model="gpt-5.6-sol", effort="high", service_tier="priority"
    )
    state_path = tmp_path / "model.json"
    connection = CodexLiveConnection.build(
        tmp_path,
        on_queue_snapshot=lambda snapshot: None,
        on_activity=lambda activity: None,
        on_user_turn=lambda event: None,
        model_state_path=state_path,
        open_client=lambda _: client,
    )
    assert connection is not None
    identity = read_model_identity(state_path)
    assert identity is not None
    assert identity.model_id == "gpt-5.6-sol"
    assert identity.effort == "high"
    assert identity.fast is True
    connection.stop()
