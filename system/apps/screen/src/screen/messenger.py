"""Waking a queued agent: a message to its chat through the chat app (``message_chat``, the in-process send the
browser fleet's wake-ups took), which falls back to ``mngr message`` when the chat app cannot take it."""

import concurrent.futures
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from workspace_bare_scripts import message_chat

from screen.interfaces import AgentMessengerInterface

# A send has no read timeout of its own (a chat app that never answers holds it), so it runs on a thread of
# its own and the caller -- the lease's release and the stack's stop -- waits only this long for the answer.
MESSAGE_TIMEOUT_SECONDS: Final[float] = 60.0

SendMessage = Callable[[str, str, Path], int]


def send_through_chat_app(agent_id: str, text: str, repo_root: Path) -> int:
    return message_chat.send(agent_id, message_chat.wrap_browser_fleet_nudge(text), cwd=repo_root)


class ChatAgentMessenger(AgentMessengerInterface):
    """Sends through the chat messenger from the repo root; the browser fleet's sentinel renders the nudge as a
    collapsed chip in the transcript rather than a user bubble."""

    model_config = {"arbitrary_types_allowed": True}

    repo_root: Path = Field(frozen=True, description="Where the app registry and the ``mngr`` shim resolve from")
    send: SendMessage = Field(
        default=send_through_chat_app, frozen=True, description="The send, answering the messenger's exit code"
    )
    timeout_seconds: float = Field(
        default=MESSAGE_TIMEOUT_SECONDS, frozen=True, description="How long a caller waits for the send's answer"
    )
    executor: concurrent.futures.ThreadPoolExecutor = Field(
        default_factory=lambda: concurrent.futures.ThreadPoolExecutor(thread_name_prefix="screen-wake"),
        frozen=True,
        description="The sends' own threads, so a slow one never holds the caller",
    )

    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        future = self.executor.submit(self.send, agent_id, text, self.repo_root)
        try:
            returncode = future.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "The chat messenger has not answered in {}s messaging agent {} about the Screen; the message may not have landed",
                self.timeout_seconds,
                agent_name or agent_id,
            )
            return
        except OSError as e:
            logger.warning("Could not message agent {} about the Screen ({})", agent_name or agent_id, e)
            return
        if returncode != message_chat.EXIT_DELIVERED:
            logger.warning(
                "the chat messenger answered {} messaging agent {} about the Screen; the message may not have landed",
                returncode,
                agent_name or agent_id,
            )
