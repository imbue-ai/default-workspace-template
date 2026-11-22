"""Suggestions: short entries above the composer that the user can click instead of typing.

A chat's next-step suggestions are the replies offered under one assistant message, kept as one
file in the chat's own folder so every page of the chat reads the same set and it travels with
the chat. The file names the message it follows (``after_event_id``): a page shows the set only
while that message is still the chat's last assistant message, so a set left over from an
earlier turn is never shown.
"""

from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.chat.chat_setting_files import read_chat_setting_file
from imbue.chat.chat_setting_files import remove_chat_setting_file
from imbue.chat.chat_setting_files import write_chat_setting_file
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

SUGGESTIONS_FILENAME: Final[str] = "suggestions.json"
SUGGESTIONS_FILE_VERSION: Final[int] = 1
# What is read from a reply for a set: a label is one line of at most this many characters, and at most this many
# are taken (a model's suggestions, or the ideas of a list, which the tree's two fixed replies then follow).
MAX_SUGGESTION_LABEL_LENGTH: Final[int] = 80
MAX_SUGGESTION_COUNT: Final[int] = 10


class SuggestionSource(LowerCaseStrEnum):
    """Where a set of suggestions came from (a wire value)."""

    # The chat app's own, with no model asked: its own words, or labels read out of the reply the set follows.
    FIXED = auto()
    # Worked out by a model from the conversation.
    GENERATED = auto()


class SuggestionsStatus(LowerCaseStrEnum):
    """Where a chat's next-step suggestions stand, as the read route reports it (a wire value)."""

    READY = auto()
    # A set for the chat's last reply is being worked out; a ``suggestions_updated`` push follows when one is stored.
    PENDING = auto()
    NONE = auto()


class Suggestion(FrozenModel):
    """One clickable entry. Clicking it sends its label as the user's message, so what the pill says and what
    the transcript shows never differ."""

    suggestion_id: str = Field(min_length=1, description="The suggestion's id, unique within its set")
    label: str = Field(min_length=1, description="What the pill says, and the message a click sends")
    is_recommended: bool = Field(default=False, description="Whether the pill is drawn as the recommended one")


class ChatSuggestions(FrozenModel):
    """What a chat's suggestions file holds: the set offered under one assistant message."""

    version: int = Field(
        default=SUGGESTIONS_FILE_VERSION,
        ge=1,
        le=SUGGESTIONS_FILE_VERSION,
        description="The on-disk shape the file was written with",
    )
    after_event_id: str = Field(min_length=1, description="The event id of the assistant message the set follows")
    source: SuggestionSource = Field(description="Where the set came from")
    suggestions: tuple[Suggestion, ...] = Field(description="The suggestions, in the order they are drawn")


def read_chat_suggestions(chat_dir: Path) -> ChatSuggestions | None:
    """The chat's stored set; None for a chat that has none, or an unreadable file (warned about)."""
    return read_chat_setting_file(chat_dir, SUGGESTIONS_FILENAME, ChatSuggestions, "suggestions")


def write_chat_suggestions(chat_dir: Path, suggestions: ChatSuggestions) -> None:
    """Replace the chat's stored set whole (a temp file renamed into place)."""
    write_chat_setting_file(chat_dir, SUGGESTIONS_FILENAME, suggestions)


def remove_chat_suggestions(chat_dir: Path) -> bool:
    """Remove the chat's stored set; False when it had none."""
    return remove_chat_setting_file(chat_dir, SUGGESTIONS_FILENAME)
