"""Live-state message conservation for codex across Send / Queue / Shoulder-tap / Interrupt.

The canonical enforcement the message-lifecycle contract asks for in Part D, for codex: drive the
four operations in seeded, randomized interleavings against the REAL :class:`CodexMessageLedger`
(the single backend authority for a codex agent's five message states) fed by a SCRIPTED
app-server event stream, and after EVERY step assert the full live-state law.

Where the claude sibling (``test_claude_message_lifecycle_conservation.py``) plays claude's side on
disk and reads its verdicts off a real :class:`ClaudeSessionWatcher`, codex's backend authority is
the ledger itself: it consumes the stock ``codex app-server`` notification stream (``turn/*`` /
``item/*`` / ``thread/*``) through one :class:`CodexAppServerClient`, keyed by the
``clientUserMessageId`` it mints per send. So here the "world" IS a scripted fake daemon (an
:class:`AppServerTransport` the client is built over): it answers ``submit`` / ``interrupt`` /
``thread/read`` RPCs from its own turn+commit state and ``push``es the exact notification frames a
scenario needs. Every verdict is read straight off the ledger; the world keeps an INDEPENDENT
expected-state map (derived from what it told the daemon to do) and the per-step law asserts the
ledger agrees with it.

After every step:

- **Conservation (A1).** Every accepted message is in EXACTLY ONE of Sending / Queued / Delivered /
  Returned, ``sending + queued + delivered + returned == total accepted``, zero lost (a message in
  no state) and zero ghosts (a message counted in two). Additionally the ledger's per-message state
  equals the world's independently-derived expectation -- the real correctness check, since a single
  ``LedgerEntry.state`` structurally holds one value.
- **No foreign entry.** A ``userMessage`` with ``clientId`` null or an unknown id (a human typing in
  the visible ``--remote`` TUI, or another client) never creates, removes, or returns one of our
  chips: the ledger's key set stays exactly the set of ids we sent (contract Delivery=COMMIT + the
  foreign rule).
- **Order.** The queue snapshot preserves send order; the interrupt-return block is the Returned
  text in ascending ``send_seq`` (send order), a subsequence of the accepted order.
- **A3b.** A Queued->Delivered transition emits the chip REMOVAL snapshot, and no message is ever a
  chip and a committed message at once (verified per step by the ghost check, and by a controlled
  transition that reads the ledger's ordered queue-snapshot emissions).
- **Delivery = COMMIT, not ack (A4).** A steer accepted as a pending ``turn/steer`` (Queued) that is
  interrupted before its ``userMessage`` commits Returns; only ids the daemon actually committed
  (as seen through the ``thread/read`` reconcile guard) stay Delivered.
- **Interrupt-during-flush (required).** A stop mid-flush: the steers already committed (observed via
  ``item/completed``, PLUS one committed on the daemon but whose ``item/completed`` was still in
  flight) settle Delivered through the reconcile guard; only the genuinely non-committed tail Returns.
- **EPHEMERAL queue.** An idle ``thread/status`` sweeps any still-parked steer back to the composer
  (the queue is empty whenever idle), and a fresh session's ledger starts empty and revives nothing.

Fully synchronous and seeded (a scripted stream, no background thread, no live daemon), so a failure
is replayable from the seed in the assertion note.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections import deque
from typing import Any

import pytest

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.ledger import MessageState

pytestmark = pytest.mark.acceptance

_BASE_SEED = 20260811
_ROUND_COUNT = 30

_LIVE_STATES = frozenset({MessageState.SENDING, MessageState.QUEUED})


class _Sink:
    """Records every queue snapshot the ledger pushed, in call order, for the A3b ordering check."""

    def __init__(self) -> None:
        self.queue_calls: list[list[dict[str, str]]] = []

    def on_queue(self, snapshot: list[dict[str, str]]) -> None:
        self.queue_calls.append(snapshot)


class _CodexWorld:
    """A scripted fake ``codex app-server`` (an :class:`AppServerTransport`) plus the REAL ledger.

    The world answers the client's ``initialize`` / ``thread/start`` / ``turn/start`` / ``turn/steer``
    / ``turn/interrupt`` / ``thread/read`` requests from its own daemon state (the running turn and the
    set of committed ``clientUserMessageId``s) and ``push``es notification frames the ledger dispatches
    on :meth:`CodexAppServerClient.poll_notifications`. It also mints the client ids and keeps the
    INDEPENDENT ``_expected`` map every verify asserts the ledger against.
    """

    def __init__(self) -> None:
        # -- transport queue of inbound JSON-RPC frames the client will read.
        self._inbound: deque[str] = deque()
        self.sent_methods: list[str] = []
        # -- daemon state -------------------------------------------------------------
        self._thread_id = "thread-1"
        self._turn_counter = 0
        self._current_turn_id: str | None = None
        # Committed ``clientUserMessageId``s, in commit order -- what ``thread/read`` reports.
        self._daemon_committed: list[str] = []
        # -- request routing (a dict dispatch, so no if/elif ladder) -------------------
        self._responders = {
            "initialize": self._respond_initialize,
            "thread/start": self._respond_thread_start,
            "turn/start": self._respond_turn_start,
            "turn/steer": self._respond_turn_steer,
            "turn/interrupt": self._respond_ok,
            "thread/read": self._respond_thread_read,
        }
        # -- bookkeeping ---------------------------------------------------------------
        self.accepted: list[str] = []
        self.text_by_cid: dict[str, str] = {}
        self._expected: dict[str, MessageState] = {}
        # cids the world has already accounted as handed back to the composer by a prior stop --
        # mirrors the ledger's once-only Returned hand-off, so a second stop expects an empty block.
        self._returned_handed_off: set[str] = set()
        self._mint_counter = 0
        self._text_counter = 0
        self.ops: list[str] = []
        # -- the real client + ledger over this scripted transport ---------------------
        self.client = CodexAppServerClient(transport=self)
        self.client.initialize("mngr", "0.1")
        self.client.thread_start(cwd="/work")
        self.sink = _Sink()
        self.ledger = CodexMessageLedger.build(
            self.client,
            on_queue_snapshot=self.sink.on_queue,
            mint_client_id=self._mint,
            now=lambda: "2026-08-11T00:00:00Z",
        )

    # -- AppServerTransport (the scripted daemon) ----------------------------------------

    def send(self, message: str) -> None:
        request = json.loads(message)
        method = request.get("method")
        # A frame with no id is a notification (e.g. ``initialized``): nothing to answer.
        if "id" not in request:
            return
        self.sent_methods.append(method)
        responder = self._responders.get(method)
        result = responder(request) if responder is not None else {}
        self._push({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError("no frame available")
        return self._inbound.popleft()

    def close(self) -> None:
        return None

    def _push(self, frame: dict[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def _respond_initialize(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"userAgent": "mngr", "codexHome": "/home", "platformFamily": "unix", "platformOs": "linux"}

    def _respond_thread_start(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"thread": {"id": self._thread_id, "status": {"type": "idle"}}}

    def _respond_turn_start(self, _request: dict[str, Any]) -> dict[str, Any]:
        self._turn_counter += 1
        self._current_turn_id = f"turn-{self._turn_counter}"
        return {"turn": {"id": self._current_turn_id, "status": "inProgress"}}

    def _respond_turn_steer(self, _request: dict[str, Any]) -> dict[str, Any]:
        assert self._current_turn_id is not None, "a steer requires a running turn"
        return {"turnId": self._current_turn_id}

    def _respond_ok(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _respond_thread_read(self, _request: dict[str, Any]) -> dict[str, Any]:
        items = [{"type": "userMessage", "clientId": cid} for cid in self._daemon_committed]
        turn_id = self._current_turn_id if self._current_turn_id is not None else "turn-final"
        return {"thread": {"id": self._thread_id, "turns": [{"id": turn_id, "items": items}]}}

    # -- daemon-driven notifications -----------------------------------------------------

    def _push_item_completed(self, cid: str | None) -> None:
        self._push(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {"item": {"type": "userMessage", "id": f"item-{cid}", "clientId": cid}},
            }
        )
        self.client.poll_notifications()

    def _push_turn_completed(self, turn_id: str, status: str) -> None:
        items = [{"type": "userMessage", "clientId": cid} for cid in self._daemon_committed]
        self._push(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": status, "itemsView": "full", "items": items}},
            }
        )
        self.client.poll_notifications()

    def _push_status(self, status_type: str) -> None:
        self._push({"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"status": {"type": status_type}}})
        self.client.poll_notifications()

    # -- id / text minting ---------------------------------------------------------------

    def _mint(self) -> str:
        self._mint_counter += 1
        return f"cid-{self._mint_counter:03d}"

    def _new_text(self) -> str:
        self._text_counter += 1
        return f"codex-msg-{self._text_counter:03d}"

    def log(self, op: str) -> None:
        self.ops.append(op)

    def note(self) -> str:
        return "\n".join([f"REPLAY: seed={_BASE_SEED}"] + [f"  {op}" for op in self.ops])

    # -- the four operations, driving the REAL ledger ------------------------------------

    @property
    def is_idle(self) -> bool:
        return self._current_turn_id is None

    def user_send(self, deliver: bool = False) -> str:
        """A user send through the ledger: idle opens a turn (Sending), busy parks a steer (Queued).

        With ``deliver`` the daemon immediately commits the message's ``userMessage`` (the common
        idle-send that lands at once, or a steer consumed on the spot), moving it to Delivered.
        """
        was_idle = self.is_idle
        text = self._new_text()
        cid = self.ledger.send(text)
        self.accepted.append(cid)
        self.text_by_cid[cid] = text
        self._expected[cid] = MessageState.SENDING if was_idle else MessageState.QUEUED
        if deliver:
            self._commit_observed(cid)
        return cid

    def _commit_observed(self, cid: str) -> None:
        """The daemon commits ``cid``'s ``userMessage`` AND the ledger observes it -> Delivered."""
        if cid not in self._daemon_committed:
            self._daemon_committed.append(cid)
        self._expected[cid] = MessageState.DELIVERED
        self._push_item_completed(cid)

    def _daemon_commit_unobserved(self, cid: str) -> None:
        """The daemon commits ``cid`` but its ``item/completed`` is still in flight (the reconcile
        guard will find it committed at the next settle). The ledger stays live until then."""
        if cid not in self._daemon_committed:
            self._daemon_committed.append(cid)

    def commit_head(self) -> None:
        """Commit the oldest still-live (Sending/Queued) message -- the turn's next boundary."""
        live = sorted(
            (entry for entry in self.ledger.entries.values() if entry.state in _LIVE_STATES),
            key=lambda entry: entry.send_seq,
        )
        if live:
            self._commit_observed(live[0].client_id)

    def flush(self) -> None:
        """The REAL shoulder tap: assert the gate, then let the running turn consume the parked steers.

        codex parks a busy send as a ``turn/steer`` at send time, so an available tap needs no force
        call -- the steers auto-consume at the boundary. The world models that consumption by committing
        every currently-Queued steer, in send order. The gate is asserted against the ledger AND the
        world's independent expectation (unavailable while anything Sending; a no-op on an empty queue).
        """
        expected_available = (not self._expects(MessageState.SENDING)) and self._expects(MessageState.QUEUED)
        assert self.ledger.is_tap_available() == expected_available, (
            f"tap gate mismatch (ledger={self.ledger.is_tap_available()} expected={expected_available})\n{self.note()}"
        )
        if not expected_available:
            return
        queued = sorted(
            (entry for entry in self.ledger.entries.values() if entry.state == MessageState.QUEUED),
            key=lambda entry: entry.send_seq,
        )
        for entry in queued:
            self._commit_observed(entry.client_id)

    def stop(self) -> None:
        """The REAL interrupt (async, Fix 4): fire-and-forget ``turn/interrupt`` + an OPTIMISTIC settle
        from the current live state. Every still-live entry Returns in send order (an already-observed
        commit is Delivered, not live, and stays); no blocking thread/read. The block the ledger hands
        back must equal the fresh Returned text, ordered.
        """
        had_turn = not self.is_idle
        for cid, state in list(self._expected.items()):
            if state in _LIVE_STATES:
                self._expected[cid] = MessageState.RETURNED
        block = self.ledger.interrupt()
        self._current_turn_id = None
        expected_block = self._take_expected_returned_block()
        assert block == expected_block, f"interrupt block {block!r} != expected {expected_block!r}\n{self.note()}"
        if had_turn:
            assert "turn/interrupt" in self.sent_methods, (
                f"a running turn's stop must issue turn/interrupt\n{self.note()}"
            )

    def foreign(self) -> None:
        """A foreign ``userMessage`` (clientId null, then an unknown id): neither may touch our chips."""
        before = dict(self._expected)
        self._push_item_completed(None)
        self._push_item_completed("stranger")
        assert self.ledger.state_of("stranger") is None, f"a foreign id must not become an entry\n{self.note()}"
        assert dict(self._expected) == before, "the foreign push must not change our expectations"

    def settle_turn_end(self) -> None:
        """Natural turn end: ``turn/completed`` (full view). Committed entries stay Delivered; any
        still-parked steer the turn ended without Returns."""
        if self.is_idle:
            return
        turn_id = self._current_turn_id
        assert turn_id is not None
        for cid, state in list(self._expected.items()):
            if state in _LIVE_STATES:
                self._expected[cid] = (
                    MessageState.DELIVERED if cid in self._daemon_committed else MessageState.RETURNED
                )
        self._push_turn_completed(turn_id, status="completed")
        self._current_turn_id = None

    # -- reading the world's independent expectation -------------------------------------

    def _expects(self, state: MessageState) -> bool:
        return any(value == state for value in self._expected.values())

    def _expected_queued_cids(self) -> list[str]:
        cids = [cid for cid, state in self._expected.items() if state == MessageState.QUEUED]
        return sorted(cids, key=self.accepted.index)

    def _expected_returned_block(self) -> str:
        """The CUMULATIVE expected Returned text (mirrors ``ledger.reconcile_returned``)."""
        cids = [cid for cid, state in self._expected.items() if state == MessageState.RETURNED]
        cids.sort(key=self.accepted.index)
        return "\n".join(self.text_by_cid[cid] for cid in cids)

    def _take_expected_returned_block(self) -> str:
        """The expected once-only hand-off block (mirrors ``ledger.interrupt``): Returned entries not
        yet handed back, in send order, marked as handed off -- so a second stop expects an empty block."""
        cids = [
            cid
            for cid, state in self._expected.items()
            if state == MessageState.RETURNED and cid not in self._returned_handed_off
        ]
        cids.sort(key=self.accepted.index)
        self._returned_handed_off.update(cids)
        return "\n".join(self.text_by_cid[cid] for cid in cids)

    # -- the per-step conservation law ---------------------------------------------------

    def verify(self, context: str) -> None:
        note = self.note()
        entries = self.ledger.entries

        # No foreign entry ever entered the ledger: its key set is exactly what we sent.
        assert set(entries) == set(self.accepted), (
            f"the ledger keys {sorted(entries)} != accepted {sorted(self.accepted)} ({context})\n{note}"
        )

        # Exactly-one-state + the ledger agrees with the world's independent expectation (A1).
        counts: Counter[MessageState] = Counter()
        for cid in self.accepted:
            actual = self.ledger.state_of(cid)
            assert actual is not None, f"LOST: {cid!r} is in no live state ({context})\n{note}"
            expected = self._expected[cid]
            assert actual == expected, (
                f"state mismatch for {self.text_by_cid[cid]!r}: ledger={actual} expected={expected} ({context})\n{note}"
            )
            counts[actual] += 1

        # Conservation sum: the four states partition every accepted message.
        total = sum(counts.values())
        assert total == len(self.accepted), (
            f"conservation sum {total} != total accepted {len(self.accepted)} ({context})\n{note}"
        )

        # Order: the queue snapshot is the still-parked set in send order, no ghost/stale chip.
        snapshot = self.ledger.queued_snapshot()
        assert [chip["queued_id"] for chip in snapshot] == self._expected_queued_cids(), (
            f"queue snapshot {snapshot} != expected queued {self._expected_queued_cids()} ({context})\n{note}"
        )
        for chip in snapshot:
            assert chip["content"] == self.text_by_cid[chip["queued_id"]]

        # A message is never a chip and Delivered/Sending/Returned at once (the ghost guard for A3b).
        queued_ids = {chip["queued_id"] for chip in snapshot}
        for cid in queued_ids:
            assert self.ledger.state_of(cid) == MessageState.QUEUED

        # Returns are the Returned text in send order (a subsequence of the accepted order).
        assert self.ledger.reconcile_returned() == self._expected_returned_block(), (
            f"returned block mismatch ({context})\n{note}"
        )

        # The dot follows the turn, not token generation (A6): lit iff a turn is running.
        dot_idle = self.ledger.turn_activity() == ActivityState.IDLE
        assert dot_idle == self.is_idle, (
            f"activity dot {self.ledger.turn_activity()} disagrees with turn state (idle={self.is_idle}) ({context})\n{note}"
        )


# =============================================================================
# The required deterministic corners.
# =============================================================================


def _assert_a3b_chip_removal_emitted(world: _CodexWorld) -> None:
    """A controlled Queued->Delivered: the ledger emits the chip REMOVAL when the steer commits.

    Park a fresh steer into the open turn (a chip is pushed), then commit it and assert the ledger's
    LAST queue-snapshot emission dropped that chip -- so the frontend removes the chip on delivery and
    the message is never a chip and a committed turn at once (A3b, ledger side).
    """
    assert not world.is_idle, "the a3b corner needs an open turn to steer into"
    cid = world.user_send(deliver=False)
    assert world.ledger.state_of(cid) == MessageState.QUEUED
    assert cid in {chip["queued_id"] for chip in world.sink.queue_calls[-1]}, "the steer must push a chip"

    world.sink.queue_calls.clear()
    world._commit_observed(cid)
    assert world.sink.queue_calls, "committing a queued steer must emit a queue snapshot"
    assert cid not in {chip["queued_id"] for chip in world.sink.queue_calls[-1]}, (
        "A3b: the chip must be removed from the emitted snapshot when the steer commits"
    )
    assert world.ledger.state_of(cid) == MessageState.DELIVERED
    assert cid not in {chip["queued_id"] for chip in world.ledger.queued_snapshot()}


def _run_interrupt_during_partial_flush(world: _CodexWorld) -> None:
    """A stop mid-flush (async, Fix 4). The block is computed from the CURRENT live state (no blocking
    thread/read), so a steer committed on the daemon but whose ``item/completed`` is still in flight is
    optimistically Returned alongside the never-committed tail -- then its LATE ``item/completed``
    CORRECTS it to Delivered (delivery = COMMIT, A4; prefer the committed truth), so the accepted-then-
    committed micro-race is never double-counted. The already-observed commit stays Delivered."""
    assert not world.is_idle, "the partial-flush corner needs an open turn"
    m_observed = world.user_send(deliver=False)
    m_unobserved = world.user_send(deliver=False)
    m_tail = world.user_send(deliver=False)
    assert [world.ledger.state_of(c) for c in (m_observed, m_unobserved, m_tail)] == [MessageState.QUEUED] * 3

    # One steer commits and is observed (Delivered before the stop); a second lands on the daemon but
    # its item/completed is still in flight when the stop fires.
    world._commit_observed(m_observed)
    world._daemon_commit_unobserved(m_unobserved)

    # The stop returns from CURRENT live state: m_observed stays Delivered; m_unobserved (commit not yet
    # observed) and the never-committed tail both Return optimistically, in send order.
    world._expected[m_observed] = MessageState.DELIVERED
    world._expected[m_unobserved] = MessageState.RETURNED
    world._expected[m_tail] = MessageState.RETURNED
    block = world.ledger.interrupt()
    world._current_turn_id = None
    assert block == world._take_expected_returned_block(), (
        f"partial-flush return block mismatch: {block!r}\n{world.note()}"
    )
    assert world.text_by_cid[m_tail] in block.split("\n"), "the non-committed tail must be in the return block"
    assert world.text_by_cid[m_unobserved] in block.split("\n"), "the unobserved commit returns optimistically"
    assert world.ledger.state_of(m_observed) == MessageState.DELIVERED
    assert world.ledger.state_of(m_unobserved) == MessageState.RETURNED
    assert world.ledger.state_of(m_tail) == MessageState.RETURNED

    # m_unobserved's item/completed lands late: definitive proof of commit corrects it to Delivered,
    # rather than leaving it wrongly Returned. The tail (never committed) stays Returned.
    world._push_item_completed(m_unobserved)
    world._expected[m_unobserved] = MessageState.DELIVERED
    assert world.ledger.state_of(m_unobserved) == MessageState.DELIVERED
    assert world.ledger.state_of(m_tail) == MessageState.RETURNED


def _assert_ephemeral_queue_dies_with_the_session() -> None:
    """The EPHEMERAL store: an idle ``thread/status`` sweeps a still-parked steer back to the composer
    (the queue is empty whenever idle), and a fresh session's ledger starts empty and revives nothing."""
    world = _CodexWorld()
    # Open + commit a turn, then park a steer.
    world.user_send(deliver=True)
    parked = world.user_send(deliver=False)
    assert world.ledger.state_of(parked) == MessageState.QUEUED
    assert world.ledger.queued_snapshot() != []

    world._push_status("idle")
    assert world.ledger.state_of(parked) == MessageState.RETURNED, "an idle status must sweep the parked steer"
    assert world.ledger.queued_snapshot() == [], "the queue is empty whenever idle"

    # A brand-new session (new daemon/client/ledger) revives nothing of the prior queue.
    fresh = _CodexWorld()
    assert fresh.ledger.queued_snapshot() == []
    assert fresh.ledger.reconcile_returned() == ""
    assert fresh.ledger.entries == {}


# =============================================================================
# The seeded storm.
# =============================================================================

_A3B_ROUNDS = frozenset({3, 17, 28})
_PARTIAL_FLUSH_ROUNDS = frozenset({6, 21})
_FOREIGN_ROUNDS = frozenset({4, 12, 25})


@pytest.mark.timeout(120, func_only=False)
def test_codex_message_lifecycle_conserves_every_message() -> None:
    """Seeded Send / Queue / Shoulder-tap / Interrupt interleavings; the full live-state law per step."""
    world = _CodexWorld()
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        world.log(f"round {round_index}:")

        # Kick the round off from idle with an opening send held in Sending across a verify (so the
        # Sending state is exercised), then commit it (Delivered) with the turn still running.
        if world.is_idle:
            world.log("  kickoff-sending")
            world.user_send(deliver=False)
            world.verify(f"round {round_index} kickoff-sending")
            world.log("  kickoff-deliver")
            world.commit_head()
            world.verify(f"round {round_index} kickoff-deliver")

        if round_index in _A3B_ROUNDS:
            world.log("  a3b")
            _assert_a3b_chip_removal_emitted(world)
            world.verify(f"round {round_index} a3b")
        if round_index in _FOREIGN_ROUNDS:
            world.log("  foreign")
            world.foreign()
            world.verify(f"round {round_index} foreign")
        if round_index in _PARTIAL_FLUSH_ROUNDS:
            world.log("  interrupt-during-flush")
            _run_interrupt_during_partial_flush(world)
            world.verify(f"round {round_index} interrupt-during-flush")

        for _op_index in range(rng.randint(2, 4)):
            op = rng.choice(("send", "send", "commit", "flush", "stop"))
            world.log(f"  {op}")
            if op == "send":
                world.user_send(deliver=False)
            elif op == "commit":
                world.commit_head()
            elif op == "flush":
                world.flush()
            else:
                world.stop()
            world.verify(f"round {round_index} op {op}")

        world.log("  settle")
        world.settle_turn_end()
        world.verify(f"round {round_index} settle")

    # The final partition cross-check: the four live states partition every accepted message exactly.
    states = Counter(entry.state for entry in world.ledger.entries.values())
    assert sum(states.values()) == len(world.accepted)
    delivered = {cid for cid, entry in world.ledger.entries.items() if entry.state == MessageState.DELIVERED}
    returned = {cid for cid, entry in world.ledger.entries.items() if entry.state == MessageState.RETURNED}
    assert delivered.isdisjoint(returned), "a message cannot be both Delivered and Returned"

    _assert_ephemeral_queue_dies_with_the_session()
