from datetime import datetime

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import HasCompactionMixin


class RecordingCompactionAgent(HasCompactionMixin):
    """An agent mngr can compact, idle since ``idle_since`` (None: mid-turn), recording each compaction requested.

    ``error`` is raised by ``request_compaction`` in place of compacting, after the request is recorded.
    """

    def __init__(self, idle_since: datetime | None, error: MngrError | None = None) -> None:
        self.idle_since = idle_since
        self.error = error
        # Each request, as ``(instructions, message_lock_timeout_seconds, expected_idle_since)``.
        self.requests: list[tuple[str | None, float | None, datetime | None]] = []

    def request_compaction(
        self,
        instructions: str | None = None,
        message_lock_timeout_seconds: float | None = None,
        expected_idle_since: datetime | None = None,
    ) -> None:
        self.requests.append((instructions, message_lock_timeout_seconds, expected_idle_since))
        if self.error is not None:
            raise self.error

    def get_idle_since(self) -> datetime | None:
        return self.idle_since
