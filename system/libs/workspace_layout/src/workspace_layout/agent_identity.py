import os
from typing import Final

ENV_MINDS_CHAT_ID: Final[str] = "MINDS_CHAT_ID"
ENV_MNGR_AGENT_ID: Final[str] = "MNGR_AGENT_ID"
# The requester an agent's op carries is its own chat: the chat app's name, and the chat id its window's path carries.
AGENT_REQUESTER_APP_NAME: Final[str] = "chat"


def chat_id_from_environment() -> str | None:
    """The calling agent's own chat, or None outside an agent: ``MINDS_CHAT_ID`` from the chat app that created the
    agent, else the agent's own id (an agent created any other way is its own chat)."""
    return os.environ.get(ENV_MINDS_CHAT_ID, "") or os.environ.get(ENV_MNGR_AGENT_ID, "") or None
