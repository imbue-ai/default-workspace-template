from pydantic import Field

from routines.interfaces import NotifierInterface
from routines.primitives import RoutineId


class RecordingNotifier(NotifierInterface):
    """Keeps every notification instead of sending it."""

    sent: list[tuple[RoutineId, str, str]] = Field(
        default_factory=list, description="Every notification as (routine, title, message), in order"
    )
    is_accepting: bool = Field(default=True, description="What the app answers")

    def notify(self, routine_id: RoutineId, title: str, message: str) -> bool:
        self.sent.append((routine_id, title, message))
        return self.is_accepting
