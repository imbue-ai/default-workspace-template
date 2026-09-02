"""Live-state message conservation for claude across Send / Queue / Shoulder-tap / Interrupt.

The canonical enforcement the message-lifecycle contract asks for in Part D, for claude:
drive the four operations in seeded, randomized interleavings against the REAL executors
(``execute_claude_shoulder_tap`` / ``execute_claude_stop_to_composer`` / the shared
``restart_drain``) over a REAL :class:`ClaudeSessionWatcher` reading real session JSONL, and
after EVERY step assert the full live-state law:

- **Conservation (A1).** Every accepted message is, at each step boundary, in EXACTLY ONE of the
  four live states -- Delivered (a ``user_message`` committed on disk), Queued (a chip in the
  watcher's own queue mirror), Sending (an in-flight record in the backend's Sending registry),
  or Returned (text handed back to the composer). ``delivered + queued + sending + returned ==
  total_sent``; zero lost (a message in no state) and zero ghosts (a message in two states).
- **Order.** The queue mirror preserves enqueue (send) order; every Returned block is prepended
  to the composer in send order (queued first, in FIFO order, then any in-flight send).
- **A3b ordering.** For a Queued->Delivered transition the watcher emits the queue update (the
  chip REMOVAL) BEFORE the transcript turn -- never the turn while the chip still shows (no
  double-show, no gap). Verified by draining a controlled transition through the real poll loop
  and reading the ordered emission log.
- **Sending removal is backend-driven and ordered (A2).** The backend clears a message's Sending
  record only AFTER its real representation (queued chip or committed turn) is already visible --
  never before -- so the frontend's "Sending..." is never removed into a gap. Verified inside
  every send: the on-disk real state is asserted present before ``commit_sent_message`` runs.
- **Interrupt-during-flush (required).** A stop that fires when a flush has committed only a
  prefix of the parked queue: each already-committed message stays Delivered, each not-yet-
  committed message returns to the composer in send order. Plus the sibling not-committed case --
  a stop that fires while a message is still mid-send (holding ``message.lock`` past the bounded
  wait): the in-flight message rides the returned block instead of being lost.

The world plays claude's side on disk (enqueue records park; a leave + ``user`` record commits;
the cancel chord aborts or flushes-through; a restart drops the parked queue) exactly as the
sibling ``harnesses/conservation_storm_test.py`` does, but this test additionally tracks the
Sending and Queued LIVE states and reads every verdict off the backend, not the world's own
bookkeeping. Fully synchronous and seeded (injected clocks, no background thread), so a failure
is replayable from the seed in the assertion note.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.system_interface.harnesses.claude.tap import execute_claude_shoulder_tap
from imbue.system_interface.harnesses.claude.tap import execute_claude_stop_to_composer
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.sending_registry import SendingRegistry

pytestmark = pytest.mark.acceptance

_BASE_SEED = 20260811
_ROUND_COUNT = 30

# Logical clock base for the world's on-disk ISO timestamps and marker mtimes (an arbitrary past
# epoch; only the ordering matters, exactly as the sibling storm's ``_CLOCK_BASE``).
_CLOCK_BASE = 1_600_000_000.0

# The injected bounded-lock wait for the executors: kept tiny so the one in-flight-send corner
# resolves fast. The staged in-flight send holds the real flock, and the injected ``now`` forces
# the acquire to time out deterministically (see ``_run_stop_with_in_flight_send``).
_LOCK_WAIT_SECONDS = 0.2
_IN_FLIGHT_HOLD_SECONDS = 0.25


def _iso_timestamp(epoch_seconds: float) -> str:
    """An ISO-8601 UTC timestamp (claude session-record shape) for a logical epoch second."""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_seconds))
    millis = int(round((epoch_seconds % 1.0) * 1000.0))
    return f"{base}.{millis:03d}Z"


class _InFlightSend:
    """A REAL in-flight mngr send: holds the agent's ``message.lock`` flock, then parks-and-releases.

    Mirrors the sibling storm's helper. The exclusive flock is taken on a separate open file
    description in the driver thread, before the executor under test starts its bounded acquire, so
    the contention ordering is deterministic; a timer thread then runs ``deliver`` (the send's
    durable park in the dead epoch) and releases the lock.
    """

    def __init__(self, agent_state_dir: Path, hold_seconds: float, deliver: Callable[[], None]) -> None:
        lock_path = agent_state_dir / MESSAGE_LOCK_FILENAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(lock_path, "w")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)
        self._deliver = deliver
        self._released = threading.Event()
        self._timer = threading.Timer(hold_seconds, self._complete)
        self._timer.start()

    def _complete(self) -> None:
        try:
            self._deliver()
        finally:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._released.set()

    def join(self) -> None:
        is_released = self._released.wait(timeout=30.0)
        assert is_released, "the staged in-flight send never released message.lock"


class _Ledger:
    """Every accepted message and the world's op log, for the replayable failure note.

    The LIVE states are read from the backend at verify time (not stored here); this only records
    what was accepted, what text each returned block carried (in the order stop produced them), and
    the cumulative op log.
    """

    def __init__(self) -> None:
        self.accepted: list[str] = []
        self.returned: list[str] = []
        self.ops: list[str] = []

    def log(self, op: str) -> None:
        self.ops.append(op)

    def note(self) -> str:
        return "\n".join([f"REPLAY: seed={_BASE_SEED}"] + [f"  {op}" for op in self.ops])


class _ClaudeWorld:
    """Ground truth for the storm: a REAL session JSONL a simulated claude appends to, plus the
    REAL :class:`ClaudeSessionWatcher` (unstarted; synchronous reads) deriving the queue mirror and
    holding the backend Sending registry.

    ``emissions`` records the watcher's two broadcast channels in call order (the queue-snapshot
    callback and the ``on_events`` transcript callback) so the A3b ordering of a Queued->Delivered
    transition is observable off the real poll loop.
    """

    def __init__(self, root: Path, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.agent_state_dir = root / "state"
        self.agent_state_dir.mkdir()
        self.claude_config_dir = root / "config"
        session_dir = self.claude_config_dir / "projects" / "storm"
        session_dir.mkdir(parents=True)
        self.session_id = "storm-session"
        self.session_file = session_dir / f"{self.session_id}.jsonl"
        self.session_file.write_text("")
        (self.agent_state_dir / "claude_session_id_history").write_text(f"{self.session_id}\n")
        self.keybindings_path = self.claude_config_dir / "keybindings.json"
        self.keybindings_path.write_text(
            json.dumps({"bindings": [{"context": "Chat", "bindings": {"meta+q": "chat:cancel"}}]})
        )
        self.clock = _CLOCK_BASE
        os.utime(self.keybindings_path, (self.clock, self.clock))
        self.process_marker = self.agent_state_dir / "claude_process_started"
        self.process_marker.write_text("")
        self.clock += 5.0
        os.utime(self.process_marker, (self.clock, self.clock))
        self.active_marker = self.agent_state_dir / "active"

        self.parked: list[str] = []
        self.turn_open = False
        self.restart_count = 0
        self._generation = 0
        self._uuid_counter = 0
        self._message_counter = 0
        # token -> text of every not-yet-committed Sending record the world mirrors alongside the
        # backend registry, so a verify can reconcile the backend's in-flight block against it.
        self._sending: dict[str, str] = {}
        # The session-owned Sending registry (contract A1); the watcher is a pure reader now.
        self.sending = SendingRegistry.build()
        self.emissions: list[tuple[str, Any]] = []
        self.watcher = self._build_watcher()

    def _build_watcher(self) -> ClaudeSessionWatcher:
        agent_info = self._agent_info()
        watcher = ClaudeSessionWatcher.build(
            agent_info, on_events=lambda _agent_id, events: self.emissions.append(("turn", _user_texts(events)))
        )
        watcher.set_queue_snapshot_callback(
            lambda snapshot: self.emissions.append(("chips", [e["content"] for e in snapshot]))
        )
        return watcher

    def _agent_info(self) -> AgentInfo:
        return AgentInfo(
            id="claude-lifecycle-agent",
            name="claude-lifecycle-agent",
            state="RUNNING",
            agent_state_dir=self.agent_state_dir,
            claude_config_dir=self.claude_config_dir,
            harness=HarnessType.CLAUDE,
        )

    def new_text(self) -> str:
        self._message_counter += 1
        return f"claude-msg-{self._message_counter:03d}"

    def _tick(self) -> float:
        self.clock += 1.0
        return self.clock

    def _next_uuid(self) -> str:
        self._uuid_counter += 1
        return f"storm-uuid-{self._uuid_counter:04d}"

    def _append(self, record: dict[str, Any]) -> None:
        with self.session_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _append_enqueue(self, text: str, timestamp: str) -> None:
        self._append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": text,
                "timestamp": timestamp,
                "sessionId": self.session_id,
            }
        )

    def _append_leave(self) -> None:
        self._append({"type": "queue-operation", "operation": "dequeue", "sessionId": self.session_id})

    def _append_user(self, text: str) -> None:
        self._append(
            {
                "type": "user",
                "uuid": self._next_uuid(),
                "timestamp": _iso_timestamp(self._tick()),
                "sessionId": self.session_id,
                "message": {"role": "user", "content": text},
            }
        )

    def _append_assistant(self, text: str) -> None:
        self._append(
            {
                "type": "assistant",
                "uuid": self._next_uuid(),
                "timestamp": _iso_timestamp(self._tick()),
                "sessionId": self.session_id,
                "message": {"role": "assistant", "model": "storm-model", "content": [{"type": "text", "text": text}]},
            }
        )

    def open_turn(self) -> None:
        self.active_marker.write_text("")
        self.turn_open = True

    # --- the four operations, driving the REAL backend --------------------------------------

    def send(self) -> None:
        """A completed send through the backend's exact endpoint sequence (contract A2/A1/A4).

        note_sent_message (Sending) -> the harness record lands on disk (park when a turn is open,
        deliver otherwise) -> refresh so the real state is visible -> commit_sent_message clears
        Sending. The Sending-removal ordering is asserted between the last two: the real state must
        already be visible when the record is cleared, never after.
        """
        text = self.new_text()
        self.ledger.accepted.append(text)
        token = self.new_token()
        self.sending.record(token, text)
        self._sending[token] = text
        assert text in _split_block(self.sending.concatenated_block()), "a noted send must be in the Sending registry"

        will_park = self.turn_open
        if will_park:
            self._append_enqueue(text, _iso_timestamp(self._tick()))
        else:
            self._append_user(text)
            self.open_turn()

        # Refresh so the real representation (chip or committed turn) is on the backend BEFORE the
        # Sending record is cleared -- the backend-driven, ordered "Sending..." removal.
        self.watcher.get_all_events()
        real_states = set(self._backend_queued()) | set(self._backend_delivered())
        assert text in real_states, (
            f"the real state of {text!r} must be visible before its Sending record clears\n{self.ledger.note()}"
        )
        self.sending.resolve(token)
        del self._sending[token]
        if will_park:
            self.parked.append(text)

    def flush(self) -> None:
        """The REAL shoulder tap: cancel-flush the parked queue through the live turn.

        The injected ``press_chord`` plays claude's flush-through (a leave + ``user`` record per
        parked message, committed into a still-running turn), so the mirror drains and the executor
        resolves TAPPED. An empty queue or no open turn is the executor's own no-op.
        """
        result = execute_claude_shoulder_tap(
            agent_state_dir=self.agent_state_dir,
            keybindings_path=self.keybindings_path,
            watcher=self.watcher,
            press_chord=self._press_chord_flush,
            send_recovery=lambda _text: True,
            try_message_lock=lambda: try_hold_message_lock(self.agent_state_dir, wait_seconds=_LOCK_WAIT_SECONDS),
        )
        # The status is the executor's own concern (the sibling tap storm asserts the lattice); here
        # only conservation matters, verified by the caller after the op.
        _ = result

    def stop(self) -> None:
        """The REAL stop-to-composer executor; return the block into the composer (Returned)."""
        block = execute_claude_stop_to_composer(
            agent_state_dir=self.agent_state_dir,
            keybindings_path=self.keybindings_path,
            watcher=self.watcher,
            press_chord=self._press_chord_stop,
            mark_idle=self.mark_idle,
            restart_drain_to_base=self._restart_drain_to_base,
            try_message_lock=lambda: try_hold_message_lock(self.agent_state_dir, wait_seconds=_LOCK_WAIT_SECONDS),
            get_in_flight_block=self.sending.concatenated_block,
        )
        self._absorb_returned_block(block)

    # --- claude's side, played on disk ------------------------------------------------------

    def _press_chord_stop(self) -> bool:
        """The cancel chord on an EMPTY queue: a pure abort. The sentinel lands; the turn dies."""
        self._append_user(INTERRUPT_SENTINEL_TEXT)
        self.turn_open = False
        return True

    def _press_chord_flush(self) -> bool:
        """The cancel chord on a NONEMPTY queue: the flush-through. The parked queue commits as a
        merged turn that keeps running."""
        self._append_user(INTERRUPT_SENTINEL_TEXT)
        self._commit_parked()
        return True

    def _commit_parked(self) -> None:
        for text in self.parked:
            self._append_leave()
            self._append_user(text)
        self.parked = []

    def commit_one_parked(self) -> str | None:
        """Commit the FIFO-head parked message (a flush mid-way): a leave + ``user`` record."""
        if not self.parked:
            return None
        text = self.parked.pop(0)
        self._append_leave()
        self._append_user(text)
        return text

    def mark_idle(self) -> None:
        self.active_marker.unlink(missing_ok=True)

    def restart_process(self) -> tuple[bool, str]:
        """The SIGKILL-relaunch: parked enqueues dangle (their epoch died); the process marker's
        mtime advances past them; the relaunched process is idle."""
        self.restart_count += 1
        self._generation += 1
        self.parked = []
        self.turn_open = False
        self.active_marker.unlink(missing_ok=True)
        restart_time = self._tick()
        os.utime(self.process_marker, (restart_time, restart_time))
        return (True, "ok")

    def _restart_drain_to_base(self) -> str:
        return restart_drain(self._agent_info(), self.watcher, self.restart_process, lambda: None)

    def new_token(self) -> str:
        return f"token-{self._message_counter:03d}-{self._uuid_counter:04d}"

    def begin_inflight_send(self, text: str) -> _InFlightSend:
        """Stage an in-flight send holding the real ``message.lock``, resolving in the dead epoch."""
        self.ledger.accepted.append(text)
        timestamp = _iso_timestamp(self._tick())
        generation = self._generation

        def deliver() -> None:
            # The paste raced the SIGKILL: the enqueue lands in a dead epoch (dangling, never runs),
            # exactly as a send caught mid-flight by a restart does.
            self._append_enqueue(text, timestamp)
            _ = generation

        return _InFlightSend(self.agent_state_dir, _IN_FLIGHT_HOLD_SECONDS, deliver)

    # --- reading the backend's live states --------------------------------------------------

    def _backend_delivered(self) -> list[str]:
        events = self.watcher.get_all_events()
        return _user_texts(events)

    def _backend_queued(self) -> list[str]:
        self.watcher.get_all_events()
        return [entry["content"] for entry in self.watcher.get_queued_messages()]

    def _backend_sending(self) -> list[str]:
        return _split_block(self.sending.concatenated_block())

    def _absorb_returned_block(self, block: str) -> None:
        for text in _split_block(block):
            self.ledger.returned.append(text)
            # A returned in-flight send's Sending record is cleared by the send handler's retract
            # (server.py) once the SIGKILL-failed send returns; mirror that so the message settles
            # in exactly the Returned state.
            for token, sending_text in list(self._sending.items()):
                if sending_text == text:
                    self.sending.resolve(token)
                    del self._sending[token]

    def settle_turn_end(self) -> None:
        """Natural turn end: the auto-flush commits the parked queue, the Stop hook settles."""
        if self.parked:
            self._commit_parked()
        if self.turn_open:
            self._append_assistant("ok")
            self.turn_open = False
        self.active_marker.unlink(missing_ok=True)

    # --- the per-step conservation law ------------------------------------------------------

    def verify(self, context: str) -> None:
        note = self.ledger.note()
        delivered = Counter(self._backend_delivered())
        queued = Counter(self._backend_queued())
        sending = Counter(self._backend_sending())
        returned = Counter(self.ledger.returned)

        # Every state must hold real messages once each (a stray blank / phantom would break the sum).
        for name, counts in (("delivered", delivered), ("queued", queued), ("sending", sending)):
            dupes = {text: n for text, n in counts.items() if n > 1}
            assert not dupes, f"{name} shows a message more than once ({context}): {dupes}\n{note}"

        accepted = set(self.ledger.accepted)
        for text in accepted:
            states = [
                name
                for name, counts in (
                    ("delivered", delivered),
                    ("queued", queued),
                    ("sending", sending),
                    ("returned", returned),
                )
                if counts.get(text, 0) > 0
            ]
            assert states != [], f"LOST: {text!r} is in no live state ({context})\n{note}"
            assert len(states) == 1, f"GHOST: {text!r} is in multiple states {states} ({context})\n{note}"

        total = sum(counts.total() for counts in (delivered, queued, sending, returned))
        assert total == len(self.ledger.accepted), (
            f"conservation sum {total} != total accepted {len(self.ledger.accepted)} "
            f"(delivered={delivered.total()} queued={queued.total()} sending={sending.total()} "
            f"returned={returned.total()}) ({context})\n{note}"
        )

        # Order: the queue mirror is the still-parked set in enqueue (send) order.
        assert self._backend_queued() == self.parked, (
            f"queue mirror {self._backend_queued()} != live parked {self.parked} ({context})\n{note}"
        )
        # Returns are prepended in send order: the ledger's returned list is a subsequence of the
        # accepted (send) order.
        assert _is_subsequence(self.ledger.returned, self.ledger.accepted), (
            f"returned {self.ledger.returned} is not in send order ({context})\n{note}"
        )


def _user_texts(events: list[dict[str, Any]]) -> list[str]:
    """Every committed non-meta user turn's text, in order (the Delivered set on disk)."""
    return [
        event["content"]
        for event in events
        # A genuine user turn carries no render decision; framework injections (resume
        # markers, compaction summaries, model-bar echoes) arrive with `display` set.
        if event.get("type") == "user_message" and event.get("display") is None
    ]


def _split_block(block: str) -> list[str]:
    return block.split("\n") if block else []


def _is_subsequence(subset: list[str], full: list[str]) -> bool:
    """True iff ``subset`` appears in ``full`` in the same relative order (no reordering)."""
    it = iter(full)
    return all(item in it for item in subset)


# =============================================================================
# The required deterministic corners (A3b ordering, interrupt-during-flush).
# =============================================================================


def _assert_a3b_chip_removed_before_turn(world: _ClaudeWorld) -> None:
    """A controlled Queued->Delivered through the REAL poll loop: chip removal emits before the turn.

    Enqueue a fresh message while a turn is open and poll -> a chip is pushed. Commit it (leave +
    ``user`` record) and poll ONE cycle -> the queue-snapshot (chip REMOVAL) must be emitted before
    the transcript turn, so the message is never a chip and a turn at once (A3b).
    """
    if not world.turn_open:
        opener = world.new_text()
        world.ledger.accepted.append(opener)
        world._append_user(opener)
        world.open_turn()
    # Flush any backlog through the poll loop first so ``emitted_count`` is current; the two polls
    # below then each carry exactly the one transition under test.
    world.watcher._emit_cycle()

    text = world.new_text()
    world.ledger.accepted.append(text)
    world._append_enqueue(text, _iso_timestamp(world._tick()))
    world.parked.append(text)
    world.emissions.clear()
    world.watcher._emit_cycle()
    assert text in [c for kind, payload in world.emissions if kind == "chips" for c in payload], (
        "the enqueue must push a chip through the poll loop"
    )

    world.emissions.clear()
    # Commit the parked message (its leave + user record) and drain one poll cycle.
    world.commit_one_parked()
    world.watcher._emit_cycle()
    kinds = [kind for kind, _payload in world.emissions]
    assert "chips" in kinds and "turn" in kinds, f"the commit cycle must emit both channels: {world.emissions}"
    assert kinds.index("chips") < kinds.index("turn"), (
        f"A3b: the chip removal must be emitted before the transcript turn, got {kinds}"
    )
    # `text` left the queue (removal) and arrived as a turn; close the turn so the round continues.
    world.settle_turn_end()


def _run_interrupt_during_partial_flush(world: _ClaudeWorld) -> None:
    """A stop that fires when a flush has committed only a PREFIX of the parked queue.

    Park three messages, commit the FIFO head (the flush got that far), then run the REAL stop: the
    committed head stays Delivered, the two not-yet-committed messages return to the composer in
    send order.
    """
    heads = [world.new_text() for _ in range(3)]
    for text in heads:
        world.ledger.accepted.append(text)
        world._append_enqueue(text, _iso_timestamp(world._tick()))
        world.parked.append(text)
    if not world.turn_open:
        world.open_turn()
    world.watcher.get_all_events()

    committed = world.commit_one_parked()
    assert committed == heads[0]
    world.watcher.get_all_events()

    block = execute_claude_stop_to_composer(
        agent_state_dir=world.agent_state_dir,
        keybindings_path=world.keybindings_path,
        watcher=world.watcher,
        press_chord=world._press_chord_stop,
        mark_idle=world.mark_idle,
        restart_drain_to_base=world._restart_drain_to_base,
        try_message_lock=lambda: try_hold_message_lock(world.agent_state_dir, wait_seconds=_LOCK_WAIT_SECONDS),
        get_in_flight_block=world.sending.concatenated_block,
    )
    returned = _split_block(block)
    assert returned == heads[1:], f"the not-committed prefix must return in send order: {returned} vs {heads[1:]}"
    world._absorb_returned_block(block)
    assert committed in world._backend_delivered(), "the committed head must stay delivered"


def _run_stop_with_in_flight_send(world: _ClaudeWorld) -> None:
    """A stop that fires while a message is still mid-send (holding ``message.lock`` past the wait).

    The in-flight (Sending) message never committed, so the stop must fold it into the returned
    block (contract A4/B). Deterministic: the staged send holds the REAL flock and the injected
    ``now`` forces the executor's bounded acquire to time out at once.
    """
    parked_lead = world.new_text()
    world.ledger.accepted.append(parked_lead)
    world._append_enqueue(parked_lead, _iso_timestamp(world._tick()))
    world.parked.append(parked_lead)
    if not world.turn_open:
        world.open_turn()
    world.watcher.get_all_events()

    in_flight_text = world.new_text()
    token = world.new_token()
    world.sending.record(token, in_flight_text)
    world._sending[token] = in_flight_text
    sender = world.begin_inflight_send(in_flight_text)

    # A monotonic clock that jumps past the bounded deadline after the first (failed) flock attempt,
    # so the acquire deterministically reports "still held" while the send's real flock is up.
    ticks = iter([0.0] + [_LOCK_WAIT_SECONDS + 100.0] * 64)

    def forced_now() -> float:
        return next(ticks, _LOCK_WAIT_SECONDS + 100.0)

    block = execute_claude_stop_to_composer(
        agent_state_dir=world.agent_state_dir,
        keybindings_path=world.keybindings_path,
        watcher=world.watcher,
        press_chord=world._press_chord_stop,
        mark_idle=world.mark_idle,
        restart_drain_to_base=world._restart_drain_to_base,
        try_message_lock=lambda: try_hold_message_lock(
            world.agent_state_dir, wait_seconds=_LOCK_WAIT_SECONDS, now=forced_now, sleep=lambda _s: None
        ),
        get_in_flight_block=world.sending.concatenated_block,
    )
    sender.join()
    returned = _split_block(block)
    assert parked_lead in returned, f"the parked lead must return: {returned}"
    assert in_flight_text in returned, f"the in-flight send must ride the returned block: {returned}"
    assert returned.index(parked_lead) < returned.index(in_flight_text), (
        f"the queued message must lead the in-flight send in the block: {returned}"
    )
    world._absorb_returned_block(block)
    # The dead-epoch enqueue the send finally wrote must not re-derive as a ghost chip.
    world.watcher.get_all_events()


# =============================================================================
# The seeded storm.
# =============================================================================

# Rounds that force the two required not-committed corners so the seed always exercises them.
_PARTIAL_FLUSH_ROUNDS = frozenset({6, 21})
_IN_FLIGHT_STOP_ROUNDS = frozenset({11, 26})
_A3B_ROUNDS = frozenset({3, 17, 28})


@pytest.mark.timeout(120, func_only=False)
def test_claude_message_lifecycle_conserves_every_message(tmp_path: Path) -> None:
    """Seeded Send / Queue / Shoulder-tap / Interrupt interleavings; the full live-state law per step."""
    ledger = _Ledger()
    world = _ClaudeWorld(tmp_path, ledger)
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        ledger.log(f"round {round_index}:")
        if not world.turn_open:
            ledger.log("  kickoff-send")
            world.send()
            world.verify(f"round {round_index} kickoff")

        if round_index in _A3B_ROUNDS:
            ledger.log("  a3b-ordering")
            _assert_a3b_chip_removed_before_turn(world)
            world.verify(f"round {round_index} a3b")
        if round_index in _PARTIAL_FLUSH_ROUNDS:
            ledger.log("  interrupt-during-flush")
            _run_interrupt_during_partial_flush(world)
            world.verify(f"round {round_index} interrupt-during-flush")
        if round_index in _IN_FLIGHT_STOP_ROUNDS:
            ledger.log("  stop-with-in-flight-send")
            _run_stop_with_in_flight_send(world)
            world.verify(f"round {round_index} stop-with-in-flight")

        for _op_index in range(rng.randint(2, 4)):
            op = rng.choice(("send", "send", "flush", "stop"))
            ledger.log(f"  {op}")
            if op == "send":
                world.send()
            elif op == "flush":
                world.flush()
            else:
                world.stop()
            world.verify(f"round {round_index} op {op}")

        world.settle_turn_end()
        world.verify(f"round {round_index} settle")

    # The on-disk cross-check: the committed user turns are exactly the Delivered set, once each, and
    # never a Returned message (conservation read straight off disk).
    delivered = Counter(world._backend_delivered())
    for text in ledger.returned:
        assert delivered.get(text, 0) == 0, (
            f"{text!r} was returned to the composer yet appears as a delivered user turn\n{ledger.note()}"
        )
    assert set(delivered) | set(ledger.returned) | set(world._backend_queued()) | set(world._backend_sending()) == set(
        ledger.accepted
    ), f"the final live states must partition every accepted message\n{ledger.note()}"
