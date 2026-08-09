"""The Claude queued-message populator -- the ONLY harness-specific queue code.

Wraps one common :class:`QueuedSet` and maps Claude's raw queue ledger onto its
``add`` / ``resolve_oldest`` / ``clear`` mutators. It is a pure function of the
ledger it is fed (plus the coarse ``on_idle`` backstop); it holds no UI state and
knows nothing about the frontend or the two common actions -- those all read the
shared entity.

The model is the conservation law ``enqueue = dequeue + remove + popAll`` (see
``docs/claude_queued_messages_impl.md``):

* ``enqueue`` -> ``add`` a FIFO entry. If the content starts with
  ``<task-notification>`` or is blank it is added as a PHANTOM (occupies a slot to
  keep positions aligned, but never surfaces); otherwise it is a REAL entry.
* dequeue / remove / popAll -> ``resolve_oldest`` (pop the FIFO head, phantom or
  real). One record = one pop; popAll emits one record per flushed message, so a
  per-record pop is uniform (no special "clear all").
* working -> IDLE -> ``clear`` (the one backstop; sweeps interrupts, SIGKILL,
  crashes -- none of which the poll loop would otherwise reconcile).

This keys resolution off the ledger's LEAVE ops ONLY -- never ``promptSource`` or
the ``queued_command`` attachment -- because in the real Minds flow every message
is delivered via mngr (typed into the TUI) and commits as a ``dequeue`` whose
``promptSource`` is "typed", which those markers do not catch. Resolution is
POSITIONAL (drop the FIFO head): the ledger carries no correlation id. There is NO
fuzzy text matching -- the only content use is the ``<task-notification>`` prefix /
blank check that decides phantom-ness, and salting the ``queued_id`` hash.

Feeding the whole ledger from the start is self-correcting: every enqueue nets
against its one leave, leaving exactly the still-parked entries, so no durable
cursor is needed. The watcher scopes the feed to the LATEST main session's
ledger only (a new session file means the process restarted and its in-memory
queue is gone) and calls ``reset`` when a new latest session is registered, so
``consume`` never sees a dead session's signals.
"""

import hashlib

from imbue.system_interface.harnesses.claude.session_parser import QueueSignal
from imbue.system_interface.harnesses.claude.session_parser import QueueSignalKind
from imbue.system_interface.harnesses.claude.session_parser import TASK_NOTIFICATION_CONTENT_PREFIX
from imbue.system_interface.harnesses.queued_set import QueuedSet


def _queued_id(session_id: str, enqueue_ts: str, content: str) -> str:
    """A stable synthetic id for a queued entry, salted by its session + enqueue.

    Stable across full ledger replays (so a replay reproduces the same ids) and
    distinct for two identical messages queued at different times or in different
    sessions. It is NOT a correlation key -- resolution is positional -- it only
    keys the rendered bubble on the frontend.
    """
    digest = hashlib.sha1(f"{session_id}\0{enqueue_ts}\0{content}".encode()).hexdigest()
    return digest[:16]


def _is_phantom_content(content: str) -> bool:
    """True for an enqueue that must never surface: a task-notification or blank."""
    return content.startswith(TASK_NOTIFICATION_CONTENT_PREFIX) or not content.strip()


class ClaudeQueueTracker:
    """Populates one agent's :class:`QueuedSet` from Claude's queue ledger."""

    # Declared at class level so ``build`` (no ``__init__``, matching the other
    # live-state holders) can assign it.
    _queued_set: QueuedSet

    @classmethod
    def build(cls) -> "ClaudeQueueTracker":
        tracker = cls.__new__(cls)
        tracker.reset()
        return tracker

    def consume(self, signal: QueueSignal) -> None:
        """Fold one recognized queue-ledger transition into the queued set.

        The watcher only ever feeds the latest main session's signals, and resets
        this tracker when a new latest session is registered, so no session
        discrimination happens here.
        """
        match signal.kind:
            case QueueSignalKind.ENQUEUE:
                # Task-notifications / blank enqueues are added as PHANTOM slots:
                # they hold a FIFO position so leaves stay aligned, but never
                # surface. Real user turns are added as visible entries.
                is_phantom = _is_phantom_content(signal.content)
                self._queued_set.add(
                    _queued_id(signal.session_id, signal.timestamp, signal.content),
                    signal.content,
                    signal.timestamp,
                    is_phantom,
                )
            case QueueSignalKind.LEAVE:
                self._queued_set.resolve_oldest()

    def on_idle(self) -> None:
        """Clear the queue -- the working->IDLE backstop.

        At a genuine IDLE the queue is drained (a queued message would have opened
        a turn), so any survivor is stale (an interrupt, a flush-restart SIGKILL, a
        crash -- none of which write a resolution record) and is dropped.
        """
        self._queued_set.clear()

    def clear(self) -> None:
        """Drop everything (a flush restart invalidated the harness queue)."""
        self._queued_set.clear()

    def reset(self) -> None:
        """Reset to an empty set (a truncation, or a new latest main session)."""
        self._queued_set = QueuedSet.build()

    def snapshot(self) -> list[dict[str, str]]:
        """The full wire snapshot of currently-queued messages, in enqueue order."""
        return self._queued_set.snapshot()

    def concatenated_block(self) -> str:
        """The queue as one newline-joined turn (shared by both common actions)."""
        return self._queued_set.concatenated_block()
