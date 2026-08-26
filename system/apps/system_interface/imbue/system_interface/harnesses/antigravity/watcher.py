"""Tail an antigravity (agy) agent's own conversation store and emit UI events.

Like the claude/codex watchers, this reads agy's OWN transcript -- never mngr's mirror.
agy stores each conversation as a protobuf SQLite ``.db`` (``steps`` table), so tailing is
SQLite row-offset polling rather than byte-offset file reading: each poll queries rows past
a per-conversation cursor, decodes them (:mod:`agy_transcript`), and maps them to events
(:mod:`session_parser`).

Two-phase emission gives a live activity caption: a tool step's ``tool_call`` is emitted as
soon as its row appears (even ``RUNNING``), its ``tool_result`` only once the row settles.
The cursor advances only through the leading run of terminal rows, so a row seen while
running is re-read (and its result added) once it settles; dedup by ``event_id`` keeps the
already-emitted call from repeating. A row still mid-write decodes to ``TruncatedError`` and
stops the scan for that conversation until the next pass.

No subagents in this cut: agy's ``invoke_subagent`` opens a separate conversation, which we
do not follow yet (``get_subagent_metadata`` -> None, ``is_main_session_event`` -> True).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final

from loguru import logger
from watchdog.observers import Observer

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.agy_transcript import TruncatedError
from imbue.system_interface.harnesses.antigravity.agy_transcript import decode_step
from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import OUTBOX_FILENAME
from imbue.system_interface.harnesses.antigravity.queue_tracker import get_tracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import session_token
from imbue.system_interface.harnesses.antigravity.session_parser import parse_step
from imbue.system_interface.harnesses.antigravity.turn_state import TurnState
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.harnesses.session_watcher import QueueSnapshotCallback
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

# agy's per-agent conversation store + the capture-hook file listing this agent's
# conversation ids, both relative to the mngr agent state dir.
_CONVERSATIONS_RELATIVE = Path("plugin") / "antigravity" / "home" / ".gemini" / "antigravity-cli" / "conversations"
_CONVERSATION_IDS_RELATIVE = Path("antigravity_conversation_ids")

_STEPS_QUERY = "SELECT idx, step_type, status, step_payload FROM steps WHERE idx >= ? ORDER BY idx"


# How long the flush worker waits for agy to be reachable before giving up on one attempt.
# A failed attempt leaves the queue intact and re-arms, so this bounds a try, not the queue.
# mngr stamps this on every launch and resume, so its mtime is the current agy process's
# start boundary. Named here rather than imported from the activity tracker to keep the
# watcher independent of it (they are wired together only through the registry).
_PROCESS_STARTED_MARKER_FILENAME: Final[str] = "antigravity_process_started"

_FLUSH_RETRY_SECONDS: Final[float] = 5.0

# How long to wait, after mngr reports a send accepted, for agy's store to show the turn our
# block opened. mngr's ack fires at the BUSY edge -- before agy has written the user row -- so
# sampling at the ack would call every real delivery a failure and re-paste the block.
_DELIVERY_WITNESS_SECONDS: Final[float] = 15.0
_DELIVERY_POLL_SECONDS: Final[float] = 0.25

# Wedge-breaker for the emit embargo (see ``_attempt_flush``), NOT its functional bound -- the
# ``finally`` clears the embargo on every path a flush can leave by. This only decides how long
# the transcript stays muted if the flush THREAD itself dies mid-send, so it has to outlast a
# real send (mngr's message.lock wait + TUI-ready + confirm) plus the full witness window.
# A timestamp rather than a bool for exactly that reason: a bool left set by a lost thread mutes
# the transcript permanently, which is far worse than the ordering bug it exists to fix.
# ponytail: one fixed ceiling; derive it from mngr's own send timeout if that ever moves.
_EMIT_EMBARGO_CEILING_SECONDS: Final[float] = 120.0


class AntigravitySessionWatcher(AgentSessionWatcher):
    """Watches an agy agent's conversation ``.db``(s) and emits parsed UI events."""

    _agent_id: str
    _state_dir: Path
    _on_events: Callable[[str, list[dict[str, Any]]], None]
    _lock: threading.Lock
    _events: list[dict[str, Any]]
    _index_by_id: dict[str, int]
    _emitted_ids: set[str]
    _scan_from: dict[str, int]
    _wake: threading.Event
    _stopping: threading.Event
    _thread: threading.Thread | None
    # The queue we hold on agy's behalf, its delivery capabilities, and the worker that
    # performs the delivery. See system/apps/system_interface/imbue/system_interface/harnesses/core-contracts/messages-lifecycle-contract-state-of-things.md.
    _queue: AntigravityQueueTracker
    _turn_state: TurnState
    _queue_snapshot_callback: Any
    _flush_send: Any
    _flush_is_alive: Any
    _flush_wake: threading.Event
    _flush_thread: threading.Thread | None
    _emit_embargo_until: float
    # Instance-level so a test can shorten it by assignment. A module constant would have to be
    # monkeypatched, which the ratchets forbid (and rightly -- it leaks across tests).
    _delivery_witness_seconds: float
    _observer: Any

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "AntigravitySessionWatcher":
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._state_dir = agent_info.agent_state_dir
        self._on_events = on_events
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._index_by_id: dict[str, int] = {}
        self._emitted_ids: set[str] = set()
        # Per-conversation cursor: the lowest idx not yet known-terminal (re-read until it is).
        self._scan_from: dict[str, int] = {}
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._emit_embargo_until = 0.0
        self._delivery_witness_seconds = _DELIVERY_WITNESS_SECONDS
        self._thread: threading.Thread | None = None
        # The session's identity: the marker mngr stamps on every launch/resume. A journal
        # written under a different token belongs to a session that has since restarted, and
        # the contract says such a queue is gone -- never replayed, never delivered.
        self._queue = get_tracker(self._agent_id, self._state_dir / OUTBOX_FILENAME, session_token(self._state_dir))
        self._turn_state = get_turn_state(self._agent_id)
        self._queue_snapshot_callback = None
        self._flush_send = None
        self._flush_is_alive = None
        self._flush_wake = threading.Event()
        self._flush_thread = None
        self._observer: Any = None
        return self

    # --- paths ---------------------------------------------------------------------------

    def _conversations_dir(self) -> Path:
        return self._state_dir / _CONVERSATIONS_RELATIVE

    def _conversation_ids_file(self) -> Path:
        return self._state_dir / _CONVERSATION_IDS_RELATIVE

    def _conversation_ids(self) -> list[str]:
        """This agent's conversation ids in order, from the capture-hook file (deduped)."""
        path = self._conversation_ids_file()
        if not path.is_file():
            return []
        seen: list[str] = []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            candidate = line.strip()
            if candidate and candidate not in seen:
                seen.append(candidate)
        return seen

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Prime the backlog WITHOUT broadcasting it -- it is delivered via the REST tail path,
        # not the live stream (mirrors claude's prime-vs-poll split).
        self._stopping.clear()
        # Bind the publisher and the wake BEFORE anything can enqueue, so the very first
        # message is announced and delivered rather than sitting until the next tick.
        self._queue.attach(publish=self._publish_snapshot, wake=self._flush_wake.set)
        with self._lock:
            self._collect_new_events()
            # Publish during priming, not one poll later: an unpublished turn state makes the
            # first send after every backend restart fall back to the marker alone.
            self._publish_turn_state()
        self._observer = Observer()
        handler = WakeOnChangeHandler(self._wake)
        # Watch whatever exists now; the 1s poll safety net covers a dir/file created later.
        for directory in (self._state_dir, self._conversations_dir()):
            if directory.is_dir():
                self._observer.schedule(handler, str(directory), recursive=False)
        self._observer.start()
        self._thread = threading.Thread(target=self._run, name=f"agy-watcher-{self._agent_id}", daemon=True)
        self._thread.start()
        # Its own thread: a flush runs mngr's send, which blocks on a lock and a bounded
        # confirmation, and must never sit on the transcript thread.
        self._flush_thread = threading.Thread(
            target=self._run_flush_worker, name=f"agy-flush-{self._agent_id}", daemon=True
        )
        self._flush_thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        self._flush_wake.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._flush_thread is not None:
            # Longer than the transcript thread's: a flush may be inside mngr's send, and
            # abandoning it mid-keystroke would leave a half-typed message in agy's composer.
            self._flush_thread.join(timeout=10.0)
            if self._flush_thread.is_alive():
                # It is inside mngr's send (bounded at 90s), so it will outlive us. Detaching
                # stops it publishing into a dead callback; its settle carries a generation
                # this tracker has since moved past, so it cannot resurrect anything either.
                logger.warning("antigravity: {}'s flush outlived teardown; detaching it", self._agent_id)
            self._flush_thread = None
        # The tracker is NOT dropped: it is the agent's for the agent's life, and a queue held
        # across a watcher restart is still the same live session's. Only detach the wiring.
        self._queue.detach()
        drop_turn_state(self._agent_id)

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake.clear()
            if self._stopping.is_set():
                return
            self._poll_once()

    def _poll_once(self) -> None:
        """One poll tick: adopt the session, collect, publish, emit.

        Extracted from the loop so a test can drive a tick deterministically -- the A3b
        ordering guarantee is between THIS and the flush worker, so a test that cannot
        interleave them cannot see the bug (the original depart-before-arrive test ran on a
        watcher whose poll thread was never started, which is why the race shipped green).
        """
        # The ONLY caller of set_session. Adopting a new agy session discards the old
        # one's queue, so it must never happen as a side effect of a read -- a broadcast
        # asking "is the tap available?" used to be able to trigger exactly that.
        self._queue.set_session(session_token(self._state_dir))
        with self._lock:
            # THE EMBARGO (contract A3b). While a flush holds the claim, this thread must not
            # emit: `_collect_new_events` is destructive, so whichever thread sees the
            # delivered row first is the one that emits it, and this one wakes on agy's own
            # sqlite write (the watchdog handler) -- so it wins essentially every time and
            # puts the committed turn on screen while its chip is still showing. The flush
            # worker is already collecting on its own cadence inside the witness window and
            # releases what it saw only AFTER finish_flush, so skipping the collect here
            # loses nothing; it just stops this thread overtaking that ordering.
            is_embargoed = time.monotonic() < self._emit_embargo_until
            pending = [] if is_embargoed else self._collect_new_events()
            # Published unconditionally, embargo or not: the hold decision and the activity
            # dot must never read a staler transcript than the flush worker is acting on.
            self._publish_turn_state()
        if pending:
            self._on_events(self._agent_id, pending)

    # --- scanning ------------------------------------------------------------------------

    def _collect_new_events(self) -> list[dict[str, Any]]:
        """Scan every conversation for new/settled rows; append + return newly-emitted events.
        Caller holds ``self._lock``."""
        pending: list[dict[str, Any]] = []
        for conv_id in self._conversation_ids():
            db_path = self._conversations_dir() / f"{conv_id}.db"
            if db_path.is_file():
                self._scan_conversation(conv_id, db_path, pending)
        return pending

    def _scan_conversation(self, conv_id: str, db_path: Path, pending: list[dict[str, Any]]) -> None:
        scan_from = self._scan_from.get(conv_id, 0)
        rows = self._read_rows(db_path, scan_from)
        terminal_prefix_end = scan_from - 1
        for idx, step_type, status, payload in rows:
            try:
                decoded = decode_step(conv_id, idx, step_type, status, bytes(payload))
            except TruncatedError:
                # This row is mid-write; stop here so it is re-read whole next pass.
                break
            for event in parse_step(decoded):
                event_id = event["event_id"]
                if event_id in self._emitted_ids:
                    continue
                self._emitted_ids.add(event_id)
                self._index_by_id[event_id] = len(self._events)
                self._events.append(event)
                pending.append(event)
            # Advance the cursor only through the unbroken leading run of terminal rows, so a
            # still-running row (and anything after it) is re-scanned until it settles.
            if decoded.is_terminal and idx == terminal_prefix_end + 1:
                terminal_prefix_end = idx
        self._scan_from[conv_id] = terminal_prefix_end + 1

    def _read_rows(self, db_path: Path, scan_from: int) -> list[tuple[int, int, int, bytes]]:
        # Read-only + WAL-aware; agy is concurrently writing. A transient lock/checkpoint
        # surfaces as sqlite3.Error -> skip this conversation this pass, retry next.
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            return connection.execute(_STEPS_QUERY, (scan_from,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    # --- read interface (single flat session; subagents not surfaced) --------------------

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events[-limit:]) if limit > 0 else []

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            index = self._index_by_id.get(before_event_id)
            if index is None:
                return []
            return list(self._events[max(0, index - limit) : index])

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            index = self._index_by_id.get(after_event_id)
            if index is None:
                return []
            return list(self._events[index + 1 : index + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if offset < 0 or limit <= 0:
                return []
            return list(self._events[offset : offset + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        with self._lock:
            return self._index_by_id.get(event_id, -1)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True

    # --- queued messages: the queue we hold on agy's behalf ---------------------------
    #
    # Every other harness mirrors a queue its harness keeps. agy parks mid-turn input
    # invisibly inside its TUI, so instead we hold the messages and deliver them ourselves
    # once agy goes idle. These overrides are what make the shared consumers -- the WS
    # snapshot, stop's return block, the tap's availability gate -- see a real queue.

    def set_queue_snapshot_callback(self, callback: QueueSnapshotCallback) -> None:
        self._queue_snapshot_callback = callback

    def set_flush_hooks(self, send: Any, is_alive: Any) -> None:
        """Receive the ability to deliver, and to tell whether the agent is still alive."""
        self._flush_send = send
        self._flush_is_alive = is_alive

    def get_queued_messages(self) -> list[dict[str, Any]]:
        return self._queue.snapshot()

    def get_queued_block(self) -> str:
        return self._queue.concatenated_block()

    def clear_queue(self) -> None:
        """Drop the queue without delivering it.

        Only for a session that no longer exists. Stop and the tap must NOT use this: it
        cannot distinguish the entries they accounted for from ones the user sent while they
        were working, and wiping the latter leaves them in no state at all.
        """
        self._queue.clear()

    def take_unclaimed_queue(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return the unclaimed queue as one block -- stop's return path."""
        return self._queue.take_unclaimed()

    def take_whole_queue(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return EVERY entry -- stop's restart path, where the send dies too."""
        return self._queue.take_all()

    def claim_queue_for_tap(self) -> tuple[str, tuple[str, ...], int]:
        """Claim the queue on the tap's behalf, so the button greys for its whole run.

        Same primitive the flush worker claims with, which is what makes the two mutually
        exclusive: whichever gets there first, the other is refused.
        """
        return self._queue.begin_flush()

    def release_tap_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        """Hand a tap's claim back unsettled -- the worker delivers, not the tap."""
        self._queue.release_claim(claimed, generation)

    def is_turn_open(self) -> bool:
        """Whether a turn is open, per the bounded predicate (see turn_state)."""
        return self._turn_state.is_hold_required(self._state_dir)

    def turn_state(self) -> TurnState:
        """The shared reading, for the tap/stop executors' cancel interlock."""
        return self._turn_state

    def notify_idle(self) -> list[dict[str, Any]]:
        """The working->IDLE backstop. For agy this ARMS the flush; it never delivers here.

        Delivering on this call would run mngr's send -- a blocking lock, a TUI-ready wait,
        and a confirmation bounded at 90s -- on whichever thread drove the recompute, which
        is normally this watcher's own. That would stall transcript parsing and the activity
        indicator for the duration. So this only wakes the worker and returns the snapshot
        unchanged; the worker checks liveness and does the sending off-thread.
        """
        self._flush_wake.set()
        return self._queue.snapshot()

    def _publish_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        """The tracker's publisher: it calls this from inside its own lock."""
        callback = self._queue_snapshot_callback
        if callback is not None:
            callback(snapshot)

    def _publish_turn_state(self) -> None:
        """Hand the transcript to the shared reading. Caller holds ``self._lock``."""
        self._turn_state.publish(self._events, self._process_started_at())

    def _process_started_at(self) -> float | None:
        try:
            return (self._state_dir / _PROCESS_STARTED_MARKER_FILENAME).stat().st_mtime
        except OSError:
            return None

    # --- the flush worker: the ONLY typist ---------------------------------------------

    def _run_flush_worker(self) -> None:
        """Deliver the held queue when no turn is open. Own thread, so a slow send stalls nothing.

        This is the only code in the system that types into agy. ``session.send`` enqueues and
        wakes this loop; the tap cancels and wakes this loop; nothing else delivers. One typist
        is what removes the check-then-act window that let a message land in a live turn.
        """
        while not self._stopping.is_set():
            self._flush_wake.wait(timeout=_FLUSH_RETRY_SECONDS)
            self._flush_wake.clear()
            if self._stopping.is_set():
                return
            try:
                self._attempt_flush()
            # Never let one bad attempt kill the worker.
            except Exception as error:
                logger.opt(exception=error).warning("antigravity: flush attempt failed for {}", self._agent_id)

    def _attempt_flush(self) -> None:
        """One delivery attempt. The gate, the send, and the verdict.

        Order is load-bearing:

        1. **Republish**, always. Level-triggered visibility: an untrack/re-track cycle drops
           the manager's cached queue, and without this nothing restores the chips until the
           next mutation -- A1a's forbidden "resurfaces later".
        2. **Liveness.** mngr's send auto-starts a stopped agent, so delivering to a dead one
           would resurrect it. The queue is RETURNED, never cleared: a message the user sent
           and saw accepted must end up somewhere, and silently deleting it is the swallow
           wearing a different hat.
        3. **Is a turn open?** The whole bug was that this question was never asked. Bounded on
           every rung, so a cancelled or abandoned turn cannot wedge the queue forever.
        4. **Claim.** The tracker refuses a second claim while one is open, which is the mutual
           exclusion between every typist.
        5. **Send, then look.** mngr's ack is the busy marker's mtime, which advances even for
           a message that merely parked -- so the ack is not evidence. The evidence is agy's
           own store gaining a user turn whose text is ours.
        6. **Embargo the poll thread** for the span of the send, so the turn our block becomes
           departs the queue before it arrives in the transcript (A3b) no matter which thread
           happened to scan the row first.
        """
        send, is_alive = self._flush_send, self._flush_is_alive
        if send is None or is_alive is None:
            return
        self._queue.republish()
        if not self._queue.has_entries():
            return
        if not is_alive():
            self._return_queue_to_composer()
            return
        if self.is_turn_open():
            # A turn opened before we got here, so anything presented as "about to be typed"
            # is genuinely parked now and must read as queued rather than Sending.
            self._queue.demote_pending()
            return
        block, claimed, generation = self._queue.begin_flush()
        if not claimed:
            return
        before = self._turn_state.user_turn_texts()
        delivered: tuple[str, ...] = ()
        witnessed: list[dict[str, Any]] = []
        try:
            # Mute the poll thread for the span of this flush, so the turn our block becomes
            # cannot be emitted by it before the entry departs below. Set INSIDE the try, so
            # every path out of here runs the finally that clears it.
            self._emit_embargo_until = time.monotonic() + _EMIT_EMBARGO_CEILING_SECONDS
            if send(block):
                delivered, witnessed = self._observe_delivery(before, block, claimed)
        finally:
            # DEPART BEFORE ARRIVE (contract A3b). The queue entry is removed FIRST, and only
            # then are the transcript events it turned into released. Emitting as we found them
            # -- which is what the witness loop used to do -- put the committed turn on screen
            # while the entry was still showing as Sending: one message in two states at once,
            # and the "chat, then still queued, then gone" blip.
            self._queue.finish_flush(claimed, generation, delivered=delivered)
            if not delivered:
                # No immediate re-arm: the retry cadence is this loop's own timeout. Re-arming
                # here turned a persistently failing send into a hot loop that re-pasted the
                # whole block, re-ran discovery and re-broadcast on every iteration.
                logger.info("antigravity: {} did not witness a turn for its block", self._agent_id)
            if witnessed:
                self._on_events(self._agent_id, witnessed)
            # Cleared LAST, after the witnessed batch has gone out. Clearing it before that
            # emit would let the poll thread wake in between and ship a later row first,
            # inverting the transcript -- the ordering this whole embargo exists to protect.
            self._emit_embargo_until = 0.0

    def _observe_delivery(
        self, before: tuple[str, ...], block: str, claimed: tuple[str, ...]
    ) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
        """Which claimed ids agy committed, plus the events seen while looking.

        Returns ``(delivered_ids, witnessed_events)``. It deliberately does NOT emit those
        events itself: the caller releases them only after the queue entry has been removed,
        so the turn never appears while its entry is still on screen (contract A3b).

        NOT "did a turn open": a turn opened by the human at the tmux pane, or by ``mngr
        message`` from cron, would resolve our entries while our block sat parked. The text is
        the only thing that identifies the block as ours.

        Measured on agy 1.1.20, a newline-joined block commits as exactly one turn (an embedded
        newline is inserted in the composer, not submitted), so the whole-block arm is the live
        path. The prefix arm is defence in depth for a block that only partly committed.
        """
        deadline = time.monotonic() + self._delivery_witness_seconds
        lines = block.split("\n")
        witnessed: list[dict[str, Any]] = []
        is_deadline_passed = False
        while not is_deadline_passed:
            # Rescan here rather than waiting on the transcript thread's cadence: the verdict
            # must not depend on another thread having ticked, and a row agy has already
            # written should be seen the moment we look.
            with self._lock:
                witnessed.extend(self._collect_new_events())
                self._publish_turn_state()
            for text in self._turn_state.user_turn_texts()[len(before) :]:
                cleaned = text.strip()
                if cleaned == block.strip():
                    return claimed, witnessed
                covered = _covered_prefix(lines, cleaned)
                if covered:
                    return claimed[:covered], witnessed
            is_deadline_passed = time.monotonic() >= deadline
            if not is_deadline_passed:
                # Waiting on the stop event rather than sleeping: teardown interrupts the
                # witness window immediately instead of after the full deadline, and the
                # worker cannot sit blind while its watcher is being shut down.
                if self._stopping.wait(_DELIVERY_POLL_SECONDS):
                    return (), witnessed
        return (), witnessed

    def _return_queue_to_composer(self) -> None:
        """A dead agent's queue goes back to the user, not into the bin."""
        _block, taken = self._queue.take_unclaimed()
        if taken:
            logger.info("antigravity: returned {} queued message(s) -- {} is not alive", len(taken), self._agent_id)


def _covered_prefix(lines: list[str], committed: str) -> int:
    """How many leading lines of the block ``committed`` accounts for (0 = none).

    Whole-block equality is handled by the caller; this is only the partial arm.
    """
    if not committed:
        return 0
    for count in range(len(lines) - 1, 0, -1):
        if committed == "\n".join(lines[:count]).strip():
            return count
    return 0
