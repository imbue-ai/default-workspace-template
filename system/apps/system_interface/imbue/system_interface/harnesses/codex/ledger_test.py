"""Unit tests for :class:`CodexMessageLedger`, driven by a scripted app-server event stream.

Every path runs against a :class:`CodexAppServerClient` over a scripted in-memory transport
(constructor injection) -- no live daemon. The transport answers ``submit`` RPCs and lets the
test ``push`` the exact notification stream (``turn/*`` / ``item/*`` / ``thread/*``) a scenario
needs; ``client.poll_notifications()`` then dispatches those frames into the ledger, exactly as
the live persistent connection does.

Covered: each transition (Sending/Queued/Delivered/Returned), Reconcile with and without the
``itemsView!="full"`` ``thread/read`` guard, idempotent replay, the foreign ``clientId`` rule,
A3b (the chip is removed before the turn is shown), the EPHEMERAL queue (idle sweep + a fresh
session starting empty, no revival), the activity dot RUNNING until ``turn/completed``, and a
small conservation storm.
"""

from __future__ import annotations

import json
from collections import Counter
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.ledger import MessageState
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import model_state_path
from imbue.system_interface.harnesses.model import read_model_identity


class ScriptedTransport:
    """An in-memory transport double whose responses tests configure per method (see the
    sibling ``mngr_codex/app_server_client_test.py``)."""

    def __init__(self) -> None:
        self._inbound: deque[str] = deque()
        self.sent: list[str] = []
        self._responders: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self._is_closed = False

    def send(self, message: str) -> None:
        self.sent.append(message)
        request = json.loads(message)
        responder = self._responders.get(request.get("method"))
        if responder is not None:
            responder(request)

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError("no frame available")
        return self._inbound.popleft()

    def close(self) -> None:
        self._is_closed = True

    def push(self, frame: Mapping[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def respond_result(self, method: str, result: Mapping[str, Any]) -> None:
        self._responders[method] = lambda request: self.push(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        )

    def respond_error(self, method: str, code: int, message: str) -> None:
        self._responders[method] = lambda request: self.push(
            {"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}}
        )


def _handshaken_client(transport: ScriptedTransport, *, status_type: str = "idle") -> CodexAppServerClient:
    transport.respond_result(
        "initialize",
        {"userAgent": "mngr", "codexHome": "/home", "platformFamily": "unix", "platformOs": "linux"},
    )
    transport.respond_result("thread/start", {"thread": {"id": "thread-1", "status": {"type": status_type}}})
    client = CodexAppServerClient(transport=transport)
    client.initialize("mngr", "0.1")
    client.thread_start(cwd="/work")
    return client


class _Sink:
    """Records every queue snapshot and activity state the ledger pushed, in call order."""

    def __init__(self) -> None:
        self.queue_calls: list[list[dict[str, str]]] = []
        self.activity_calls: list[ActivityState] = []
        self.channel_log: list[str] = []

    def on_queue(self, snapshot: list[dict[str, str]]) -> None:
        self.queue_calls.append(snapshot)
        self.channel_log.append("queue")

    def on_activity(self, state: ActivityState) -> None:
        self.activity_calls.append(state)
        self.channel_log.append("activity")


def _build_ledger(
    transport: ScriptedTransport,
    *,
    status_type: str = "idle",
    sink: _Sink | None = None,
) -> tuple[CodexMessageLedger, CodexAppServerClient, _Sink]:
    client = _handshaken_client(transport, status_type=status_type)
    sink = sink if sink is not None else _Sink()
    counter = {"n": 0}

    def mint() -> str:
        counter["n"] += 1
        return f"cid-{counter['n']}"

    ledger = CodexMessageLedger.build(
        client,
        on_queue_snapshot=sink.on_queue,
        on_activity=sink.on_activity,
        mint_client_id=mint,
        now=lambda: "2026-08-11T00:00:00Z",
    )
    return ledger, client, sink


def _push_user_message_committed(transport: ScriptedTransport, client: CodexAppServerClient, client_id: str | None) -> None:
    transport.push(
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {"item": {"type": "userMessage", "id": f"item-{client_id}", "clientId": client_id}},
        }
    )
    client.poll_notifications()


def _push_turn_completed(
    transport: ScriptedTransport,
    client: CodexAppServerClient,
    turn_id: str,
    *,
    status: str = "completed",
    items_view: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> None:
    turn: dict[str, Any] = {"id": turn_id, "status": status}
    if items_view is not None:
        turn["itemsView"] = items_view
    if items is not None:
        turn["items"] = items
    transport.push({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": turn}})
    client.poll_notifications()


# =============================================================================
# Send transitions
# =============================================================================


def test_send_when_idle_stays_sending_until_commit_then_delivered() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})

    cid = ledger.send("hello")
    # Delivery is COMMIT, not ack: an idle send is Sending until its userMessage commits (A4).
    assert ledger.state_of(cid) == MessageState.SENDING
    assert ledger.is_sending() is True
    assert ledger.queued_snapshot() == []

    _push_user_message_committed(transport, client, cid)
    assert ledger.state_of(cid) == MessageState.DELIVERED
    assert ledger.is_sending() is False


def test_send_when_busy_is_queued_then_delivered_at_boundary() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)

    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    assert ledger.state_of(second) == MessageState.QUEUED
    assert [chip["content"] for chip in ledger.queued_snapshot()] == ["second"]
    assert ledger.queued_snapshot()[0]["queued_id"] == second

    # Auto-consumed at the next yield boundary: the steer's userMessage commits in the same turn.
    _push_user_message_committed(transport, client, second)
    assert ledger.state_of(second) == MessageState.DELIVERED
    assert ledger.queued_snapshot() == []


def test_send_failure_returns_to_composer() -> None:
    transport = ScriptedTransport()
    ledger, _client, _sink = _build_ledger(transport)
    transport.respond_error("turn/start", -32000, "boom")
    cid = ledger.send("doomed")
    assert ledger.state_of(cid) == MessageState.RETURNED
    assert ledger.reconcile_returned() == "doomed"
    assert ledger.queued_snapshot() == []
    assert ledger.is_sending() is False


# =============================================================================
# Reconcile
# =============================================================================


def test_reconcile_delivers_from_full_items_view_without_item_completed() -> None:
    """A ``turn/completed`` whose full item view carries our id delivers an entry that never
    received a separate ``item/completed`` (the accumulated-events path missed it)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    assert ledger.state_of(cid) == MessageState.SENDING

    _push_turn_completed(
        transport,
        client,
        "turn-1",
        items_view="full",
        items=[{"type": "userMessage", "id": "i1", "clientId": cid}, {"type": "agentMessage", "id": "a1"}],
    )
    assert ledger.state_of(cid) == MessageState.DELIVERED


def test_reconcile_returns_uncommitted_entry_on_completed_full_view() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")

    # Completed, authoritative (full) view, our id absent -> not committed -> Returned.
    _push_turn_completed(transport, client, "turn-1", items_view="full", items=[{"type": "agentMessage", "id": "a1"}])
    assert ledger.state_of(cid) == MessageState.RETURNED


def test_reconcile_returns_on_interrupted_turn() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    _push_turn_completed(transport, client, "turn-1", status="interrupted")
    # The already-committed first stays Delivered; the parked steer that never committed Returns.
    assert ledger.state_of(first) == MessageState.DELIVERED
    assert ledger.state_of(second) == MessageState.RETURNED


def test_reconcile_uncertainty_guard_reads_thread_and_delivers() -> None:
    """A completed turn with a non-full view + an unresolved entry triggers ONE ``thread/read``;
    the read shows the commit -> Delivered (the race where our ``item/completed`` was in flight)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")

    transport.respond_result(
        "thread/read",
        {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": cid}]}]}},
    )
    _push_turn_completed(transport, client, "turn-1", items_view="summary")
    assert ledger.state_of(cid) == MessageState.DELIVERED
    read_frames = [json.loads(f) for f in transport.sent if json.loads(f).get("method") == "thread/read"]
    assert len(read_frames) == 1


def test_reconcile_uncertainty_guard_returns_when_read_absent() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")

    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "turns": []}})
    _push_turn_completed(transport, client, "turn-1", items_view="summary")
    assert ledger.state_of(cid) == MessageState.RETURNED


# =============================================================================
# Idempotency / foreign
# =============================================================================


def test_duplicate_turn_completed_is_a_noop() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    _push_user_message_committed(transport, client, cid)
    assert ledger.state_of(cid) == MessageState.DELIVERED

    # A replayed completed turn (even one claiming aborted) must not un-deliver.
    _push_turn_completed(transport, client, "turn-1", status="interrupted")
    _push_turn_completed(transport, client, "turn-1", status="interrupted")
    assert ledger.state_of(cid) == MessageState.DELIVERED


def test_duplicate_item_completed_is_absorbing() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    _push_user_message_committed(transport, client, cid)
    _push_user_message_committed(transport, client, cid)
    assert ledger.state_of(cid) == MessageState.DELIVERED


def test_foreign_client_id_never_touches_our_chips() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    assert ledger.state_of(second) == MessageState.QUEUED

    # A human typing in the --remote TUI commits a userMessage with clientId null, and another
    # client commits one with an unknown id. Neither is ours: our chip must be untouched.
    _push_user_message_committed(transport, client, None)
    _push_user_message_committed(transport, client, "someone-elses-id")
    assert ledger.state_of(second) == MessageState.QUEUED
    assert ledger.state_of("someone-elses-id") is None
    assert [chip["content"] for chip in ledger.queued_snapshot()] == ["second"]


# =============================================================================
# A3b: the chip is removed before the turn is shown
# =============================================================================


def test_a3b_queue_removal_emitted_on_commit() -> None:
    """When a Queued entry commits, the ledger pushes the chip REMOVAL (an empty snapshot) so the
    frontend never shows the message as a chip and a turn at once (A3b, ledger side)."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    assert sink.queue_calls[-1] == [{"queued_id": second, "content": "second", "timestamp": "2026-08-11T00:00:00Z"}]

    sink.queue_calls.clear()
    _push_user_message_committed(transport, client, second)
    # The commit pushed exactly one queue snapshot: the removal (now empty).
    assert sink.queue_calls == [[]]
    assert ledger.queued_snapshot() == []
    assert ledger.state_of(second) == MessageState.DELIVERED


# =============================================================================
# EPHEMERAL queue
# =============================================================================


def test_idle_status_sweeps_the_queue_to_returned() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    assert ledger.state_of(second) == MessageState.QUEUED

    # The thread goes idle with a still-parked steer: it can never commit, so it Returns and the
    # queue is empty (the EPHEMERAL backstop).
    transport.push({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"status": {"type": "idle"}}})
    client.poll_notifications()
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.queued_snapshot() == []


def test_fresh_session_starts_empty_with_no_revival() -> None:
    """The queue lives and dies with the session: a new ledger over a new client starts empty and
    revives nothing from a prior session (no durable journal)."""
    transport_a = ScriptedTransport()
    ledger_a, client_a, _sink_a = _build_ledger(transport_a)
    transport_a.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger_a.send("first")
    _push_user_message_committed(transport_a, client_a, first)
    transport_a.respond_result("turn/steer", {"turnId": "turn-1"})
    ledger_a.send("still-parked")

    # A brand-new session (new daemon/client/ledger) knows nothing of the prior queue.
    transport_b = ScriptedTransport()
    ledger_b, _client_b, _sink_b = _build_ledger(transport_b)
    assert ledger_b.queued_snapshot() == []
    assert ledger_b.reconcile_returned() == ""
    assert ledger_b.entries == {}


# =============================================================================
# Activity: RUNNING until turn/completed (A6)
# =============================================================================


def test_activity_running_until_turn_completed() -> None:
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    assert ledger.activity_state() == ActivityState.IDLE

    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    ledger.send("go")
    assert ledger.activity_state() == ActivityState.THINKING

    # A tool starts -> TOOL_RUNNING; it completes -> THINKING. The turn is still open throughout.
    transport.push(
        {"jsonrpc": "2.0", "method": "item/started", "params": {"item": {"type": "commandExecution", "id": "t1"}}}
    )
    client.poll_notifications()
    assert ledger.activity_state() == ActivityState.TOOL_RUNNING
    transport.push(
        {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "t1"}}}
    )
    client.poll_notifications()
    assert ledger.activity_state() == ActivityState.THINKING

    # An assistant message completes (token generation stops) -- the turn is NOT done, so the dot
    # STAYS lit (the old codex idle-too-early bug).
    transport.push(
        {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "a1"}}}
    )
    client.poll_notifications()
    assert ledger.activity_state() == ActivityState.THINKING

    # turn/completed is the only signal that clears the dot.
    _push_turn_completed(transport, client, "turn-1", items_view="full", items=[])
    assert ledger.activity_state() == ActivityState.IDLE
    assert sink.activity_calls[-1] == ActivityState.IDLE


def test_callbacks_fire_only_on_change() -> None:
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    ledger.send("go")
    # A no-op notification (an unrelated turn method) must not re-push identical state.
    activity_before = list(sink.activity_calls)
    queue_before = list(sink.queue_calls)
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
    assert sink.activity_calls == activity_before
    assert sink.queue_calls == queue_before


# =============================================================================
# Shoulder-tap availability + interrupt (Contract B)
# =============================================================================


def _sent_methods(transport: ScriptedTransport) -> list[str]:
    return [json.loads(frame).get("method") for frame in transport.sent]


def test_tap_available_only_when_idle_send_and_nonempty_queue() -> None:
    """The tap gate: unavailable while anything is Sending, a no-op on an empty queue, offered iff
    (nothing Sending) AND (queue non-empty)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    # Empty queue -> no tap.
    assert ledger.is_tap_available() is False

    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    # An in-flight Sending message blocks the tap even though a turn is open.
    assert ledger.is_sending() is True
    assert ledger.is_tap_available() is False

    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    ledger.send("second")
    # Nothing Sending, one parked steer -> the tap is offered.
    assert ledger.is_sending() is False
    assert ledger.is_tap_available() is True


def test_interrupt_returns_uncommitted_in_send_order() -> None:
    """One turn/interrupt, then the non-committed owned entries Return in ascending send order; the
    already-committed one stays Delivered (A4)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    third = ledger.send("third")
    assert [chip["content"] for chip in ledger.queued_snapshot()] == ["second", "third"]

    transport.respond_result("turn/interrupt", {})
    # The post-interrupt thread shows only the committed first; the two parked steers never landed.
    transport.respond_result(
        "thread/read",
        {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}]}},
    )
    block = ledger.interrupt()

    assert block == "second\nthird"
    assert ledger.state_of(first) == MessageState.DELIVERED
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.state_of(third) == MessageState.RETURNED
    assert ledger.queued_snapshot() == []
    assert "turn/interrupt" in _sent_methods(transport)


def test_interrupt_clears_the_dot_immediately() -> None:
    """The interrupted turn is over, so the activity dot goes IDLE at once (A6) -- not deferred to the
    async turn/completed(interrupted)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    assert ledger.activity_state() == ActivityState.THINKING

    transport.respond_result("turn/interrupt", {})
    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "turns": []}})
    ledger.interrupt()
    assert ledger.activity_state() == ActivityState.IDLE
    assert client.active_turn_id is None


def test_interrupt_during_flush_keeps_the_committed_prefix() -> None:
    """Interrupt mid-flush: the steers that already committed (observed, plus one committed on the
    daemon but not yet observed) stay/settle Delivered; only the genuinely non-committed tail Returns."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    m1 = ledger.send("m1")
    _push_user_message_committed(transport, client, m1)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    m2 = ledger.send("m2")
    m3 = ledger.send("m3")
    m4 = ledger.send("m4")
    # m2 commits during the flush (observed via item/completed) -> Delivered before the interrupt.
    _push_user_message_committed(transport, client, m2)
    assert ledger.state_of(m2) == MessageState.DELIVERED

    transport.respond_result("turn/interrupt", {})
    # The committed prefix is m1, m2, and m3 (m3 landed on the daemon but its item/completed was still
    # in flight when the interrupt fired); m4 never committed.
    transport.respond_result(
        "thread/read",
        {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "items": [
                            {"type": "userMessage", "clientId": m1},
                            {"type": "userMessage", "clientId": m2},
                            {"type": "userMessage", "clientId": m3},
                        ],
                    }
                ],
            }
        },
    )
    block = ledger.interrupt()

    assert block == "m4"
    assert ledger.state_of(m1) == MessageState.DELIVERED
    assert ledger.state_of(m2) == MessageState.DELIVERED
    assert ledger.state_of(m3) == MessageState.DELIVERED
    assert ledger.state_of(m4) == MessageState.RETURNED


def test_interrupt_with_no_active_turn_hands_back_without_an_rpc() -> None:
    """With nothing running, interrupt issues no turn/interrupt and returns whatever is already
    non-committed (here a send that failed straight to the composer)."""
    transport = ScriptedTransport()
    ledger, _client, _sink = _build_ledger(transport)
    transport.respond_error("turn/start", -32000, "boom")
    ledger.send("doomed")
    assert ledger.state_of("cid-1") == MessageState.RETURNED

    block = ledger.interrupt()
    assert block == "doomed"
    assert "turn/interrupt" not in _sent_methods(transport)


def test_interrupt_settles_even_when_the_interrupt_rpc_fails() -> None:
    """A failed turn/interrupt still settles the parked steers against the committed thread (best
    effort): the daemon's thread is the delivery authority regardless of the RPC's fate."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    transport.respond_error("turn/interrupt", -32000, "daemon exploded")
    transport.respond_result(
        "thread/read",
        {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}]}},
    )
    block = ledger.interrupt()
    assert block == "second"
    assert ledger.state_of(second) == MessageState.RETURNED


def test_second_interrupt_does_not_re_return_already_returned() -> None:
    """A message reaches the composer exactly once: a second stop in the same session must NOT
    re-prepend a message an earlier stop already handed back (the cumulative-Returned bug)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    transport.respond_result("turn/interrupt", {})
    transport.respond_result(
        "thread/read",
        {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}]}},
    )
    assert ledger.interrupt() == "second"
    assert ledger.state_of(second) == MessageState.RETURNED

    # The second stop (nothing running now) hands back nothing -- "second" was already prepended once.
    assert ledger.interrupt() == ""
    # It stays Returned (terminal) and is still the cumulative snapshot; it just is not handed off again.
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.reconcile_returned() == "second"


# =============================================================================
# Model-bar mirror (the codex writer that feeds the uniform read path)
# =============================================================================


def _push_settings_updated(
    transport: ScriptedTransport,
    client: CodexAppServerClient,
    settings: dict[str, Any],
) -> None:
    transport.push(
        {
            "jsonrpc": "2.0",
            "method": "thread/settings/updated",
            "params": {"threadId": "thread-1", "threadSettings": settings},
        }
    )
    client.poll_notifications()


def test_settings_updated_mirrors_to_minds_model_state_and_reads_back(tmp_path: Path) -> None:
    """The writer produces the uniform {model, effort, fast} file from a scripted
    thread/settings/updated, and the shared read path then yields the matching ModelChoice."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    CodexMessageLedger.build(client, model_state_path=state_path)

    _push_settings_updated(transport, client, {"model": "gpt-5.6-sol", "effort": "high", "serviceTier": "priority"})

    assert json.loads(state_path.read_text()) == {"model": "gpt-5.6-sol", "effort": "high", "fast": True}
    identity = read_model_identity(state_path)
    assert identity is not None
    assert identity == ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=True)
    matched = match_option(identity, CODEX_CATALOG.options)
    assert matched is not None
    assert matched.id == "gpt-5.6-sol"


def test_settings_updated_without_priority_tier_is_not_fast(tmp_path: Path) -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    CodexMessageLedger.build(client, model_state_path=state_path)

    # A non-priority tier (or an effort-less model) reads back as fast off / effort None.
    _push_settings_updated(transport, client, {"model": "gpt-5.6-terra", "effort": None, "serviceTier": "default"})

    assert json.loads(state_path.read_text()) == {"model": "gpt-5.6-terra", "effort": None, "fast": False}


def test_settings_updated_is_a_noop_without_a_model_state_path(tmp_path: Path) -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    # No model_state_path -> the mirror is disabled and nothing is written.
    CodexMessageLedger.build(client)

    _push_settings_updated(transport, client, {"model": "gpt-5.6-sol", "effort": "high", "serviceTier": "priority"})
    assert not state_path.exists()


def test_settings_updated_without_a_model_writes_nothing(tmp_path: Path) -> None:
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    state_path = model_state_path(tmp_path, CODEX_STATE_RELATIVE_PATH)
    CodexMessageLedger.build(client, model_state_path=state_path)

    # A settings frame carrying no model (or a malformed one) leaves the file absent.
    _push_settings_updated(transport, client, {"effort": "high", "serviceTier": "priority"})
    _push_settings_updated(transport, client, {})
    assert not state_path.exists()


# =============================================================================
# Conservation storm
# =============================================================================


def test_defaults_without_injection_mint_and_timestamp() -> None:
    """Built with no injected mint/now/callbacks: the default id is minted and a real chip
    timestamp stamped, and the change-gated callbacks are simply skipped."""
    transport = ScriptedTransport()
    client = _handshaken_client(transport)
    ledger = CodexMessageLedger.build(client)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    assert first.startswith("minds-")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    chip = ledger.queued_snapshot()[0]
    assert chip["queued_id"] == second
    assert chip["timestamp"] != ""


def test_malformed_notifications_are_tolerated() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")

    # Directly deliver malformed params / item / turn shapes -- each is a no-op guard.
    ledger.handle_notification("item/started", None)
    ledger.handle_notification("item/started", {"item": "not-a-dict"})
    ledger.handle_notification("item/completed", {"item": 7})
    ledger.handle_notification("turn/completed", {"turn": "not-a-dict"})
    # A completed full-view turn with no ``items`` key at all -> the entry Returns (nothing committed).
    ledger.handle_notification("turn/completed", {"turn": {"id": "turn-1", "status": "completed", "itemsView": "full"}})
    assert ledger.state_of(cid) == MessageState.RETURNED


def test_uncertainty_guard_returns_when_thread_read_raises() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    transport.respond_error("thread/read", -32000, "daemon exploded")
    _push_turn_completed(transport, client, "turn-1", items_view="summary")
    assert ledger.state_of(cid) == MessageState.RETURNED


def test_conservation_holds_across_send_queue_deliver_return() -> None:
    """Every accepted message is in exactly one state and the four states partition the total."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    transport.respond_result("turn/steer", {"turnId": "turn-1"})

    accepted: list[str] = []

    def verify(total: int) -> None:
        counts = Counter(entry.state for entry in ledger.entries.values())
        assert sum(counts.values()) == total
        for cid in accepted:
            state = ledger.state_of(cid)
            assert state is not None

    # Open the turn, then queue two steers.
    accepted.append(ledger.send("m1"))
    verify(1)
    _push_user_message_committed(transport, client, accepted[0])
    verify(1)
    accepted.append(ledger.send("m2"))
    accepted.append(ledger.send("m3"))
    verify(3)
    assert [c["content"] for c in ledger.queued_snapshot()] == ["m2", "m3"]

    # m2 commits at a boundary; the turn is then interrupted, returning m3.
    _push_user_message_committed(transport, client, accepted[1])
    verify(3)
    _push_turn_completed(transport, client, "turn-1", status="interrupted")
    verify(3)

    states = Counter(entry.state for entry in ledger.entries.values())
    assert states[MessageState.DELIVERED] == 2
    assert states[MessageState.RETURNED] == 1
    assert states[MessageState.QUEUED] == 0
    assert states[MessageState.SENDING] == 0
    assert ledger.reconcile_returned() == "m3"
