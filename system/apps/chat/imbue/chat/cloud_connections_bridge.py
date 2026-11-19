"""What the cloud-connection routes borrow from the router: a chat's agent, its restart, and a message to it.

The routes live in ``cloud_connections_endpoints`` and the router in ``server``; the app state holds the
router's bridge between them, so neither module imports the other.
"""

from abc import ABC
from abc import abstractmethod

from flask import Response

from imbue.chat.agent_discovery import AgentInfo
from imbue.imbue_common.mutable_model import MutableModel


class ChatSendRefusedError(RuntimeError):
    """The chat could not take a message the cloud-connection routes sent it."""


class CloudConnectionsChatBridge(MutableModel, ABC):
    """The router's side of the cloud-connection routes."""

    @abstractmethod
    def find_agent(self, chat_id: str) -> AgentInfo | None:
        """The agent ``chat_id`` runs on, or None for an id that names no chat."""

    @abstractmethod
    def refuse_restart(self, chat_id: str, agent_info: AgentInfo) -> Response | None:
        """The answer for a chat whose agent must not be restarted now, or None."""

    @abstractmethod
    def restart(self, chat_id: str, agent_info: AgentInfo) -> str:
        """Restart the chat's agent and return the messages that were queued for it; raises AgentRestartError."""

    @abstractmethod
    def send(self, chat_id: str, text: str) -> None:
        """Send the chat's agent a message through the ordinary send path; raises ChatSendRefusedError."""
