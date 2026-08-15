"""The backend record of messages in the *Sending* state (contract A1).

A message the UI has POSTed but the backend has not yet resolved to Delivered
(committed as a user turn), Queued (parked in the harness queue), or Returned
(the send failed) is *Sending*: accepted, in flight, not yet confirmed. Unlike a
Queued message it has no on-disk harness record yet, so the backend holds it here
for the duration of the (synchronous) send.

The one consumer that needs it is Interrupt (contract B / A4): when a stop fires
while a send is still in flight, the message never committed, so it must return to
the composer rather than be lost. The stop path reads :meth:`concatenated_block`
for exactly the still-in-flight sends (each keyed by the stable send-time id) and
folds them into the returned block.

Following the neighbouring live-state holders (:class:`QueuedSet`,
:class:`HarnessActivityTracker`) this is a plain mutable class, not a pydantic
value object: it is per-agent scratch state. It holds no lock of its own -- the
owning watcher mutates and reads it under that watcher's lock, exactly as it does
its queued-message populator.
"""

import threading
from uuid import uuid4

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class SendingRecord(FrozenModel):
    """One message the backend has accepted for send but not yet resolved.

    ``token`` is the stable send-time id the sender minted (contract A4); it keys
    the record so the exact message can be resolved on commit/enqueue/failure,
    correct even for duplicate content.
    """

    token: str = Field(description="Stable send-time id; keys resolution and the frontend's Sending bubble")
    content: str = Field(description="Verbatim message text being sent")


class SendingRegistry:
    """The ordered set of an agent's currently in-flight (Sending) messages.

    Records are appended in send order and removed by ``token`` when the send
    resolves. Read only by the interrupt path, which needs the still-in-flight
    text to return to the composer.
    """

    # Insertion-ordered (send order); a message is appended on note and removed on
    # resolve. Declared at class level so a ``build`` factory (no ``__init__``,
    # matching the other live-state holders) can assign it.
    pending: list[SendingRecord]

    @classmethod
    def build(cls) -> "SendingRegistry":
        registry = cls.__new__(cls)
        registry.pending = []
        return registry

    def record(self, token: str, content: str) -> None:
        """Append a newly-accepted in-flight send.

        A repeated token (the same send noted twice) replaces the prior entry's
        content in place rather than duplicating, so the set stays one-per-token.
        """
        for existing in self.pending:
            if existing.token == token:
                self.pending = [
                    SendingRecord(token=token, content=content) if entry.token == token else entry
                    for entry in self.pending
                ]
                return
        self.pending.append(SendingRecord(token=token, content=content))

    def resolve(self, token: str) -> None:
        """Drop the record this token names -- the send committed, enqueued, or failed.

        A resolve of an unknown token (already resolved, or never recorded) is a
        harmless no-op.
        """
        self.pending = [record for record in self.pending if record.token != token]

    def clear(self) -> None:
        """Drop every record (a process restart invalidated the in-flight set)."""
        self.pending.clear()

    def in_flight_texts(self) -> list[str]:
        """The verbatim text of every still-in-flight send, in send order."""
        return [record.content for record in self.pending]

    def concatenated_block(self) -> str:
        """The still-in-flight sends as one newline-joined block, in send order.

        Empty string when nothing is in flight. Mirrors ``QueuedSet.concatenated_block``
        so the interrupt path can concatenate the queued block and this block uniformly.
        """
        return "\n".join(record.content for record in self.pending)


class SendingStateWatcherMixin:
    """The four *Sending*-state methods of :class:`AgentSessionWatcher`, backed by a
    :class:`SendingRegistry`, shared by every watcher that tracks the state on-watcher.

    A watcher tracks *Sending* (contract A1: a message the UI POSTed that the backend has
    not yet resolved to Delivered/Queued/Returned) when the send goes through the watcher
    rather than a separate ledger -- i.e. the claude and pi harnesses. Both had (or would
    have had) an identical copy of these four methods; this mixin is the single copy they
    inherit. The base :class:`AgentSessionWatcher` keeps its no-op defaults, so a harness
    whose send bypasses the watcher (codex, via its live ledger) is unaffected.

    Mix it in AHEAD of ``AgentSessionWatcher`` in the bases so these override the no-ops,
    and call :meth:`_init_sending_state` from the subclass ``build`` (watchers construct via
    ``cls.__new__``/``build``, not ``__init__``). The registry has its OWN private lock, not
    the subclass's transcript lock, so the mixin needs nothing from the subclass and the two
    concerns never contend.
    """

    _sending_registry: SendingRegistry
    _sending_lock: threading.Lock

    def _init_sending_state(self) -> None:
        """Create the empty registry + its private lock. Called from the subclass ``build``."""
        self._sending_registry = SendingRegistry.build()
        self._sending_lock = threading.Lock()

    def note_sent_message(self, content: str, message_id: str = "") -> str | None:
        """Record a message the send endpoint is about to deliver as *Sending* (contract A1).

        Keyed by the sender's stable send-time id (or a minted one when the caller sent
        none), so :meth:`commit_sent_message` / :meth:`retract_sent_message` resolve the
        exact message even when two identical messages are in flight. Returns the token.
        """
        token = message_id or uuid4().hex
        with self._sending_lock:
            self._sending_registry.record(token, content)
        return token

    def retract_sent_message(self, token: str) -> None:
        """The send failed: drop its Sending record (the message is Returned, not Sending)."""
        with self._sending_lock:
            self._sending_registry.resolve(token)

    def commit_sent_message(self, token: str) -> None:
        """The send resolved (committed or queued): drop its Sending record."""
        with self._sending_lock:
            self._sending_registry.resolve(token)

    def get_in_flight_block(self) -> str:
        """The still-in-flight (Sending) messages as one concatenated block (''=none)."""
        with self._sending_lock:
            return self._sending_registry.concatenated_block()
