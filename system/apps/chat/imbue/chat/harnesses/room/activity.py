from typing import ClassVar

from imbue.chat.harnesses.placeholder import PlaceholderActivityTracker


class RoomActivityTracker(PlaceholderActivityTracker):
    """The dot of a room: its reader never marks a turn in flight, so it reads idle; a room's row is marked working
    by the chats it asked instead (``awaiting_chat_ids`` on its snapshot)."""

    marker_filename: ClassVar[str] = "room_process_started"
