"""Opening the Fix chat: the brief goes to the chat app's create route through the workspace's chat client."""

import time
from abc import ABC
from abc import abstractmethod
from pathlib import Path

from loguru import logger
from pydantic import Field
from workspace_bare_scripts import message_chat

from imbue.imbue_common.mutable_model import MutableModel
from routines_app.errors import FixChatError


class FixChatStarterInterface(MutableModel, ABC):
    """What the app's fix route calls: the brief and the chat's name in, the chat's id out."""

    @abstractmethod
    def start(self, brief: str, name: str) -> str:
        """Open the chat on the brief and answer its id; raises FixChatError when it could not be opened."""


class ChatAppFixChatStarter(FixChatStarterInterface):
    """The starter the served app uses: the chat app's create route, which answers once the chat runs or its own
    wait runs out."""

    repo_root: Path = Field(frozen=True, description="The checkout whose app registry names the chat app")
    environ: dict[str, str] = Field(frozen=True, description="The process environment, which may move the registry")

    def start(self, brief: str, name: str) -> str:
        request = message_chat.CreateRequest(name=name, message=brief, labels={"auto_open": "true"})
        result = message_chat.create_through_chat_app(
            message_chat.chat_app_url(self.environ, self.repo_root), request, time.monotonic, time.sleep
        )
        if isinstance(result, message_chat.SendResult):
            logger.warning("Starting a fix chat failed ({}): {}", result.outcome.name, result.detail)
            raise FixChatError(result.detail or "The chat could not be started.")
        return result.chat_id
