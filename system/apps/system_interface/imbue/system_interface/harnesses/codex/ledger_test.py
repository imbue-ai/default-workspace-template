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
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.ledger import LedgerEntry
from imbue.system_interface.harnesses.codex.ledger import MessageState
from imbue.system_interface.harnesses.codex.model import CODEX_STATE_RELATIVE_PATH
from imbue.system_interface.harnesses.codex.model import codex_models_to_options
from imbue.system_interface.harnesses.codex.session_parser import codex_user_turn_event_id
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
        self._responders[method] = lambda request: self.push({"jsonrpc": "2.0", "id": request["id"], "result": result})

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
    """Records every queue snapshot, user-turn, and activity state the ledger pushed, in call order.

    ``channel_log`` interleaves the three channels so a test can assert the A3b ordered handoff:
    a committed steer's queue removal is logged BEFORE its user-turn."""

    def __init__(self) -> None:
        self.queue_calls: list[list[dict[str, str]]] = []
        self.user_turns: list[dict[str, Any]] = []
        self.channel_log: list[str] = []

    def on_queue(self, snapshot: list[dict[str, str]]) -> None:
        self.queue_calls.append(snapshot)
        self.channel_log.append("queue")

    def on_user_turn(self, event: dict[str, Any]) -> None:
        self.user_turns.append(event)
        self.channel_log.append("user_turn")


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
        on_user_turn=sink.on_user_turn,
        mint_client_id=mint,
        now=lambda: "2026-08-11T00:00:00Z",
    )
    return ledger, client, sink


def _push_user_message_committed(
    transport: ScriptedTransport,
    client: CodexAppServerClient,
    client_id: str | None,
    *,
    item_id: str | None = None,
    content: str | None = None,
    completed_at_ms: int | None = None,
) -> None:
    item: dict[str, Any] = {
        "type": "userMessage",
        "id": item_id if item_id is not None else f"item-{client_id}",
        "clientId": client_id,
    }
    if content is not None:
        item["content"] = [{"type": "text", "text": content}]
    params: dict[str, Any] = {"item": item}
    if completed_at_ms is not None:
        params["completedAtMs"] = completed_at_ms
    transport.push({"jsonrpc": "2.0", "method": "item/completed", "params": params})
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
        {
            "thread": {
                "id": "thread-1",
                "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": cid}]}],
            }
        },
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
    assert sink.queue_calls[-1] == [
        {"queued_id": second, "content": "second", "timestamp": "2026-08-11T00:00:00Z", "is_sending": False}
    ]

    sink.queue_calls.clear()
    _push_user_message_committed(transport, client, second)
    # The commit pushed exactly one queue snapshot: the removal (now empty).
    assert sink.queue_calls == [[]]
    assert ledger.queued_snapshot() == []
    assert ledger.state_of(second) == MessageState.DELIVERED


def test_a3b_chip_removal_is_emitted_before_the_user_turn() -> None:
    """The ordered handoff (A3b): when a Queued entry commits, the ledger broadcasts the chip
    REMOVAL (queue snapshot) and THEN the committed user-turn -- never the turn while the chip is
    still shown. The interleaved channel log proves the order across the two channels."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    sink.channel_log.clear()
    sink.user_turns.clear()
    _push_user_message_committed(transport, client, second, content="second")

    # Exactly one user-turn, and the queue removal was logged strictly before it.
    assert [event["content"] for event in sink.user_turns] == ["second"]
    assert "queue" in sink.channel_log and "user_turn" in sink.channel_log
    assert sink.channel_log.index("queue") < sink.channel_log.index("user_turn")


def test_delivery_records_the_app_server_item_id() -> None:
    """Fix 2: delivery adopts codex's own ``item.id`` onto the entry (the identity we track), while
    the minted token remains only the correlation link."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    assert ledger.entries[cid].item_id is None

    _push_user_message_committed(transport, client, cid, item_id="codex-item-xyz", content="hello")
    assert ledger.state_of(cid) == MessageState.DELIVERED
    assert ledger.entries[cid].item_id == "codex-item-xyz"


def test_delivered_user_turn_is_emitted_keyed_on_the_correlation_token() -> None:
    """The ledger emits the live user-turn on commit; its event_id keys on the echoed client_id so
    the rollout file reader's hydration copy of the same message dedups against it."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    _push_user_message_committed(transport, client, cid, content="hello", completed_at_ms=1786526413296)

    assert len(sink.user_turns) == 1
    event = sink.user_turns[0]
    assert event["type"] == "user_message"
    assert event["content"] == "hello"
    assert event["event_id"] == f"codex-user-cid-{cid}"
    assert event["source"] == "codex/common_transcript"


def test_foreign_user_message_emits_a_source_agnostic_user_turn() -> None:
    """Fix 1/2 source-agnostic: a userMessage committed with a null (TUI) or another client's id is
    emitted as a live user-turn, but creates NO entry and never touches our chips (A3)."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    ours = ledger.send("ours")
    _push_user_message_committed(transport, client, ours, content="ours")
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    parked = ledger.send("parked")
    assert ledger.state_of(parked) == MessageState.QUEUED
    sink.user_turns.clear()

    # A TUI human types (clientId null) and another client sends (clientId not ours). Both commit.
    _push_user_message_committed(
        transport, client, None, item_id="foreign-tui", content="from the TUI", completed_at_ms=1786526420000
    )
    _push_user_message_committed(
        transport, client, "someone-elses", item_id="foreign-other", content="from another client"
    )

    # Both surfaced as live user-turns, source-agnostic.
    assert [event["content"] for event in sink.user_turns] == ["from the TUI", "from another client"]
    # The anon (null-clientId) turn keys on epoch-ms + content; the tagged one keys on its own id.
    assert sink.user_turns[0]["event_id"] == codex_user_turn_event_id(None, 1786526420000, "from the TUI")
    assert sink.user_turns[1]["event_id"] == codex_user_turn_event_id("someone-elses", None, "from another client")
    assert sink.user_turns[1]["event_id"] == "codex-user-cid-someone-elses"
    # Neither created an entry, and our parked chip is untouched.
    assert "foreign-tui" not in ledger.entries
    assert "someone-elses" not in ledger.entries
    assert ledger.state_of("someone-elses") is None
    assert [chip["content"] for chip in ledger.queued_snapshot()] == ["parked"]


def test_user_turn_emitted_once_across_item_completed_and_reconcile() -> None:
    """A message delivered via item/completed and then re-settled by the turn/completed reconcile
    emits its live user-turn exactly once (deduped by event_id)."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    _push_user_message_committed(transport, client, cid, content="hello")
    _push_turn_completed(
        transport, client, "turn-1", items_view="full", items=[{"type": "userMessage", "clientId": cid}]
    )
    assert [event["content"] for event in sink.user_turns] == ["hello"]


def test_reconcile_only_delivery_still_emits_the_user_turn() -> None:
    """When a commit is seen ONLY via the turn's full item view (its own item/completed was missed),
    the reconcile both delivers the entry AND emits its live user-turn (from the entry's text)."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    cid = ledger.send("hello")
    _push_turn_completed(
        transport, client, "turn-1", items_view="full", items=[{"type": "userMessage", "clientId": cid}]
    )
    assert ledger.state_of(cid) == MessageState.DELIVERED
    assert [event["content"] for event in sink.user_turns] == ["hello"]


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

    # The thread goes idle with a still-parked steer: the idle sweep reconciles via thread/read, which
    # shows only the committed first, so the uncommitted steer Returns and the queue is empty.
    transport.respond_result(
        "thread/read",
        {
            "thread": {
                "id": "thread-1",
                "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}],
            }
        },
    )
    transport.push({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"status": {"type": "idle"}}})
    client.poll_notifications()
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.queued_snapshot() == []


def test_idle_sweep_keeps_a_steer_that_committed_just_before_idle() -> None:
    """The idle sweep is a reconcile, not a blind return (Fix 4): a steer whose commit landed on the
    daemon just before the idle edge (its item/completed still in flight) stays Delivered."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    # The thread/read at the idle edge shows BOTH committed -> second stays Delivered, not returned.
    transport.respond_result(
        "thread/read",
        {
            "thread": {
                "id": "thread-1",
                "turns": [
                    {
                        "id": "turn-1",
                        "items": [
                            {"type": "userMessage", "clientId": first},
                            {"type": "userMessage", "clientId": second},
                        ],
                    }
                ],
            }
        },
    )
    transport.push({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"status": {"type": "idle"}}})
    client.poll_notifications()
    assert ledger.state_of(second) == MessageState.DELIVERED
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
    assert ledger.turn_activity() == ActivityState.IDLE

    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    ledger.send("go")
    assert ledger.turn_activity() == ActivityState.THINKING

    # A tool starts -> TOOL_RUNNING; it completes -> THINKING. The turn is still open throughout.
    transport.push(
        {"jsonrpc": "2.0", "method": "item/started", "params": {"item": {"type": "commandExecution", "id": "t1"}}}
    )
    client.poll_notifications()
    assert ledger.turn_activity() == ActivityState.TOOL_RUNNING
    transport.push(
        {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "commandExecution", "id": "t1"}}}
    )
    client.poll_notifications()
    assert ledger.turn_activity() == ActivityState.THINKING

    # An assistant message completes (token generation stops) -- the turn is NOT done, so the dot
    # STAYS lit (the old codex idle-too-early bug).
    transport.push(
        {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "a1"}}}
    )
    client.poll_notifications()
    assert ledger.turn_activity() == ActivityState.THINKING

    # turn/completed is the only signal that clears the dot.
    _push_turn_completed(transport, client, "turn-1", items_view="full", items=[])
    assert ledger.turn_activity() == ActivityState.IDLE


def test_callbacks_fire_only_on_change() -> None:
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    ledger.send("go")
    # A no-op notification (an unrelated turn method) must not re-push identical state.
    queue_before = list(sink.queue_calls)
    transport.push({"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    client.poll_notifications()
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
        {
            "thread": {
                "id": "thread-1",
                "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}],
            }
        },
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
    assert ledger.turn_activity() == ActivityState.THINKING

    transport.respond_result("turn/interrupt", {})
    transport.respond_result("thread/read", {"thread": {"id": "thread-1", "turns": []}})
    ledger.interrupt()
    assert ledger.turn_activity() == ActivityState.IDLE
    assert client.active_turn_id is None


def test_interrupt_optimistically_returns_then_a_late_commit_corrects_to_delivered() -> None:
    """Async interrupt (A5, Fix 4): the block is computed from the CURRENT live state (no blocking
    thread/read), so a steer that committed on the daemon JUST before the interrupt but whose
    ``item/completed`` had not yet arrived is optimistically Returned -- then its LATE ``item/completed``
    CORRECTS it to Delivered (delivery = COMMIT, A4, prefer the committed truth), so the accepted-then-
    committed micro-race is never double-counted. The genuinely-uncommitted tail stays Returned."""
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

    # Stop: m3 committed on the daemon but its item/completed is still in flight, so it is optimistically
    # Returned alongside the never-committed m4 -- the block is every still-live entry, in send order.
    transport.respond_result("turn/interrupt", {})
    block = ledger.interrupt()
    assert block == "m3\nm4"
    assert ledger.state_of(m3) == MessageState.RETURNED
    assert ledger.state_of(m4) == MessageState.RETURNED

    # m3's item/completed lands late on the subscribed stream: it is definitive proof of commit, so it
    # corrects m3 to Delivered rather than leaving it wrongly Returned. m4 (never committed) stays.
    _push_user_message_committed(transport, client, m3)
    assert ledger.state_of(m1) == MessageState.DELIVERED
    assert ledger.state_of(m2) == MessageState.DELIVERED
    assert ledger.state_of(m3) == MessageState.DELIVERED
    assert ledger.state_of(m4) == MessageState.RETURNED
    # Conservation: m3 is counted once (Delivered), never as both Returned and Delivered.
    assert ledger.reconcile_returned() == "m4"


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


def test_interrupt_returns_optimistically_regardless_of_the_interrupt_rpc_fate() -> None:
    """The interrupt is fired FIRE-AND-FORGET (A5): the block is computed optimistically from the live
    state and returned at once, so even a turn/interrupt that would error never blocks or changes the
    hand-off. The parked steer Returns regardless of the RPC's fate."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")

    transport.respond_error("turn/interrupt", -32000, "daemon exploded")
    block = ledger.interrupt()
    assert block == "second"
    assert ledger.state_of(second) == MessageState.RETURNED
    # The turn/interrupt frame was still sent (fire-and-forget), just not awaited.
    assert "turn/interrupt" in _sent_methods(transport)


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
        {
            "thread": {
                "id": "thread-1",
                "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}],
            }
        },
    )
    assert ledger.interrupt() == "second"
    assert ledger.state_of(second) == MessageState.RETURNED

    # The second stop (nothing running now) hands back nothing -- "second" was already prepended once.
    assert ledger.interrupt() == ""
    # It stays Returned (terminal) and is still the cumulative snapshot; it just is not handed off again.
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.reconcile_returned() == "second"


# =============================================================================
# Shoulder-tap: deliver the parked queue EARLY (interrupt + combined resend, Fix 3)
# =============================================================================


def _last_turn_start_client_id(transport: ScriptedTransport) -> str:
    """The ``clientUserMessageId`` on the most recent ``turn/start`` -- the combined resend's id."""
    for frame in reversed(transport.sent):
        request = json.loads(frame)
        if request.get("method") == "turn/start":
            return request["params"]["clientUserMessageId"]
    raise AssertionError("no turn/start was sent")


def _queue_two_steers(
    transport: ScriptedTransport, ledger: CodexMessageLedger, client: CodexAppServerClient
) -> tuple[str, str]:
    """Open a turn (delivering ``first``) and park two steers ``second``/``third`` -- the tap setup."""
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    second = ledger.send("second")
    third = ledger.send("third")
    assert [chip["content"] for chip in ledger.queued_snapshot()] == ["second", "third"]
    return second, third


def test_shoulder_tap_is_a_benign_noop_when_a_send_is_in_flight() -> None:
    """A tap racing an in-flight Sending is a benign no-op (``send_in_flight``), never an error."""
    transport = ScriptedTransport()
    ledger, _client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    # A Sending (not committed) message makes the tap unavailable.
    ledger.send("in-flight")
    assert ledger.is_sending() is True
    assert ledger.shoulder_tap().status == "send_in_flight"


def test_shoulder_tap_is_a_noop_when_the_queue_is_empty() -> None:
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    # ``first`` is Delivered and nothing is queued, so a tap has nothing to do.
    _push_user_message_committed(transport, client, first)
    assert ledger.shoulder_tap().status == "no_open_turn"


def test_shoulder_tap_resends_the_queue_as_one_combined_turn_delivered_together() -> None:
    """The tap interrupts, then re-sends the parked queue as ONE combined ``turn/start``; when that
    turn commits, every member resolves to Delivered together and ONE user-turn is emitted (Fix 3)."""
    transport = ScriptedTransport()
    sink = _Sink()
    ledger, client, _sink = _build_ledger(transport, sink=sink)
    second, third = _queue_two_steers(transport, ledger, client)

    transport.respond_result("turn/interrupt", {})
    # After the (fire-and-forget) interrupt the daemon is idle; the combined resend opens a fresh turn.
    transport.respond_result("turn/start", {"turn": {"id": "turn-2", "status": "inProgress"}})
    sink.user_turns.clear()
    assert ledger.shoulder_tap().status == "tapped"

    # Through the resend the two messages stay visible as "Sending..." chips (A1a), never removed.
    assert ledger.state_of(second) == MessageState.SENDING
    assert ledger.state_of(third) == MessageState.SENDING
    assert [(chip["content"], chip["is_sending"]) for chip in ledger.queued_snapshot()] == [
        ("second", True),
        ("third", True),
    ]
    # While a resend is in flight the tap greys (nothing Queued, something Sending).
    assert ledger.is_tap_available() is False
    assert "turn/interrupt" in _sent_methods(transport)

    combined_id = _last_turn_start_client_id(transport)
    _push_user_message_committed(transport, client, combined_id, content="second\n\nthird")
    # The combined turn committed: both members Delivered, chips gone, ONE user-turn (the concat).
    assert ledger.state_of(second) == MessageState.DELIVERED
    assert ledger.state_of(third) == MessageState.DELIVERED
    assert ledger.queued_snapshot() == []
    assert [event["content"] for event in sink.user_turns] == ["second\n\nthird"]


def test_shoulder_tap_does_not_resend_an_already_committed_steer() -> None:
    """A steer already Delivered (its commit observed on the subscribed stream) is no longer a Queued
    chip, so the tap never re-sends it -- only the genuinely-uncommitted remainder rides the combined
    resend (Fix 3, reconcile per id via observed commits, no blocking thread/read)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    second, third = _queue_two_steers(transport, ledger, client)

    # ``second`` commits at a yield boundary before the tap -> Delivered (observed via item/completed).
    _push_user_message_committed(transport, client, second)
    assert ledger.state_of(second) == MessageState.DELIVERED

    transport.respond_result("turn/interrupt", {})
    transport.respond_result("turn/start", {"turn": {"id": "turn-2", "status": "inProgress"}})
    assert ledger.shoulder_tap().status == "tapped"

    # ``second`` stayed Delivered and was not re-sent; ``third`` is the only thing being re-sent.
    assert ledger.state_of(second) == MessageState.DELIVERED
    assert ledger.state_of(third) == MessageState.SENDING
    combined_id = _last_turn_start_client_id(transport)
    combined_frame = json.loads(transport.sent[-1])
    assert combined_frame["params"]["input"] == [{"type": "text", "text": "third"}]

    _push_user_message_committed(transport, client, combined_id, content="third")
    assert ledger.state_of(third) == MessageState.DELIVERED


def test_shoulder_tap_returns_the_queue_to_composer_when_the_resend_fails() -> None:
    """If the combined resend itself fails to submit, the parked messages are not lost: they Return AND
    the Returned block is handed back through the result for the composer, in send order (A1a, Fix 3)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    second, third = _queue_two_steers(transport, ledger, client)

    transport.respond_result("turn/interrupt", {})
    transport.respond_error("turn/start", -32000, "resend boom")
    result = ledger.shoulder_tap()
    assert result.status == "tapped"
    # The parked text is handed back to the composer (send order), never swallowed.
    assert result.returned_block == "second\nthird"
    assert ledger.state_of(second) == MessageState.RETURNED
    assert ledger.state_of(third) == MessageState.RETURNED
    assert ledger.queued_snapshot() == []
    # Once handed off, a following stop does not re-prepend the same text (once-only hand-off).
    assert ledger.interrupt() == ""


# =============================================================================
# Interrupt reconciles ALL live entries, incl. an unbound in-flight Sending (Fix 4)
# =============================================================================


def test_interrupt_returns_queued_and_unbound_inflight_sending_together() -> None:
    """The required interrupt-during-flush case: a stop while the queue is non-empty AND a message is
    in-flight Sending (still mid-``submit``, so unbound) returns BOTH, in send order (Fix 4)."""
    transport = ScriptedTransport()
    ledger, client, _sink = _build_ledger(transport)
    transport.respond_result("turn/start", {"turn": {"id": "turn-1", "status": "inProgress"}})
    first = ledger.send("first")
    _push_user_message_committed(transport, client, first)
    transport.respond_result("turn/steer", {"turnId": "turn-1"})
    queued = ledger.send("queued")
    assert ledger.state_of(queued) == MessageState.QUEUED

    # A fresh send whose ``submit`` has not resolved yet: a Sending entry with no turn binding, which
    # the old turn-scoped reconcile missed entirely (the gap Fix 4 closes).
    inflight = LedgerEntry(
        client_id="inflight", send_seq=ledger._next_seq(), text="in-flight", state=MessageState.SENDING
    )
    ledger.entries["inflight"] = inflight
    assert ledger.is_sending() is True

    transport.respond_result("turn/interrupt", {})
    transport.respond_result(
        "thread/read",
        {
            "thread": {
                "id": "thread-1",
                "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "clientId": first}]}],
            }
        },
    )
    block = ledger.interrupt()

    assert block == "queued\nin-flight"
    assert ledger.state_of(first) == MessageState.DELIVERED
    assert ledger.state_of(queued) == MessageState.RETURNED
    assert ledger.state_of("inflight") == MessageState.RETURNED


def test_late_submit_after_interrupt_reconciles_to_returned() -> None:
    """A send whose ``submit`` result lands AFTER an interrupt cleared the turn reconciles itself to
    Returned rather than leaving a stray Sending entry / opening a turn on the idle daemon (Fix 4)."""
    transport = ScriptedTransport()
    ledger, _client, _sink = _build_ledger(transport)

    # A ``turn/start`` responder that bumps the interrupt generation mid-RPC simulates an interrupt
    # landing while this send's submit is in flight.
    def bump_then_respond(request: Mapping[str, Any]) -> None:
        ledger.interrupt_generation += 1
        transport.push(
            {"jsonrpc": "2.0", "id": request["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}}
        )

    transport._responders["turn/start"] = bump_then_respond
    cid = ledger.send("racy")
    assert ledger.state_of(cid) == MessageState.RETURNED
    assert ledger.is_sending() is False


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


def test_settings_updated_mirrors_to_model_state_and_reads_back(tmp_path: Path) -> None:
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
    # Codex has no static catalog; the chip-match is against the per-agent model/list set.
    options = codex_models_to_options(
        (
            CodexModel.model_validate(
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    "serviceTiers": [{"id": "priority"}],
                }
            ),
        )
    )
    matched = match_option(identity, options)
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
    ledger.handle_notification(
        "turn/completed", {"turn": {"id": "turn-1", "status": "completed", "itemsView": "full"}}
    )
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
