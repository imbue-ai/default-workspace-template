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

from imbue.system_interface.harnesses.model import QueueBehavior
from imbue.system_interface.harnesses.queued_set import QueuedSet

# Mirrors pi's phantom rule: a background task-notification or blank send holds a FIFO
# slot (so front-run joins stay aligned with what agy actually coalesces) but never
# surfaces as a bubble.
_TASK_NOTIFICATION_CONTENT_PREFIX = "<task-notification>"


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
    """Populates one agent's :class:`QueuedSet` from UI sends + drained user turns."""

    _queued_set: QueuedSet
    _enqueue_count: int
    _queue_behavior: QueueBehavior

    @classmethod
    def build(cls, queue_behavior: QueueBehavior = QueueBehavior.COALESCES) -> "AntigravityQueueTracker":
        tracker = cls.__new__(cls)
        tracker._queue_behavior = queue_behavior
        tracker._enqueue_count = 0
        tracker.reset()
        return tracker

    def enqueue(self, content: str, timestamp: str) -> None:
        """Add one UI-sent message to the FIFO tail (phantom for a task-notification / blank)."""
        self._queued_set.add(
            _queued_id(self._enqueue_count, content),
            content,
            timestamp,
            _is_phantom_content(content),
        )
        self._enqueue_count += 1

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
                return

    def on_idle(self) -> None:
        """Clear the queue -- the working->IDLE backstop (a genuine IDLE means it drained)."""
        self._queued_set.clear()

    def clear(self) -> None:
        """Drop everything (a flush restart handed the block back)."""
        self._queued_set.clear()

    def reset(self) -> None:
        """Reset to an empty set (a re-attach or truncation)."""
        self._queued_set = QueuedSet.build()

    def snapshot(self) -> list[dict[str, str]]:
        """The full wire snapshot of currently-queued messages, in enqueue order."""
        return self._queued_set.snapshot()

    def concatenated_block(self) -> str:
        """The queue as one newline-joined turn (shared by both common actions)."""
        return self._queued_set.concatenated_block()
