"""The chat app's own primitives: the chat id, and the shape every agent id shares with it.

A chat is the user-facing conversation (one tab, one instance key, one transcript); an agent is
one mngr agent running one harness on one account. A chat's id is the id of its first agent, so
the two strings are equal today and keep the same ``agent-<hex>`` shape, but they are distinct
types in code so the type checker finds every crossing between the two
(``docs/system/blueprint/chat-agent-split/``).
"""

import re
from typing import Final

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

# An agent id: ``agent-<32 hex>`` as mngr mints it, with the instance-key alphabet so a test
# fixture's id counts too. A chat id has the same shape (it is its first agent's id).
AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^agent-[A-Za-z0-9_-]{1,120}$")


class ChatId(NonEmptyStr):
    """A chat's id: the id of its first agent, distinct from an agent id in code.

    Its shape is ``AGENT_ID_PATTERN``'s. The presence route and the chat document check that
    shape (they accept a chat the app does not list yet); every other route resolves the id
    through the manager and answers 404 otherwise, so an id minted by mngr or by a test
    passes through unchecked.
    """


@pure
def chat_id_of_first_agent(agent_id: str) -> ChatId:
    """The chat an agent belongs to under the own-chat rule: a chat's id is its first agent's id.

    Every caller assumes the agent is the chat's first (and only) agent, which is true of
    every agent until a chat can have several.
    """
    # CLEANUP: replace every call with a lookup through the chat record store once phase 3 of
    # the chat-agent split lands, since a successor agent's chat id is then not its own id.
    return ChatId(agent_id)


@pure
def first_agent_id_of_chat(chat_id: ChatId) -> str:
    """The agent a chat's id names: its first agent, which is also its active agent today."""
    # CLEANUP: replace every call with the chat record store's active-agent lookup once phase 3
    # of the chat-agent split lands, since the active agent is then not always the first.
    return str(chat_id)
