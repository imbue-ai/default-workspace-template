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

# An agent id: ``agent-<32 hex>`` as mngr mints it, loosened so a test fixture's id counts too. A
# chat id has the same shape (it is its first agent's id).
AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^agent-[A-Za-z0-9_-]{1,120}$")

# A subagent view's key is the chat id, the agent id, and the session id joined by dots, which
# no agent id carries; the chat serves the view at ``/<key>``.
SUBAGENT_KEY_SEPARATOR: Final[str] = "."


class ChatStatus(LowerCaseStrEnum):
    """What a chat is doing, as its snapshot reports it and the chat root's rail draws it (a wire value)."""

    WORKING = auto()
    IDLE = auto()
    ATTENTION = auto()
    STOPPED = auto()
    ERROR = auto()


class ChatId(NonEmptyStr):
    """A chat's id: the id of its first agent, distinct from an agent id in code.

    Its shape is ``AGENT_ID_PATTERN``'s. The presence route and the chat document check that
    shape (they accept a chat the app does not list yet); every other route resolves the id
    through the manager and answers 404 otherwise, so an id minted by mngr or by a test
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
