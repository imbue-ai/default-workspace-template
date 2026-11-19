from typing import ClassVar

from imbue.chat.harnesses.placeholder import PlaceholderActivityTracker


class RoomActivityTracker(PlaceholderActivityTracker):
    """The dot of a room: its reader never marks a turn in flight, so it reads idle; a room's row is marked working
    by the chats it asked instead (``awaiting_chat_ids`` on its snapshot)."""

    marker_filename: ClassVar[str] = "room_process_started"

    @property
    def is_tail_mid_turn(self) -> bool:
        """Never: a room has no turns, and every line of its transcript is a ``user_message``."""
        return False
