"""agy's queued-message store: the queue we hold on agy's behalf.

Unlike every other harness, this is NOT a mirror of a queue the harness keeps. agy
parks mid-turn input inside its TUI where it is invisible on disk, and merges all
parked messages into one turn -- so a mirror would have to reconstruct N messages
from one committed turn by matching text, which a byte-identical message typed into
agy's own terminal can fool.

Instead we never let agy park anything: while agy is busy a message is held here,
and the flush sends the whole block once agy goes idle. That makes this the only
queue that exists for an agy agent (see docs/design/antigravity-message-lifecycle-plan.md).

LIFETIME (contract Part B, and the reason this is not a plain in-memory list): the
queue must survive a ``system_interface`` restart -- the session is still alive, and
"never silently dropped while the session lives" applies -- but must NOT survive the
session. So entries are journalled to the agent state dir and stamped with the
session's identity; a journal from a dead session is discarded on load rather than
replayed. claude and pi get the same property for free by re-deriving from disk.
"""

import json
import threading
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


def session_token(state_dir: Path) -> str:
    """An identity for the CURRENT agy session: the process-start marker's mtime.

    mngr stamps ``antigravity_process_started`` on every launch and resume, so its mtime
    changes exactly when a new session begins -- which is precisely when the contract says a
    queue must be discarded rather than replayed. An absent marker yields "", which matches
    nothing previously journalled and therefore discards, the safe direction.
    """
    try:
        return str((state_dir / "antigravity_process_started").stat().st_mtime_ns)
    except OSError:
        return ""


class AntigravityQueueTracker:
    """The messages we are holding for one agy agent, and their journal.

    Every mutation rewrites the journal, so the file is always the current queue
    rather than a log to be replayed forward. Journal failures are logged and
    swallowed: losing durability degrades to an in-memory queue, which is worse than
    the contract wants but far better than failing the user's send.
    """

    _queued: QueuedSet
    _outbox_path: Path
    _session_token: str
    _lock: threading.Lock
    # Ids currently being flushed. They stay in the snapshot but render as "Sending...",
    # so they are never blanked mid-flush (contract A1a / E1).
    _sending_ids: set[str]

    @classmethod
    def build(cls, outbox_path: Path, session_token: str) -> "AntigravityQueueTracker":
        self = cls.__new__(cls)
        self._queued = QueuedSet.build()
        self._outbox_path = outbox_path
        self._session_token = session_token
        self._lock = threading.Lock()
        self._sending_ids = set()
        self._replay()
        return self

    # --- journal ---------------------------------------------------------------------

    def _replay(self) -> None:
        """Restore entries written by THIS session; discard a dead session's.

        The session token is the identity of the running agy process. An entry stamped
        with a different token belongs to a session that has since restarted, and the
        contract is explicit that such a queue is gone -- never replayed, never
        delivered. A torn final line (killed mid-write) is skipped, not fatal.
        """
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
        payload = "".join(
            json.dumps({"session": self._session_token, **entry}) + "\n" for entry in self._queued.snapshot()
        )
        try:
            tmp_path = self._outbox_path.with_suffix(self._outbox_path.suffix + ".tmp")
            tmp_path.write_text(payload)
            tmp_path.replace(self._outbox_path)
        except OSError as error:
            logger.opt(exception=error).warning("antigravity: could not journal the queue to {}", self._outbox_path)

    # --- mutations -------------------------------------------------------------------

    def enqueue(self, content: str, timestamp: str) -> str:
        """Hold ``content`` for the next flush; returns its queued id."""
        queued_id = f"agy-{uuid4().hex}"
        with self._lock:
            self._queued.add(queued_id, content, timestamp, False)
            self._write_journal()
        return queued_id

    def retract(self, queued_id: str) -> None:
        """Un-hold one entry: the send failed, so it returns to the composer instead."""
        with self._lock:
            self._queued.resolve(queued_id)
            self._sending_ids.discard(queued_id)
            self._write_journal()

    def begin_flush(self) -> tuple[str, tuple[str, ...]]:
        """Claim the current queue for a flush: returns (block, claimed ids).

        The entries are NOT removed -- they stay visible, marked sending, until the
        flush resolves. Removing them here and re-showing the turn later is exactly the
        blink contract E1 describes. Returns an empty block when there is nothing to
        flush or a flush is already claimed, which is what makes the flush idempotent
        against a level-triggered caller.
        """
        with self._lock:
            if self._sending_ids:
                return "", ()
            entries = self._queued.snapshot()
            if not entries:
                return "", ()
            claimed = tuple(str(entry["queued_id"]) for entry in entries)
            self._sending_ids = set(claimed)
            return self._queued.concatenated_block(), claimed

    def finish_flush(self, claimed: tuple[str, ...], *, is_delivered: bool) -> None:
        """Settle a claimed flush: drop the entries when delivered, un-claim when not."""
        with self._lock:
            for queued_id in claimed:
                self._sending_ids.discard(queued_id)
                if is_delivered:
                    self._queued.resolve(queued_id)
            self._write_journal()

    def clear(self) -> None:
        """Drop everything (stop, or a dead agent). Never delivers."""
        with self._lock:
            self._queued.clear()
            self._sending_ids.clear()
            self._write_journal()

    # --- reads -----------------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**entry, "is_sending": entry["queued_id"] in self._sending_ids} for entry in self._queued.snapshot()
            ]

    def concatenated_block(self) -> str:
        with self._lock:
            return self._queued.concatenated_block()

    def has_entries(self) -> bool:
        with self._lock:
            return bool(self._queued.snapshot())

    def is_sending(self) -> bool:
        """Whether a flush has entries claimed and in flight (contract Shoulder-tap).

        The tap is offered only when nothing is Sending: tapping mid-flush would press
        ctrl+c through the very turn the flush is committing our block into, and re-send
        a block the flush already handed over.
        """
        with self._lock:
            return bool(self._sending_ids)


# The one tracker per agent, shared by the two components that need it.
#
# agy's queue is read by the WATCHER (the WS snapshot, stop's return block, the tap's
# availability gate all read it there) and written by the SESSION (which owns the send and
# therefore decides hold-vs-type). Those are built by different owners -- the watcher by the
# app's composition root, the session lazily by the agent manager -- and neither can reach
# the other. Rather than thread a new capability through SessionDeps, the composition root
# and the manager, this keyed registry hands both the same instance. Deliberately scoped to
# this module: no other harness holds a queue on its harness's behalf, so nothing else needs
# it, and if agy ever stops needing it the registry goes with it.
_TRACKERS: dict[str, AntigravityQueueTracker] = {}
_TRACKERS_LOCK: Final[threading.Lock] = threading.Lock()


def get_tracker(agent_id: str, outbox_path: Path, session_token: str) -> AntigravityQueueTracker:
    """The agent's tracker, built once. A token change means a NEW session, so the tracker is
    rebuilt and the previous session's journal discarded rather than carried over."""
    with _TRACKERS_LOCK:
        existing = _TRACKERS.get(agent_id)
        if existing is not None and existing._session_token == session_token:
            return existing
        tracker = AntigravityQueueTracker.build(outbox_path, session_token)
        _TRACKERS[agent_id] = tracker
        return tracker


def drop_tracker(agent_id: str) -> None:
    """Forget an agent's tracker (its watcher stopped)."""
    with _TRACKERS_LOCK:
        _TRACKERS.pop(agent_id, None)
