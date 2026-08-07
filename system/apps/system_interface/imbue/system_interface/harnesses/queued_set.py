"""The common, harness-agnostic queued-message entity.

A message the user sends while the agent cannot start a turn for it is *queued*:
the harness has parked it and has not acted on it yet. The queued state is
ephemeral live state (like the activity dot), not a durable transcript event --
when a queued message commits it leaves the set and appears in the transcript as
an ordinary ``user_message``.

``QueuedSet`` holds that live state and ALL of the behavior the frontend and the
two common actions rely on -- the wire snapshot, and the concatenated block both
actions resend / hand to the composer. It knows nothing about any harness: the
ONLY harness-specific code is the per-harness populator that maps that harness's
raw queue signals onto ``add`` / ``resolve_oldest`` / ``clear`` (see
:mod:`harnesses.claude.queue_tracker`). Everything downstream of this entity is
common, so the two buttons can never disagree about what "the queue" is.

Following the neighbouring live-state holders (``SessionFileState``,
``HarnessActivityTracker``), this is a plain mutable class rather than a pydantic
model: it is per-session scratch state mutated in place by its populator, not a
domain value object.
"""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class QueuedMessage(FrozenModel):
    """One entry parked in a harness's queue.

    ``queued_id`` is a stable, populator-minted identity (Claude hashes
    ``(session_id, enqueue_ts, content)``); the frontend keys the rendered bubble
    on it and it survives a full ledger replay unchanged.

    ``is_phantom`` marks an entry that occupies a FIFO slot (so positional leaves
    stay aligned under the conservation law) but must never surface to the user --
    e.g. a background task-notification or a blank enqueue. Phantom entries are
    filtered from :meth:`QueuedSet.snapshot` and :meth:`QueuedSet.concatenated_block`.
    """

    queued_id: str = Field(description="Stable id minted by the populator; stable across replays")
    content: str = Field(description="Verbatim message text the user queued")
    enqueue_ts: str = Field(description="Verbatim ISO timestamp from the harness's enqueue record")
    is_phantom: bool = Field(
        default=False, description="A FIFO slot-holder that never surfaces (task-notification / blank enqueue)"
    )


class QueuedSet:
    """The live, FIFO set of an agent's currently-queued messages.

    Mutated only by the per-harness populator via ``add`` / ``resolve_oldest`` /
    ``clear``; read by the common surface (``snapshot``) and the two common
    actions (``concatenated_block``). Holds no harness knowledge and no UI state.
    """

    # FIFO, oldest first: a message enters at the tail on enqueue and the oldest
    # leaves the head when a message leaves the queue. Declared at class level so
    # a ``build`` factory (no ``__init__``, matching the other live-state holders)
    # can assign it with the type checker satisfied.
    pending: list[QueuedMessage]

    @classmethod
    def build(cls) -> "QueuedSet":
        queued_set = cls.__new__(cls)
        queued_set.pending = []
        return queued_set

    def add(self, queued_id: str, content: str, enqueue_ts: str, is_phantom: bool) -> None:
        """Append a newly-queued entry to the FIFO tail.

        ``is_phantom`` marks a slot-holder (task-notification / blank enqueue) that
        keeps FIFO positions aligned but never surfaces.
        """
        self.pending.append(
            QueuedMessage(queued_id=queued_id, content=content, enqueue_ts=enqueue_ts, is_phantom=is_phantom)
        )

    def resolve_oldest(self) -> None:
        """Drop the FIFO head -- the oldest parked entry (phantom or real) left the queue.

        Positional and uniform: one leave record pops exactly one head, whether it
        is a phantom or a real entry, so positions stay aligned under the
        conservation law. A resolve on an empty set is a harmless no-op.
        """
        if self.pending:
            self.pending.pop(0)

    def clear(self) -> None:
        """Drop every entry (backstop sweep on working->IDLE, or a flush restart)."""
        self.pending.clear()

    def snapshot(self) -> list[dict[str, str]]:
        """The full wire snapshot pushed to the frontend -- REAL entries only, in order."""
        return [
            {"queued_id": message.queued_id, "content": message.content, "timestamp": message.enqueue_ts}
            for message in self.pending
            if not message.is_phantom
        ]

    def concatenated_block(self) -> str:
        """The single text both actions use: the REAL queued entries as one newline-joined turn.

        One builder, two callers (flush resends it; the composer action hands it
        back), so they can never disagree about what "the queue" is. Phantom slots
        are excluded. Empty string when there are no real entries.
        """
        return "\n".join(message.content for message in self.pending if not message.is_phantom)
