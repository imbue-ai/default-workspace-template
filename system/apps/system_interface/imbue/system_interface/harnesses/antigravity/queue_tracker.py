"""The antigravity (agy) queued-message populator -- the ONLY agy-specific queue code.

Wraps one common :class:`QueuedSet` and maps agy's queue signals onto its mutators,
exactly as :mod:`harnesses.pi_coding.queue_tracker` does for pi. Everything downstream
(the WS snapshot field and the two common actions) is harness-agnostic.

agy has no on-disk enqueue ledger (no ``pi_inbox`` analogue, and a message queued while
agy is busy does NOT appear in its transcript until it drains). So the enqueue source is
our own send: when the UI sends a message, that IS the parked message -- agy accepted it
but will not act until the current turn ends. The tracker is a **send-sourced outbox**:
the list of messages we sent that have not yet drained into the transcript.

* **enqueue** = the UI send (the watcher's ``note_sent_message`` bridges it here).
* **leave** = a ``user_message`` draining into agy's transcript. agy joins ALL parked
  messages into ONE newline-joined turn at turn-end (``QueueBehavior.COALESCES``), so one
  drained turn commits a whole front-run -- resolved by verbatim matching (see ``leave``),
  never by guessing.
* working -> IDLE -> ``clear`` (the backstop that sweeps interrupts / crashes /
  flush-restart, none of which drain a queued message).
"""

import hashlib
import json
import os
from pathlib import Path

from loguru import logger

from imbue.system_interface.harnesses.model import QueueBehavior
from imbue.system_interface.harnesses.queued_set import QueuedSet

# Mirrors pi's phantom rule: a background task-notification or blank send holds a FIFO
# slot (so front-run joins stay aligned with what agy actually coalesces) but never
# surfaces as a bubble.
_TASK_NOTIFICATION_CONTENT_PREFIX = "<task-notification>"

# Replay reads at most this many trailing ledger lines -- a guard against pathological
# growth only; pruning keeps the file at the live queue's size in normal operation.
_OUTBOX_REPLAY_CAP = 100


def _queued_id(enqueue_index: int, content: str) -> str:
    """A stable synthetic id salted by a monotonic enqueue counter + content.

    Two identical messages sent at different times get distinct ids; the id is stable
    for the rendered bubble. Not a correlation key -- resolution is verbatim front-run
    matching, not id lookup.
    """
    digest = hashlib.sha1(f"{enqueue_index}\0{content}".encode()).hexdigest()
    return digest[:16]


def _is_phantom_content(content: str) -> bool:
    """True for a send that must never surface: a task-notification or blank."""
    return content.startswith(_TASK_NOTIFICATION_CONTENT_PREFIX) or not content.strip()


def _normalize(text: str) -> str:
    """Whitespace-normalize for verbatim matching: unify newlines, strip trailing
    space per line, drop leading/trailing blank space. Content otherwise untouched."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


class AntigravityQueueTracker:
    """Populates one agent's :class:`QueuedSet` from UI sends + drained user turns.

    With an ``outbox_path``, the outbox is also a JSONL ledger on disk -- the
    ``pi_inbox`` analogue mngr never wrote, except we write it ourselves. Each enqueue
    appends one ``{"content", "ts"}`` line; every pop/clear prunes the file (tmp +
    ``os.replace``, so a crash mid-prune leaves the old file or the new one, never
    garbage); ``build`` replays it, so bubbles survive a backend restart exactly as
    pi's do. Replay-then-reconcile is pi's proven shape: the watcher's prime scan
    replays every historical ``user_message`` through :meth:`leave`, popping any
    replayed entry whose turn drained while we were down, and the level-triggered
    idle backstop sweeps stale survivors on an idle agent. No fsync and no
    two-phase protocol on purpose: agy has no ack API, so no file protocol can be
    atomic with "the message entered agy" -- this is a display cache with a
    self-healing reconciler, and the stakes are a bubble.

    All mutators run under the owning watcher's lock, and only one process writes
    (the send endpoint and the watcher share the system_interface process).
    """

    _queued_set: QueuedSet
    _enqueue_count: int
    _queue_behavior: QueueBehavior
    _outbox_path: Path | None

    @classmethod
    def build(
        cls,
        queue_behavior: QueueBehavior = QueueBehavior.COALESCES,
        outbox_path: Path | None = None,
    ) -> "AntigravityQueueTracker":
        tracker = cls.__new__(cls)
        tracker._queue_behavior = queue_behavior
        tracker._enqueue_count = 0
        tracker._outbox_path = outbox_path
        tracker.reset()
        tracker._replay_outbox()
        return tracker

    def enqueue(self, content: str, timestamp: str) -> str:
        """Add one UI-sent message to the FIFO tail (phantom for a task-notification / blank).

        Called BEFORE the send is attempted (write-ahead, pi's ordering): an idle agy can
        commit the turn to its db within one watcher poll, so an enqueue that waited for
        send confirmation could lose the race to its own drain -- ``leave`` would pop
        nothing and the entry would stick forever. Returns the minted queued id so a
        FAILED send can :meth:`retract` exactly this entry.
        """
        queued_id = self._add(content, timestamp)
        self._append_outbox_line(content, timestamp)
        return queued_id

    def retract(self, queued_id: str) -> None:
        """The send this entry recorded failed: remove it (compensation for write-ahead).

        Unknown id is a no-op -- the entry may have already left (a drain or sweep won
        the race), which is fine either way.
        """
        self._queued_set.resolve(queued_id)
        self._prune_outbox()

    def _add(self, content: str, timestamp: str) -> str:
        queued_id = _queued_id(self._enqueue_count, content)
        self._queued_set.add(
            queued_id,
            content,
            timestamp,
            _is_phantom_content(content),
        )
        self._enqueue_count += 1
        return queued_id

    def leave(self, drained_content: str) -> None:
        """A user turn drained into the transcript: pop the front-run it verbatim-matches.

        With ``COALESCES``, one drained turn may commit N parked messages joined by
        newlines. We hold the exact parked contents, so we don't guess how many entries a
        drain covers -- we prove it: pop the largest front-run ``k`` whose newline-joined
        contents equal the drained turn (whitespace-normalized). Longest-first, so a
        coalesced turn of N is preferred over a lone first message that happens to prefix
        it. With ``NORMAL`` this degenerates to the k=1 check.

        A turn that matches no front-run pops nothing: it is a turn we never enqueued (a
        message typed straight into agy's terminal, or a divergence), and the
        working->IDLE backstop sweeps any stragglers.
        """
        pending = self._queued_set.pending
        if not pending:
            return
        drained = _normalize(drained_content)
        max_run = len(pending) if self._queue_behavior is QueueBehavior.COALESCES else 1
        for run_length in range(max_run, 0, -1):
            joined = _normalize("\n".join(entry.content for entry in pending[:run_length]))
            if joined == drained:
                del pending[:run_length]
                self._prune_outbox()
                return

    def on_idle(self) -> None:
        """Clear the queue -- the working->IDLE backstop (a genuine IDLE means it drained)."""
        self._queued_set.clear()
        self._prune_outbox()

    def clear(self) -> None:
        """Drop everything (a flush restart handed the block back)."""
        self._queued_set.clear()
        self._prune_outbox()

    def reset(self) -> None:
        """Reset to an empty set (a re-attach or truncation)."""
        self._queued_set = QueuedSet.build()

    def snapshot(self) -> list[dict[str, str]]:
        """The full wire snapshot of currently-queued messages, in enqueue order."""
        return self._queued_set.snapshot()

    def concatenated_block(self) -> str:
        """The queue as one newline-joined turn (shared by both common actions)."""
        return self._queued_set.concatenated_block()

    # --- the on-disk ledger ------------------------------------------------------------

    def _replay_outbox(self) -> None:
        """Rebuild the outbox from the ledger (a backend restart). Torn tail lines from a
        mid-append crash parse as garbage and are skipped; the cap guards pathological
        growth (the file tracks the live queue, so it is normally a handful of lines)."""
        if self._outbox_path is None or not self._outbox_path.is_file():
            return
        try:
            lines = self._outbox_path.read_text().splitlines()
        except OSError:
            return
        for line in lines[-_OUTBOX_REPLAY_CAP:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                self._add(entry["content"], str(entry.get("ts", "")))

    def _append_outbox_line(self, content: str, timestamp: str) -> None:
        """One append per enqueue; a single small write, no fsync (see class docstring)."""
        if self._outbox_path is None:
            return
        try:
            with self._outbox_path.open("a") as handle:
                handle.write(json.dumps({"content": content, "ts": timestamp}) + "\n")
        except OSError:
            logger.debug("agy outbox: failed to append to {}", self._outbox_path)

    def _prune_outbox(self) -> None:
        """Rewrite the ledger to the still-pending entries, atomically (tmp + replace)."""
        if self._outbox_path is None:
            return
        body = "".join(
            json.dumps({"content": entry.content, "ts": entry.enqueue_ts}) + "\n"
            for entry in self._queued_set.pending
        )
        tmp_path = self._outbox_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(body)
            os.replace(tmp_path, self._outbox_path)
        except OSError:
            logger.debug("agy outbox: failed to prune {}", self._outbox_path)
