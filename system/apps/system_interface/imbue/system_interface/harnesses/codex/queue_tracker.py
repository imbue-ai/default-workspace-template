"""The codex queued-message populator -- the ONLY codex-specific queue code.

Wraps one common :class:`QueuedSet` and maps codex's queue LEDGER onto its
mutators. The codex fork emits a full, id-keyed ledger to
``$CODEX_HOME/queued_input.jsonl`` (see :func:`codex.session_parser.parse_codex_queue_signals`
and ``docs/codex_queued_messages_impl.md``):

* ``queued_input`` (ENQUEUE) -> ``add`` a FIFO entry.
* ``queued_committed`` / ``queued_retracted`` (LEAVE) -> ``resolve(queued_id)``.

Resolution is BY ID -- exact, content-free, correct even for duplicate content --
which is cleaner than Claude's positional resolve (Claude's ledger names no id).
codex steers are all real user messages, so there is no phantom concept: every
enqueue is added visible.

Conservation holds only while the process lives (each enqueue gets exactly one
terminating record), so feeding the whole ledger from the start nets every enqueue
against its leave, leaving exactly the still-parked entries -- no durable cursor
needed. A kill-based death breaks conservation: the dying process writes no
terminating records, and the fork's restore-time retraction only closes entries
held in the *current* process's memory, so the on-disk records are orphaned
forever. The watcher therefore scopes the replay to the current process generation
(an enqueue predating the ``codex_process_started`` marker mtime is not fed here),
and the ``on_idle`` backstop sweeps any survivor.
"""

from imbue.system_interface.harnesses.codex.session_parser import CodexQueueSignal
from imbue.system_interface.harnesses.codex.session_parser import CodexQueueSignalKind
from imbue.system_interface.harnesses.queued_set import QueuedSet


class CodexQueueTracker:
    """Populates one codex agent's :class:`QueuedSet` from its queue ledger."""

    _queued_set: QueuedSet

    @classmethod
    def build(cls) -> "CodexQueueTracker":
        tracker = cls.__new__(cls)
        tracker.reset()
        return tracker

    def consume(self, signal: CodexQueueSignal) -> None:
        """Fold one recognized queue-ledger transition into the queued set."""
        match signal.kind:
            case CodexQueueSignalKind.ENQUEUE:
                # codex steers are all real user turns -- never phantom.
                self._queued_set.add(signal.queued_id, signal.content, signal.timestamp, is_phantom=False)
            case CodexQueueSignalKind.LEAVE:
                # Committed or retracted: the message this id names left the queue.
                self._queued_set.resolve(signal.queued_id)

    def on_idle(self) -> None:
        """Clear the queue -- the working->IDLE backstop.

        At a genuine IDLE the queue is drained (a parked steer would have injected),
        so any survivor is stale and is dropped -- e.g. an orphan of a process that
        died without writing its terminating records, matching the Claude backstop.
        """
        self._queued_set.clear()

    def clear(self) -> None:
        """Drop everything (a flush restart invalidated the harness queue)."""
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
