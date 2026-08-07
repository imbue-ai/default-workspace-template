"""The pi queued-message populator -- the ONLY pi-specific queue code.

Wraps one common :class:`QueuedSet` and maps pi's delivery signals onto its
``add`` / ``resolve_oldest`` / ``clear`` mutators, exactly as
:mod:`harnesses.claude.queue_tracker` does for claude. Everything downstream (the
WS snapshot field and the two common actions) is harness-agnostic.

Where claude reconstructs enqueue/leave from an opaque in-transcript ledger, pi
holds both signals explicitly and simply:

* **enqueue** = a new line in mngr's ``pi_inbox`` -- the file mngr appends each
  outgoing message to (one JSON string per line) before the lifecycle extension
  injects it into pi via ``sendUserMessage(deliverAs: "followUp")``. So it is the
  verbatim, ordered list of everything we sent. A ``<task-notification>`` / blank
  line is added as a PHANTOM (holds a FIFO slot so positional leaves stay aligned,
  but never surfaces); anything else is a REAL entry.
* **leave** = a ``user_message`` draining into pi's native transcript. followUp
  parks a message inside pi until the running turn ends; when pi finally consumes
  it, it lands as a user record -- verified live (a mid-turn send hit ``pi_inbox``
  immediately but only entered the session file once the turn ended). One drained
  user turn pops one FIFO head.
* working -> IDLE -> ``clear`` (the backstop that sweeps interrupts / crashes /
  flush-restart SIGKILLs, none of which drain a queued message).

Resolution is POSITIONAL (drop the FIFO head): the inbox carries no correlation id
back to the drained turn, so -- like claude -- the oldest parked entry leaves when
a user turn commits. The watcher drives the mutators (see ``PiSessionWatcher``): it
feeds each new inbox line to :meth:`enqueue` and each newly-ingested ``user_message``
to :meth:`leave`. A ``/new`` rotation resets the set (pi's followUp queue is gone).
"""

import hashlib

from imbue.system_interface.harnesses.queued_set import QueuedSet

# A background notification injected as a queued message must never surface; it only
# holds a FIFO slot so positional leaves stay aligned (mirrors claude's phantom rule).
_TASK_NOTIFICATION_CONTENT_PREFIX = "<task-notification>"


def _queued_id(inbox_index: int, content: str) -> str:
    """A stable synthetic id for a queued entry, salted by its inbox position + content.

    Stable across full inbox replays (a replay reproduces the same ids) and distinct
    for two identical messages queued at different inbox positions. Not a correlation
    key -- resolution is positional -- it only keys the rendered bubble on the frontend.
    """
    digest = hashlib.sha1(f"{inbox_index}\0{content}".encode()).hexdigest()
    return digest[:16]


def _is_phantom_content(content: str) -> bool:
    """True for an inbox entry that must never surface: a task-notification or blank."""
    return content.startswith(_TASK_NOTIFICATION_CONTENT_PREFIX) or not content.strip()


class PiQueueTracker:
    """Populates one agent's :class:`QueuedSet` from pi's inbox + drained user turns."""

    _queued_set: QueuedSet

    @classmethod
    def build(cls) -> "PiQueueTracker":
        tracker = cls.__new__(cls)
        tracker.reset()
        return tracker

    def enqueue(self, inbox_index: int, content: str, timestamp: str) -> None:
        """Add one inbox line to the FIFO tail (phantom for a task-notification / blank)."""
        self._queued_set.add(
            _queued_id(inbox_index, content),
            content,
            timestamp,
            _is_phantom_content(content),
        )

    def leave(self) -> None:
        """A user turn drained: drop the FIFO head (phantom or real)."""
        self._queued_set.resolve_oldest()

    def on_idle(self) -> None:
        """Clear the queue -- the working->IDLE backstop (a genuine IDLE means it drained)."""
        self._queued_set.clear()

    def clear(self) -> None:
        """Drop everything (a ``/new`` rotation, or a flush restart)."""
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
