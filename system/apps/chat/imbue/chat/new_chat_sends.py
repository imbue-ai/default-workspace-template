"""The sends a chat receives while it is being created.

A message typed into a chat whose agent does not exist yet waits here, in its own request,
until the create settles: then the waiting sends go to the new agent one at a time in the
order they arrived, or every one of them is refused with the create's failure. The gate stays
until the last waiting send has gone, so a send that arrives meanwhile cannot overtake them.

Whether anyone sent is also what decides a silent start's greeting: a chat that was given a
message while it started has something to say, so it is not greeted (``AgentManager``).
"""

import threading

from imbue.chat.errors import ChatAppError


class NewChatCreateFailedError(ChatAppError):
    """The chat a send was waiting on could not be created, so the send was not delivered."""


class NewChatSendGate:
    """Holds one creating chat's sends until the create settles, then lets them through in arrival order."""

    _condition: threading.Condition
    _issued_ticket_count: int
    _next_ticket_to_deliver: int
    _is_settled: bool
    _failure: str | None

    @classmethod
    def build(cls) -> "NewChatSendGate":
        gate = cls.__new__(cls)
        gate._condition = threading.Condition()
        gate._issued_ticket_count = 0
        gate._next_ticket_to_deliver = 0
        gate._is_settled = False
        gate._failure = None
        return gate

    def take_ticket(self) -> int:
        """Join the line: the returned ticket is this send's place in it."""
        with self._condition:
            ticket = self._issued_ticket_count
            self._issued_ticket_count = ticket + 1
            return ticket

    def has_issued_tickets(self) -> bool:
        with self._condition:
            return self._issued_ticket_count > 0

    def wait_for_turn(self, ticket: int) -> None:
        """Block until the create has settled and every earlier send has gone.

        Raises ``NewChatCreateFailedError`` when the create failed: nothing waiting is delivered then.
        """
        with self._condition:
            self._condition.wait_for(
                lambda: self._failure is not None or (self._is_settled and self._next_ticket_to_deliver == ticket)
            )
            if self._failure is not None:
                raise NewChatCreateFailedError(self._failure)

    def finish_turn(self, ticket: int) -> None:
        """Let the next send through, whether this one was delivered or not."""
        with self._condition:
            if ticket == self._next_ticket_to_deliver:
                self._next_ticket_to_deliver = ticket + 1
                self._condition.notify_all()

    def open(self) -> None:
        """The chat's agent is up and settled: the waiting sends may go."""
        with self._condition:
            self._is_settled = True
            self._condition.notify_all()

    def fail(self, reason: str) -> None:
        """The create failed: every waiting send is refused with ``reason``."""
        with self._condition:
            self._failure = reason
            self._condition.notify_all()

    def is_drained(self) -> bool:
        """Whether the gate has nothing left to order: it is open and every send in line has gone."""
        with self._condition:
            return self._is_settled and self._next_ticket_to_deliver == self._issued_ticket_count
