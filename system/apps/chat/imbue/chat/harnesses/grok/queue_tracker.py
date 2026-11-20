"""The messages the chat holds for a grok agent until grok can take them.

grok parks a message typed during a turn in its own queue, which leaves nothing on disk until the message runs, so
the chat holds every message itself: it waits here, and the watcher's flush worker, the only typist, types the held
block once mngr reports no turn in flight. Entries are journalled under the current grok process's session token, so
they survive a chat app restart and are dropped when grok relaunches (contract Part B).
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger

from imbue.chat.harnesses.queued_set import QueuedSet
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME

OUTBOX_FILENAME: Final[str] = "grok_outbox.jsonl"

# Sends of one entry that end with no sign grok took it before the worker stops typing it.
MAX_DELIVERY_ATTEMPTS: Final[int] = 3

_QUEUED_ID_PREFIX: Final[str] = "grok-"

PublishCallback = Callable[[list[dict[str, Any]]], None]


def session_token(agent_state_dir: Path) -> str:
    """The current grok process's identity, the launch marker's mtime; '' when grok never launched."""
    marker_path = agent_state_dir / PROCESS_STARTED_MARKER_FILENAME
    try:
        return str(marker_path.stat().st_mtime_ns)
    except FileNotFoundError:
        logger.trace("grok: no launch marker at {}", marker_path)
        return ""
    except OSError as error:
        logger.opt(exception=error).warning("grok: could not read the launch marker {}", marker_path)
        return ""


class GrokQueueTracker:
    """One grok agent's held messages: their journal, claims, delivery attempts and publisher.

    A claim keeps its entries in the snapshot, shown as Sending, until it settles. One claim is open at a time, which
    keeps the flush worker, the tap and stop apart.
    """

    _queued: QueuedSet
    _outbox_path: Path
    _session_token: str
    # Reentrant: a mutation publishes under it, and the publisher can re-enter through the manager.
    _lock: threading.RLock
    # Ids a flush or a tap has claimed.
    _sending_ids: set[str]
    # The claimed ids a tap holds only to grey its button; stop still takes them.
    _tap_claimed_ids: set[str]
    # Bumped when claimed work becomes void: grok relaunched, or its session is gone.
    _generation: int
    # How many stops are running; while any is, nothing may be claimed for a flush.
    _flush_pause_count: int
    # Accepted with no turn in the way, so the worker is about to type them; shown as Sending.
    _pending_send: set[str]
    _attempts: dict[str, int]
    # When the first flush of each entry still held began, in epoch seconds.
    _first_flushed_at: dict[str, float]
    _publish: PublishCallback | None
    _wake: Callable[[], None] | None
    # The answers of ``is_sending`` and ``has_deliverable_entries``, read without the lock, since another tracker's
    # publish reads them while it holds its own.
    _is_sending_now: bool
    _has_deliverable_now: bool

    @classmethod
    def build(cls, outbox_path: Path, session_token: str) -> "GrokQueueTracker":
        self = cls.__new__(cls)
        self._queued = QueuedSet.build()
        self._outbox_path = outbox_path
        self._session_token = session_token
        self._lock = threading.RLock()
        self._sending_ids = set()
        self._tap_claimed_ids = set()
        self._generation = 0
        self._flush_pause_count = 0
        self._pending_send = set()
        self._attempts = {}
        self._first_flushed_at = {}
        self._publish = None
        self._wake = None
        self._replay()
        self._refresh_lock_free_reads()
        return self

    def attach(self, *, publish: PublishCallback, wake: Callable[[], None]) -> None:
        """Bind the watcher's snapshot publisher and the flush worker's wake."""
        with self._lock:
            self._publish = publish
            self._wake = wake

    def detach(self, publish: PublishCallback) -> None:
        """Unbind the watcher that attached ``publish``; one that attached since keeps its hooks."""
        with self._lock:
            if self._publish == publish:
                self._publish = None
                self._wake = None

    def _wake_worker(self) -> None:
        with self._lock:
            wake = self._wake
        if wake is not None:
            wake()

    def _replay(self) -> None:
        """Restore the entries journalled under the current session; a torn or foreign line is skipped."""
        if not self._session_token:
            return
        try:
            lines = self._outbox_path.read_text().splitlines()
        except FileNotFoundError:
            return
        except OSError as error:
            logger.opt(exception=error).warning("grok: could not read the queue journal {}", self._outbox_path)
            return
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                logger.opt(exception=error).warning(
                    "grok: skipping an unparsable outbox line in {}", self._outbox_path
                )
                continue
            if not isinstance(entry, dict) or entry.get("session") != self._session_token:
                continue
            queued_id = entry.get("queued_id")
            content = entry.get("content")
            timestamp = entry.get("timestamp")
            if isinstance(queued_id, str) and isinstance(content, str) and isinstance(timestamp, str):
                self._queued.add(queued_id, content, timestamp, False)

    def _write_journal_locked(self) -> None:
        entries = self._queued.snapshot()
        if not self._session_token or not entries:
            self._outbox_path.unlink(missing_ok=True)
            return
        payload = "".join(json.dumps({"session": self._session_token, **entry}) + "\n" for entry in entries)
        tmp_path = self._outbox_path.with_suffix(self._outbox_path.suffix + ".tmp")
        try:
            tmp_path.write_text(payload)
            tmp_path.replace(self._outbox_path)
        except OSError as error:
            logger.opt(exception=error).warning("grok: could not journal the queue to {}", self._outbox_path)

    def _refresh_lock_free_reads(self) -> None:
        self._is_sending_now = bool(self._sending_ids or self._pending_send)
        self._has_deliverable_now = any(
            self._attempts.get(entry["queued_id"], 0) < MAX_DELIVERY_ATTEMPTS for entry in self._queued.snapshot()
        )

    def _settled_locked(self) -> None:
        """Journal and publish the queue under the lock, so the announcement cannot lag the change."""
        self._refresh_lock_free_reads()
        self._write_journal_locked()
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

    def _clear_entries_locked(self) -> None:
        self._queued.clear()
        self._sending_ids.clear()
        self._tap_claimed_ids.clear()
        self._pending_send.clear()
        self._attempts.clear()
        self._first_flushed_at.clear()
        self._generation += 1

    def _drop_ids_locked(self, queued_ids: tuple[str, ...]) -> None:
        for queued_id in queued_ids:
            self._queued.resolve(queued_id)
            self._sending_ids.discard(queued_id)
            self._tap_claimed_ids.discard(queued_id)
            self._pending_send.discard(queued_id)
            self._attempts.pop(queued_id, None)
            self._first_flushed_at.pop(queued_id, None)

    @staticmethod
    def _block_of(entries: list[dict[str, str]]) -> str:
        return "\n".join(str(entry["content"]) for entry in entries)

    def set_session(self, token: str) -> bool:
        """Adopt ``token``; on a change, drop every held message, which is never typed into a new grok.

        Returns whether the token changed.
        """
        with self._lock:
            if token == self._session_token:
                return False
            dropped_count = len(self._queued.snapshot())
            if dropped_count:
                logger.info("grok: discarding {} held message(s): grok relaunched", dropped_count)
            self._clear_entries_locked()
            self._session_token = token
            self._settled_locked()
            return True

    def enqueue(self, content: str, timestamp: str, *, is_turn_open: bool) -> str:
        """Hold ``content``, publish it and wake the worker; returns its queued id.

        ``is_turn_open`` False means the worker is about to type it, so it shows as Sending rather than Queued.
        """
        queued_id = f"{_QUEUED_ID_PREFIX}{uuid4().hex}"
        with self._lock:
            self._queued.add(queued_id, content, timestamp, False)
            if not is_turn_open:
                self._pending_send.add(queued_id)
            self._settled_locked()
        self._wake_worker()
        return queued_id

    def begin_flush(self) -> tuple[str, tuple[str, ...], int]:
        """Claim every entry with attempts left: (block, claimed ids, generation).

        Empty while another claim is open or a stop runs.
        """
        with self._lock:
            if self._sending_ids or self._flush_pause_count:
                return "", (), self._generation
            entries = [
                entry
                for entry in self._queued.snapshot()
                if self._attempts.get(entry["queued_id"], 0) < MAX_DELIVERY_ATTEMPTS
            ]
            if not entries:
                return "", (), self._generation
            claimed = tuple(entry["queued_id"] for entry in entries)
            self._sending_ids = set(claimed)
            self._settled_locked()
            return self._block_of(entries), claimed, self._generation

    def begin_tap_claim(self) -> tuple[str, tuple[str, ...], int]:
        """Claim as ``begin_flush`` does, only to grey the tap's button."""
        with self._lock:
            block, claimed, generation = self.begin_flush()
            self._tap_claimed_ids.update(claimed)
            return block, claimed, generation

    def pause_flushes(self) -> None:
        """Refuse every flush claim until the matching resume: a stop is returning what is held."""
        with self._lock:
            self._flush_pause_count += 1

    def resume_flushes(self) -> None:
        with self._lock:
            self._flush_pause_count -= 1
        self._wake_worker()

    def release_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        """Un-claim without charging an attempt: a claim that never typed."""
        with self._lock:
            if generation != self._generation:
                return
            self._sending_ids.difference_update(claimed)
            self._tap_claimed_ids.difference_update(claimed)
            self._settled_locked()

    def note_flush_began(self, claimed: tuple[str, ...], began_at: float) -> float | None:
        """Record a flush of ``claimed`` beginning at epoch ``began_at``; returns when an earlier flush of any of them
        began, or None."""
        with self._lock:
            earlier = [
                self._first_flushed_at[queued_id] for queued_id in claimed if queued_id in self._first_flushed_at
            ]
            for queued_id in claimed:
                self._first_flushed_at.setdefault(queued_id, began_at)
            return min(earlier, default=None)

    def finish_flush(self, claimed: tuple[str, ...], generation: int, *, delivered: tuple[str, ...]) -> None:
        """Settle a claimed flush: ``delivered`` leave, and the rest wait on as Queued with one more attempt spent.

        A stale ``generation`` (grok relaunched, or its session is gone) settles nothing.
        """
        with self._lock:
            if generation != self._generation:
                logger.info("grok: dropping a stale flush settle for {} message(s)", len(claimed))
                return
            delivered_ids = set(delivered)
            for queued_id in claimed:
                self._sending_ids.discard(queued_id)
                self._pending_send.discard(queued_id)
                if queued_id in delivered_ids:
                    self._queued.resolve(queued_id)
                    self._attempts.pop(queued_id, None)
                    self._first_flushed_at.pop(queued_id, None)
                else:
                    self._attempts[queued_id] = self._attempts.get(queued_id, 0) + 1
            self._settled_locked()

    def take_unclaimed(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return every entry no flush has claimed as one block, for stop; a tap's claim keeps none."""
        with self._lock:
            flush_claimed_ids = self._sending_ids - self._tap_claimed_ids
            entries = [entry for entry in self._queued.snapshot() if entry["queued_id"] not in flush_claimed_ids]
            taken = tuple(entry["queued_id"] for entry in entries)
            self._drop_ids_locked(taken)
            self._settled_locked()
            return self._block_of(entries), taken

    def demote_pending(self) -> bool:
        """Show the unclaimed Sending entries as Queued: something is in the way. True if any changed."""
        with self._lock:
            pending = self._pending_send - self._sending_ids
            if not pending:
                return False
            self._pending_send -= pending
            self._settled_locked()
            return True

    def clear(self) -> None:
        """Drop every held message. Only for a grok session that no longer exists."""
        with self._lock:
            self._clear_entries_locked()
            self._settled_locked()

    def republish(self) -> None:
        """Announce the queue again unchanged, so a re-tracked chat gets its chips back."""
        with self._lock:
            if self._publish is not None:
                self._publish(self._snapshot_locked())

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot_locked()

    def concatenated_block(self) -> str:
        with self._lock:
            return self._queued.concatenated_block()

    def has_entries(self) -> bool:
        with self._lock:
            return bool(self._queued.snapshot())

    def has_deliverable_entries(self) -> bool:
        """Whether a held message still has delivery attempts left, so a flush or a tap could claim it."""
        return self._has_deliverable_now

    def is_sending(self) -> bool:
        """Whether an entry is claimed or about to be typed, which withholds the shoulder tap."""
        return self._is_sending_now


# One tracker per agent for the agent's life, keyed by agent id alone; a grok relaunch is adopted in place by
# ``set_session``.
_TRACKERS: dict[str, GrokQueueTracker] = {}
_TRACKERS_LOCK: Final[threading.Lock] = threading.Lock()


def get_tracker(agent_state_dir: Path) -> GrokQueueTracker:
    """The agent's tracker, built on first use from the journal in its state dir, whose name is the agent id."""
    agent_id = agent_state_dir.name
    with _TRACKERS_LOCK:
        existing = _TRACKERS.get(agent_id)
        if existing is not None:
            return existing
        tracker = GrokQueueTracker.build(agent_state_dir / OUTBOX_FILENAME, session_token(agent_state_dir))
        _TRACKERS[agent_id] = tracker
        return tracker
