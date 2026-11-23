"""Where the chat app keeps chats' next-step suggestions.

A chat's stored set is one file in its folder (``suggestions.py``), replaced when a later reply's set is ready and
removed when a turn ends with none, and it is what a restart of the app finds. A page also draws the set of every
reply that has since been answered, under that reply, so the store remembers, in memory alone, each set it has
stored for a chat and the one it found stored, by the reply the set follows. What is remembered goes when the app
stops or the chat is deleted.
"""

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.primitives import ChatId
from imbue.chat.suggestions import ChatSuggestions
from imbue.chat.suggestions import read_chat_suggestions
from imbue.chat.suggestions import remove_chat_suggestions
from imbue.chat.suggestions import write_chat_suggestions
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# How many sets are remembered for one chat: a chat that outruns it loses the lines of its oldest replies.
MAX_REMEMBERED_SETS_PER_CHAT: Final[int] = 200


@pure
def with_set_remembered(
    remembered: Mapping[str, ChatSuggestions], suggestions: ChatSuggestions, limit: int
) -> dict[str, ChatSuggestions]:
    """A chat's remembered sets, by the reply each follows and oldest first, with ``suggestions`` as the newest.

    A set already remembered for the same reply gives way to it, and the oldest go once there are more than
    ``limit``.
    """
    kept = {reply: held for reply, held in remembered.items() if reply != suggestions.after_event_id}
    kept[suggestions.after_event_id] = suggestions
    return dict(list(kept.items())[-limit:])


class ChatSuggestionStore(MutableModel):
    """Every chat's next-step suggestions: its stored set, in its folder, and the sets remembered for it."""

    chat_files_root: Path = Field(frozen=True, description="The folder holding one folder per chat")

    # Held across each read and write of a chat's file, so sets are remembered in the order they were stored.
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _remembered_by_chat: dict[ChatId, dict[str, ChatSuggestions]] = PrivateAttr(default_factory=dict)

    def read(self, chat_id: ChatId) -> ChatSuggestions | None:
        """The chat's stored set, remembered from here on; None for a chat that has none."""
        with self._lock:
            stored = read_chat_suggestions(self.chat_files_root / chat_id)
            if stored is not None:
                self._remember_locked(chat_id, stored)
            return stored

    def write(self, chat_id: ChatId, suggestions: ChatSuggestions) -> None:
        """Make ``suggestions`` the chat's stored set, and remember it."""
        with self._lock:
            write_chat_suggestions(self.chat_files_root / chat_id, suggestions)
            self._remember_locked(chat_id, suggestions)

    def remove(self, chat_id: ChatId) -> bool:
        """Leave the chat with no stored set; False when it had none. What is remembered for it stays."""
        with self._lock:
            return remove_chat_suggestions(self.chat_files_root / chat_id)

    def remembered(self, chat_id: ChatId) -> tuple[ChatSuggestions, ...]:
        """Every set remembered for the chat, oldest first."""
        with self._lock:
            return tuple(self._remembered_by_chat.get(chat_id, {}).values())

    def forget(self, chat_id: ChatId) -> None:
        """Drop what is remembered for a chat that is gone."""
        with self._lock:
            self._remembered_by_chat.pop(chat_id, None)

    def _remember_locked(self, chat_id: ChatId, suggestions: ChatSuggestions) -> None:
        self._remembered_by_chat[chat_id] = with_set_remembered(
            self._remembered_by_chat.get(chat_id, {}), suggestions, MAX_REMEMBERED_SETS_PER_CHAT
        )
