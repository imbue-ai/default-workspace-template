"""agy's queued-message store: the queue we hold on agy's behalf, and the only one that exists.

Unlike every other harness, this is NOT a mirror of a queue the harness keeps. agy parks
mid-turn input inside its TUI where it is invisible on disk, and merges all parked messages
into one turn -- so a mirror would have to reconstruct N messages from one committed turn by
matching text, which a byte-identical message typed into agy's own terminal can fool.

Instead agy is never allowed to park anything: EVERY message is held here, and one worker --
the only typist -- delivers the block once agy is idle. See
``docs/design/antigravity-swallow-fix-plan.md``.

LIFETIME (contract Part B). The queue must survive a ``system_interface`` restart -- the
session is still alive, and "never silently dropped while the session lives" applies -- but
must NOT survive the agy session. Entries are journalled and stamped with the session's
identity; a new session clears them rather than replaying, because the contract forbids a
queue being revived or auto-sent on resume.

OWNERSHIP. Exactly one tracker per agent, for the agent's life, keyed by agent id alone. It is
the only object the watcher, the session and the tap can all reach, so it also owns the
snapshot publisher and the flush wake -- a mutation and the publish that announces it happen
under one lock, so a stale snapshot can never blank a live chip.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger

from imbue.system_interface.harnesses.queued_set import QueuedSet

# The journal, beside the other agy files in the agent state dir.
OUTBOX_FILENAME: Final[str] = "agy_outbox.jsonl"

# Trailing lines replayed on load. A queue deeper than this is not a real workflow, and
# the cap bounds the read of a file only ever appended to within one session.
_MAX_REPLAY_LINES: Final[int] = 100

# Deliveries attempted for one entry before we stop retyping it. Each retry is a FRESH paste
# of the whole block, and a delivery we cannot verify is a duplication generator, so this is
# the bound that turns "retry forever" into a terminal, visible failure.
MAX_DELIVERY_ATTEMPTS: Final[int] = 3

PublishCallback = Callable[[list[dict[str, Any]]], None]


def session_token(state_dir: Path) -> str:
    """An identity for the CURRENT agy session: the process-start marker's mtime.

    mngr stamps ``antigravity_process_started`` on every launch and resume, so its mtime
    changes exactly when a new session begins -- which is precisely when the contract says a
    queue must be discarded rather than replayed.

    An absent marker yields "" and means NO SESSION, not "a session whose token is empty".
    The distinction is load-bearing: "" once compared equal to "" on replay, so a queue
    journalled while the marker was missing survived every later restart and was auto-sent
    into a fresh session -- the exact revival Part B forbids. A falsy token now refuses to
    journal and refuses to replay.
    """
    try:
        return str((state_dir / "antigravity_process_started").stat().st_mtime_ns)
    except OSError:
        return ""


class AntigravityQueueTracker:
    """The messages we are holding for one agy agent: their journal, claims and publisher.

    Every mutation rewrites the journal, so the file is always the current queue rather than a
    log to be replayed forward. Journal failures are logged and swallowed: losing durability
    degrades to an in-memory queue, which is worse than the contract wants but far better than
    failing the user's send.
    """

    _queued: QueuedSet
    _outbox_path: Path
    _session_token: str
    # Reentrant because a mutation publishes while still holding it, and a publish callback
    # can re-enter through the manager's own bookkeeping.
    _lock: threading.RLock
    # Ids currently being flushed. They stay in the snapshot but render as "Sending...", so
    # they are never blanked mid-flush (contract A1a / E1).
    _sending_ids: set[str]
    # Bumped whenever the claimed work becomes void: a stop took the entries, or the session
    # changed. A ``finish_flush`` carrying a stale generation is a no-op, which is what makes
    # a detached worker (one that outlived its join) harmless instead of corrupting.
    _generation: int
    # Accepted with no turn to wait behind, so the worker takes them immediately. They are
    # *Sending*, not *Queued*: nothing is parked, and rendering them as a queued chip reports
    # a message as waiting when it is already on its way. Demoted the moment that stops being
    # true -- a turn opened first, or an attempt failed.
    _pending_send: set[str]
    _attempts: dict[str, int]
    _publish: PublishCallback | None
    _wake: Callable[[], None] | None

    @classmethod
    def build(cls, outbox_path: Path, session_token: str) -> "AntigravityQueueTracker":
        self = cls.__new__(cls)
        self._queued = QueuedSet.build()
        self._outbox_path = outbox_path
        self._session_token = session_token
        self._lock = threading.RLock()
        self._sending_ids = set()
        self._generation = 0
        self._pending_send = set()
        self._attempts = {}
        self._publish = None
        self._wake = None
        self._replay()
        return self

    # --- wiring ----------------------------------------------------------------------

    def attach(self, *, publish: PublishCallback, wake: Callable[[], None]) -> None:
        """Bind the watcher's snapshot publisher and flush wake. Idempotent."""
        with self._lock:
            self._publish = publish
            self._wake = wake

    def detach(self) -> None:
        """Unbind on watcher teardown, so a mutation cannot publish into a dead callback."""
        with self._lock:
            self._publish = None
            self._wake = None

    # --- journal ---------------------------------------------------------------------

    def _replay(self) -> None:
        """Restore entries written by THIS session; discard a dead session's.

        A torn final line (killed mid-write) is skipped, not fatal. A falsy token means no
        session, so nothing is replayed -- see :func:`session_token`.
        """
        if not self._session_token:
            return
        try:
            lines = self._outbox_path.read_text().splitlines()
        except OSError:
            return
        for line in lines[-_MAX_REPLAY_LINES:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                # A torn last line is expected after a crash mid-append; anything else is
                # corruption worth seeing. Either way the remaining lines still replay.
                logger.opt(exception=error).warning(
                    "antigravity: skipping unparsable outbox line in {}", self._outbox_path
                )
                continue
            if not isinstance(entry, dict) or entry.get("session") != self._session_token:
                continue
            queued_id = entry.get("queued_id")
            content = entry.get("content")
            timestamp = entry.get("timestamp")
            if isinstance(queued_id, str) and isinstance(content, str) and isinstance(timestamp, str):
                self._queued.add(queued_id, content, timestamp, False)

    def _write_journal(self) -> None:
        """Rewrite the journal from the live queue. Caller holds ``self._lock``."""
        if not self._session_token:
            # No session means nothing may be revived later, so nothing is written.
            self._outbox_path.unlink(missing_ok=True)
            return
        entries = self._queued.snapshot()
        if not entries:
            # Nothing to restore, so leave no file to restore it from.
            self._outbox_path.unlink(missing_ok=True)
            return
        payload = "".join(json.dumps({"session": self._session_token, **entry}) + "\n" for entry in entries)
        try:
            tmp_path = self._outbox_path.with_suffix(self._outbox_path.suffix + ".tmp")
            tmp_path.write_text(payload)
            tmp_path.replace(self._outbox_path)
        except OSError as error:
            logger.opt(exception=error).warning("antigravity: could not journal the queue to {}", self._outbox_path)

    def _settled(self) -> None:
        """Journal and publish, under the lock, so the announcement cannot lag the change."""
        self._write_journal()
        if self._publish is not None:
            self._publish(self._snapshot_locked())

    def _snapshot_locked(self) -> list[dict[str, Any]]:
        return [
            {
                **entry,
                "is_sending": entry["queued_id"] in self._sending_ids or entry["queued_id"] in self._pending_send,
            }
            for entry in self._queued.snapshot()
        ]

    # --- session identity ------------------------------------------------------------

    def set_session(self, token: str) -> bool:
        """Adopt ``token``; on a CHANGE, discard everything. Returns True if it changed.

        Discard rather than carry over: keeping the entries and only swapping the token would
        deliver a dead session's queue into a fresh agy, which Part B forbids in as many words
        ("NEVER revived, NEVER auto-sent on resume"). Bumping the generation additionally
        voids any flush the old session had claimed.
        """
        with self._lock:
            if token == self._session_token:
                return False
            dropped = [str(entry["queued_id"]) for entry in self._queued.snapshot()]
            if dropped:
                logger.info("antigravity: discarding {} queued message(s) -- the agy session changed", len(dropped))
            self._queued.clear()
            self._sending_ids.clear()
            self._pending_send.clear()
            self._attempts.clear()
            self._generation += 1
            self._session_token = token
            self._outbox_path.unlink(missing_ok=True)
            self._settled()
            return True

    # --- mutations -------------------------------------------------------------------

    def enqueue(self, content: str, timestamp: str, *, is_turn_open: bool = True) -> str:
        """Hold ``content`` for the next flush; returns its queued id.

        Publishes and wakes the worker inside the lock: the entry is on screen before the POST
        returns (contract A2's handoff), and the only typist starts immediately.

        ``is_turn_open`` decides how it is PRESENTED, not how it is stored. False means there
        is nothing to wait behind, so the worker will type it immediately and the honest state
        is Sending -- the contract's "submitted, in flight, not yet confirmed". True means it
        really is parked behind a live turn, which is Queued.
        """
        queued_id = f"agy-{uuid4().hex}"
        with self._lock:
            self._queued.add(queued_id, content, timestamp, False)
            if not is_turn_open:
                self._pending_send.add(queued_id)
            self._settled()
            wake = self._wake
        if wake is not None:
            wake()
        return queued_id

    def begin_flush(self) -> tuple[str, tuple[str, ...], int]:
        """Claim the current queue for a flush: returns (block, claimed ids, generation).

        The entries are NOT removed -- they stay visible, marked sending, until the flush
        resolves. Removing them here and re-showing the turn later is exactly the blink
        contract E1 describes.

        Returns an empty claim when there is nothing to flush or a claim is already open.
        That single check is the mutual exclusion between EVERY typist -- the worker, the tap
        and stop all claim through here -- so no two of them can be acting on the same block.
        """
        with self._lock:
            if self._sending_ids:
                return "", (), self._generation
            entries = [
                entry
                for entry in self._queued.snapshot()
                if self._attempts.get(str(entry["queued_id"]), 0) < MAX_DELIVERY_ATTEMPTS
            ]
            if not entries:
                # Everything left has exhausted its attempts. It stays visible as a failed
                # entry rather than being retyped: a delivery we cannot witness, retried
                # without bound, is a duplication generator.
                return "", (), self._generation
            claimed = tuple(str(entry["queued_id"]) for entry in entries)
            self._sending_ids = set(claimed)
            self._settled()
            block = "\n".join(str(entry["content"]) for entry in entries)
            return block, claimed, self._generation

    def release_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        """Un-claim without settling and without counting an attempt.

        The tap uses this: it claimed only to grey the button while it cancelled, and never
        tried to deliver, so charging those entries a delivery attempt would burn their
        budget for work that was never attempted.
        """
        with self._lock:
            if generation != self._generation:
                return
            for queued_id in claimed:
                self._sending_ids.discard(queued_id)
            self._settled()

    def finish_flush(self, claimed: tuple[str, ...], generation: int, *, delivered: tuple[str, ...] | None) -> None:
        """Settle a claimed flush.

        ``delivered`` lists the ids agy actually committed (all of ``claimed`` on a clean
        delivery, a prefix on a partial one, empty when nothing landed). Undelivered entries
        return to the queue with their attempt count incremented; at
        :data:`MAX_DELIVERY_ATTEMPTS` they stop being retried and stay visible as failed,
        because an unverifiable delivery retried forever duplicates the user's message.

        A stale ``generation`` means a stop or a session change already took these entries;
        the settle is dropped so a detached worker cannot resurrect or double-resolve them.
        """
        with self._lock:
            if generation != self._generation:
                logger.info("antigravity: dropping a stale flush settle for {} message(s)", len(claimed))
                return
            settled = set(delivered or ())
            for queued_id in claimed:
                self._sending_ids.discard(queued_id)
                # Either way it stops being "about to be typed": delivered entries leave, and
                # an entry whose attempt failed is genuinely waiting now, so it reads Queued.
                self._pending_send.discard(queued_id)
                if queued_id in settled:
                    self._queued.resolve(queued_id)
                    self._attempts.pop(queued_id, None)
                else:
                    self._attempts[queued_id] = self._attempts.get(queued_id, 0) + 1
            self._settled()

    def take_unclaimed(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return every UNCLAIMED entry as one block, for stop.

        Claimed entries are left alone: a flush is mid-send with them, and returning them to
        the composer while that send may still land is how one message becomes both Delivered
        and Returned. The generation is bumped so that flush's settle is discarded.
        """
        with self._lock:
            entries = [e for e in self._queued.snapshot() if str(e["queued_id"]) not in self._sending_ids]
            taken = tuple(str(entry["queued_id"]) for entry in entries)
            block = "\n".join(str(entry["content"]) for entry in entries)
            for queued_id in taken:
                self._queued.resolve(queued_id)
                self._pending_send.discard(queued_id)
                self._attempts.pop(queued_id, None)
            # The generation is deliberately NOT bumped, and claims are deliberately NOT
            # cleared. This call leaves an in-flight flush alone, so voiding it would be
            # exactly wrong: its settle would be refused as stale, its entries would return to
            # the queue, and a block agy had already committed would be delivered a second
            # time. Only work this call SUPERSEDES may be voided -- see take_all.
            self._settled()
            return block, taken

    def take_all(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return EVERY entry, claimed included -- the restart path.

        A restart SIGKILLs agy, so an in-flight send dies with it: its entries never committed
        and never will, which makes returning them correct rather than merely safe. This is
        the one case where taking a claimed entry does not risk a double, and taking it is
        mandatory -- the shared restart drain clears the queue on its way through, so anything
        left behind here is destroyed with no accounting.
        """
        with self._lock:
            entries = self._queued.snapshot()
            taken = tuple(str(entry["queued_id"]) for entry in entries)
            block = "\n".join(str(entry["content"]) for entry in entries)
            self._queued.clear()
            self._sending_ids.clear()
            self._pending_send.clear()
            self._attempts.clear()
            self._generation += 1
            self._settled()
            return block, taken

    def demote_pending(self) -> bool:
        """Stop presenting the not-yet-claimed entries as Sending; they are parked after all.

        Called when a turn turns out to be open, so the worker declined to take them. Returns
        True when anything changed, so the caller can skip a pointless publish.
        """
        with self._lock:
            pending = self._pending_send - self._sending_ids
            if not pending:
                return False
            self._pending_send -= pending
            self._settled()
            return True

    def clear(self) -> None:
        """Drop everything. Only for a session that no longer exists."""
        with self._lock:
            self._queued.clear()
            self._sending_ids.clear()
            self._pending_send.clear()
            self._attempts.clear()
            self._generation += 1
            self._settled()

    def republish(self) -> None:
        """Re-announce the current queue unchanged (level-triggered visibility).

        The worker calls this every tick. Without it, an untrack/re-track cycle drops the
        manager's cached queue and nothing republishes until the next mutation, so live
        entries have no chips until something else happens -- A1a's forbidden "resurfaces
        later". The manager de-dupes, so repeating an unchanged snapshot is free.
        """
        with self._lock:
            self._settled()

    # --- reads -----------------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot_locked()

    def concatenated_block(self) -> str:
        with self._lock:
            return self._queued.concatenated_block()

    def has_entries(self) -> bool:
        with self._lock:
            return bool(self._queued.snapshot())

    def is_sending(self) -> bool:
        """Whether a flush has entries claimed and in flight (contract Shoulder-tap).

        The tap is offered only when nothing is Sending: tapping mid-flush would press ctrl+c
        through the very turn the flush is committing our block into.

        Entries merely PENDING a send count too. They are about to be typed, so the tap has
        nothing useful to do -- and offering the button in that window made it flash on every
        ordinary send while the worker got to the message.
        """
        with self._lock:
            return bool(self._sending_ids or self._pending_send)

    def is_exhausted(self, queued_id: str) -> bool:
        with self._lock:
            return self._attempts.get(queued_id, 0) >= MAX_DELIVERY_ATTEMPTS

    def deliverable_block(self) -> str:
        """The block to send: entries that have not exhausted their attempts."""
        with self._lock:
            return "\n".join(
                str(entry["content"])
                for entry in self._queued.snapshot()
                if self._attempts.get(str(entry["queued_id"]), 0) < MAX_DELIVERY_ATTEMPTS
            )


# The one tracker per agent, for the agent's life.
#
# agy's queue is reached by three components that cannot reach each other: the WATCHER (built
# by the app's composition root, owns the worker thread and the publisher), the SESSION (built
# lazily by the agent manager, accepts the user's sends) and the TAP/STOP executors (built per
# request from the harness registry). Rather than thread a new capability through SessionDeps,
# the composition root and the manager, this keyed registry hands all three the same instance.
#
# Keyed by AGENT ID ALONE, deliberately. Keying by (id, session token) meant a token change
# silently REPLACED the entry, so the watcher kept using the object it bound at build time
# while the session enqueued into a new one -- messages that were never delivered, never
# returned and not recoverable. Session changes are now handled IN PLACE by ``set_session``,
# and only the watcher calls it, so a read can never mutate the queue as a side effect.
_TRACKERS: dict[str, AntigravityQueueTracker] = {}
_TRACKERS_LOCK: Final[threading.Lock] = threading.Lock()


def get_tracker(agent_id: str, outbox_path: Path, session_token: str) -> AntigravityQueueTracker:
    """The agent's tracker, built once and kept for the agent's life.

    ``session_token`` seeds a tracker that does not exist yet; it never re-keys or rebuilds an
    existing one. Adopting a NEW session is :meth:`AntigravityQueueTracker.set_session`, which
    only the watcher calls.
    """
    with _TRACKERS_LOCK:
        existing = _TRACKERS.get(agent_id)
        if existing is not None:
            return existing
        tracker = AntigravityQueueTracker.build(outbox_path, session_token)
        _TRACKERS[agent_id] = tracker
        return tracker


def drop_tracker(agent_id: str) -> None:
    """Forget an agent's tracker entirely (the agent is gone, not merely restarted)."""
    with _TRACKERS_LOCK:
        _TRACKERS.pop(agent_id, None)
