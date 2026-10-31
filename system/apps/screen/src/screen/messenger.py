"""Waking a queued agent: a message to its chat through the chat app (``message_chat``, the in-process send the
browser fleet's wake-ups took), which falls back to ``mngr message`` when the chat app cannot take it."""

from pathlib import Path

from loguru import logger
from pydantic import Field
from workspace_bare_scripts import message_chat

from screen.interfaces import AgentMessengerInterface


class ChatAgentMessenger(AgentMessengerInterface):
    """Sends through the chat messenger from the repo root; the browser fleet's sentinel renders the nudge as a
    collapsed chip in the transcript rather than a user bubble."""

    repo_root: Path = Field(frozen=True, description="Where the app registry and the ``mngr`` shim resolve from")

    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        try:
            returncode = message_chat.send(agent_id, message_chat.wrap_browser_fleet_nudge(text), cwd=self.repo_root)
        except OSError as e:
            logger.warning("Could not message agent {} about the Screen ({})", agent_name or agent_id, e)
            return
        if returncode != message_chat.EXIT_DELIVERED:
            logger.warning(
                "the chat messenger answered {} messaging agent {} about the Screen; the message may not have landed",
                returncode,
                agent_name or agent_id,
            )
