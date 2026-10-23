"""grok's session: a send holds the message for the flush worker and never types into grok."""

import threading
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.harnesses.grok.queue_tracker import GrokQueueTracker
from imbue.chat.harnesses.grok.queue_tracker import SEND_REJECTING_VERDICTS
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.queue_tracker import session_token
from imbue.chat.harnesses.grok.turn_state import is_hold_required
from imbue.chat.harnesses.sending_registry import SendingRegistry
from imbue.chat.harnesses.session import FileHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.mngr_grok.api import read_grok_pid

# How long a send accepted against an idle grok waits for the worker's first send of it before answering.
SEND_RESULT_WAIT_SECONDS: Final[float] = 30.0


class GrokHarnessSession(FileHarnessSession):
    """grok's file session, whose sends wait in the agent's held queue for the one typist."""

    send_result_wait_seconds: float

    @classmethod
    def build(cls, deps: SessionDeps) -> "GrokHarnessSession":
        self = cls.__new__(cls)
        self._deps = deps
        self._sending = SendingRegistry.build()
        self._sending_lock = threading.Lock()
        self.send_result_wait_seconds = SEND_RESULT_WAIT_SECONDS
        return self

    def ensure_live(self) -> None:
        """Give the flush worker this session's send, which carries mngr's reason for a refusal."""
        self._queue().bind_session_send(self._deps.send_to_harness)

    def close(self) -> None:
        self._queue().unbind_session_send(self._deps.send_to_harness)

    def send(self, text: str, message_id: str) -> SendOutcome:
        """Hold the message for the flush worker, the only typist.

        NOT_READY while grok is not running or still starting, so the route starts it and retries. The queue
        first adopts the running grok's session, so a message sent just after a relaunch is not dropped with the
        old session's. A message accepted with no turn in the way answers once the worker's first send of it
        settles: a send mngr refused for a gate screen or an unreachable grok fails here with mngr's words, and the
        message leaves the queue.
        """
        if read_grok_pid(self._deps.state_dir) is None or self.is_starting_up():
            return SendOutcome.NOT_READY
        self.ensure_live()
        queue = self._queue()
        queue.set_session(session_token(self._deps.state_dir))
        is_turn_open = is_hold_required(self._deps.state_dir)
        queued_id = queue.enqueue(text, _now_iso(), is_turn_open=is_turn_open)
        self._deps.notify_agents_changed()
        logger.debug("grok: holding message {} for the flush worker", message_id)
        if is_turn_open:
            return SendOutcome.OK
        result = queue.wait_for_send_result(queued_id, self.send_result_wait_seconds)
        if result is not None and result.verdict in SEND_REJECTING_VERDICTS and queue.withdraw(queued_id):
            self._deps.notify_agents_changed()
            raise SendFailedError(result.reason or "grok did not take the message", result.failure_kind or "unknown")
        return SendOutcome.OK

    def is_sending(self) -> bool:
        return self._queue().is_sending() or super().is_sending()

    def is_tap_available(self, *, has_queued: bool) -> bool:
        """Offered while a held message still has delivery attempts left and nothing is Sending."""
        return super().is_tap_available(has_queued=has_queued) and self._queue().has_deliverable_entries()

    def _queue(self) -> GrokQueueTracker:
        return get_tracker(self._deps.state_dir)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
