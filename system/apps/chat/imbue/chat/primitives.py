"""The chat app's own primitives: the chat id, and the shape every agent id shares with it.

A chat is the user-facing conversation (one window, one transcript); an agent is
one mngr agent running one harness on one account. A chat's id is the id of its first agent, so
the two strings keep the same ``agent-<hex>`` shape, but they are distinct types in code so the
type checker finds every crossing between the two, and a successor agent's chat id is not its own
(``docs/system/blueprint/chat-agent-split/``).
"""

import re
from enum import auto
from typing import Final

from app_manifest.primitives import AppName

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

# The chat app's registered name: the one app that may name itself to the shell (the client-activity
# report, the manifest, the desktop's open op).
CHAT_APP_NAME: Final[AppName] = AppName("chat")

# An agent id: ``agent-<32 hex>`` as mngr mints it, loosened so a test fixture's id counts too. An agent chat's
# id has the same shape (it is its first agent's id).
AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^agent-[A-Za-z0-9_-]{1,120}$")
# A room's id, as ``rooms.py`` mints it: the prefix is for people and logs, not a type (the multiplayer spec, S3).
ROOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^room-[0-9a-f]{16}$")
# Any chat's id, whatever its kind: what the places that check an id's shape before resolving it check.
CHAT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    f"(?:{AGENT_ID_PATTERN.pattern.strip('^$')})|(?:{ROOM_ID_PATTERN.pattern.strip('^$')})"
)

# A subagent view's key is the chat id, the agent id, and the session id joined by dots, which
# no agent id carries; the chat serves the view at ``/<key>``.
SUBAGENT_KEY_SEPARATOR: Final[str] = "."


class ChatStatus(LowerCaseStrEnum):
    """What a chat is doing, as its snapshot reports it and the chat root's rail draws it (a wire value).

    A chat switching agents is ``working``, or ``error`` once the switch failed, whatever its agent
    does. Otherwise the first that holds: ``stopped`` (the agent is dead), ``attention`` (a
    permission request waits on the user), ``working`` (a turn is in flight), ``background`` (no
    turn, but a pending background task will start one), ``idle``.
    """

    WORKING = auto()
    BACKGROUND = auto()
    IDLE = auto()
    ATTENTION = auto()
    STOPPED = auto()
    ERROR = auto()


class ChatKind(LowerCaseStrEnum):
    """What a chat is (a wire value on its snapshot): a conversation with an agent, or a room, which is a message
    log among the workspace's people with no agent of its own (the multiplayer spec, S3). A later kind adds a
    value here, never a second id type."""

    AGENT = auto()
    ROOM = auto()


class ChatId(NonEmptyStr):
    """A chat's id, whatever its kind: an agent chat's is the id of its first agent, distinct from an agent id in
    code; a room's is minted by the room store.

    Its shape is ``CHAT_ID_PATTERN``'s. The presence route and the chat document check that
    shape (they accept a chat the app does not list yet); every other route resolves the id
    through the manager or the room store and answers 404 otherwise, so an id minted by mngr or by a test
    passes through unchecked.
    """


@pure
def parse_chat_ref(chat_ref: str) -> ChatId | None:
    """The chat id a caller's string names, or None for a blank one.

    Routes and page keys hand the manager whatever string they were given; a blank one
    names no chat, and answering None lets the caller say "not found" instead of tripping over
    the primitive's own validation.
    """
    if not chat_ref.strip():
        return None
    return ChatId(chat_ref)
