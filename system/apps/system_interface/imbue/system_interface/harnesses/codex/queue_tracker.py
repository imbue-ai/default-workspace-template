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

Conservation holds (each enqueue gets exactly one terminating record), so feeding
the whole ledger from the start is self-correcting -- every enqueue nets against
its leave, leaving exactly the still-parked entries, no durable cursor needed. The
ledger also self-cleans on ``codex resume`` (the fork retracts every live entry),
so unlike Claude no session/rotation reset is required; the ``on_idle`` backstop
is retained purely as a coarse safety net.
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
        so any survivor is stale and is dropped. codex's ledger already retracts live
        entries on resume, so this is a coarse safety net (a crash that skipped the
        retract records), matching the Claude backstop.
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
