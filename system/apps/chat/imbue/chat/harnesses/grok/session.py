"""grok's session: a send holds the message for the watcher's flush worker and never types into grok."""

from loguru import logger

from imbue.chat.activity_state import is_lifecycle_dead
from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.chat.harnesses.grok.turn_gate import is_ready_to_type
from imbue.chat.harnesses.queued_set import now_enqueue_timestamp
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SendOutcome


class GrokHarnessSession(FileHarnessSession):
    """grok's file session, whose sends wait in the agent's held queue for the one typist."""

    def send(self, text: str, message_id: str, is_resent_when_unsure: bool = True) -> SendOutcome:
        """Hold the message for the flush worker; NOT_READY while grok is starting or mngr reports it dead.

        The queue first adopts the running grok's session, so a message sent just after a relaunch is not dropped
        with the old session's.
        """
        lifecycle_state = self._deps.read_lifecycle_state()
        if self.is_starting_up() or (lifecycle_state is not None and is_lifecycle_dead(lifecycle_state)):
            return SendOutcome.NOT_READY
        queue = self._queue()
        queue.set_session(session_token(self._deps.state_dir))
        is_turn_open = not is_ready_to_type(lifecycle_state, self._deps.state_dir)
        queue.enqueue(text, now_enqueue_timestamp(), is_turn_open=is_turn_open)
        self._deps.notify_agents_changed()
        logger.debug("grok: holding message {} for the flush worker", message_id)
        return SendOutcome.OK

    def is_sending(self) -> bool:
        return self._queue().is_sending() or super().is_sending()

    def has_held_commands(self) -> bool:
        """Whether the control lane holds a model bar pick."""
        return self._queue().has_control_commands()

    def is_tap_available(self, *, has_queued: bool) -> bool:
        """Offered while a held message still has delivery attempts left and nothing is Sending."""
        return super().is_tap_available(has_queued=has_queued) and self._queue().has_deliverable_entries()

    def _queue(self) -> GrokQueueTracker:
        return get_tracker(self._deps.state_dir)
