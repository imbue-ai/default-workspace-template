"""Mentions (the multiplayer spec, sections 7.1 and 7.2), pure: the handle table a workspace answers, and the
handles a text mentions.

A handle is a participant's display name with runs of whitespace as one hyphen and anything outside
``[A-Za-z0-9._-]`` dropped, matched case-insensitively; the shell assigns the roster's handles (``@Josh``, and
``@Josh-2`` for the later of two Joshes), the workspace's agent is ``@agent``, and every chat's title as it stands
now is a handle too (``@Main-chat``), resolved after the people and agents so a chat never shadows a person. A
sent message's mentions are found here on the server by the same rule the composer's popover uses, so a handle
typed by hand resolves the same as one picked.
"""

import re
from collections.abc import Sequence
from typing import Final

from pydantic import Field
from workspace_layout.primitives import ParticipantKey
from workspace_layout.roster import RosterParticipant
from workspace_layout.roster import assign_handles

from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The kind a chat's handle wears in the table, beside the roster's participant kinds.
CHAT_HANDLE_KIND: Final[str] = "chat"

# ``@`` then a handle, not inside a word or another mention (``foo@bar`` and ``@@x`` mention nobody). The handle
# alphabet includes ``.`` and ``-``, so a sentence's trailing punctuation rides along and is tried off below.
MENTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]+)")
_TRAILING_PUNCTUATION: Final[str] = ".-_"


class MentionTarget(FrozenModel):
    """One handle and whom it names: a participant (by key) or a chat (by id), never both."""

    handle: str = Field(description="The handle, as the table spells it")
    display_name: str = Field(description="The name the handle stands for")
    kind: str = Field(description="The roster's participant kind, or ``chat``")
    key: ParticipantKey | None = Field(default=None, description="The participant the handle names, if one")
    chat_id: ChatId | None = Field(default=None, description="The chat the handle names, if one")

    @property
    def is_chat(self) -> bool:
        return self.chat_id is not None


class MentionTable(FrozenModel):
    """What ``GET /api/mentions`` answers: every handle the workspace resolves, people first, then agents, then chats."""

    handles: tuple[MentionTarget, ...] = Field(description="The handle table, in resolution order")


@pure
def handle_table(
    participants: Sequence[RosterParticipant], chats: Sequence[tuple[ChatId, str]]
) -> tuple[MentionTarget, ...]:
    """The table: the roster's handles as the shell assigned them, then one per chat title, suffixed on a collision
    with anything before it."""
    people = [
        MentionTarget(handle=entry.handle, display_name=entry.display_name, kind=entry.kind.value, key=entry.key)
        for entry in participants
    ]
    chat_handles = assign_handles([title for _chat_id, title in chats], taken=[entry.handle for entry in people])
    chat_targets = [
        MentionTarget(handle=handle, display_name=title, kind=CHAT_HANDLE_KIND, chat_id=chat_id)
        for (chat_id, title), handle in zip(chats, chat_handles, strict=True)
    ]
    return (*people, *chat_targets)


@pure
def find_mentions(text: str, table: Sequence[MentionTarget]) -> tuple[MentionTarget, ...]:
    """The targets ``text`` mentions, each once, in order of first appearance. A token that matches no handle is
    tried again with its trailing punctuation taken off (``@Josh.`` mentions Josh)."""
    by_handle = {target.handle.casefold(): target for target in table}
    found: list[MentionTarget] = []
    for match in MENTION_PATTERN.finditer(text):
        token = match.group(1)
        target = None
        while token and target is None:
            target = by_handle.get(token.casefold())
            if target is None:
                token = token.rstrip(_TRAILING_PUNCTUATION) if token[-1] in _TRAILING_PUNCTUATION else ""
        if target is not None and target not in found:
            found.append(target)
    return tuple(found)
