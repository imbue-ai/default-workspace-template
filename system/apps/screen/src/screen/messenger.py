"""Waking a queued agent: a message to its chat through the chat app (``message_chat``, the in-process send the
browser fleet's wake-ups took), which falls back to ``mngr message`` when the chat app cannot take it."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from workspace_bare_scripts import message_chat

from screen.interfaces import AgentMessengerInterface

# A send has no read timeout of its own (a chat app that never answers holds it), so it runs on a daemon thread
# of its own and the caller -- the lease's release and the stack's stop -- waits only this long for the answer;
# a send still hanging past that never holds the service's exit either.
MESSAGE_TIMEOUT_SECONDS: Final[float] = 60.0

SendMessage = Callable[[str, str, Path], int]


def send_through_chat_app(agent_id: str, text: str, repo_root: Path) -> int:
    return message_chat.send(agent_id, message_chat.wrap_browser_fleet_nudge(text), cwd=repo_root)


class ChatAgentMessenger(AgentMessengerInterface):
    """Sends through the chat messenger from the repo root; the browser fleet's sentinel renders the nudge as a
    collapsed chip in the transcript rather than a user bubble."""

    repo_root: Path = Field(frozen=True, description="Where the app registry and the ``mngr`` shim resolve from")
    send: SendMessage = Field(
        default=send_through_chat_app, frozen=True, description="The send, answering the messenger's exit code"
    )
    timeout_seconds: float = Field(
        default=MESSAGE_TIMEOUT_SECONDS, frozen=True, description="How long a caller waits for the send's answer"
    )

    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        answers: list[int | OSError] = []

        def run_send() -> None:
            try:
                answers.append(self.send(agent_id, text, self.repo_root))
            except OSError as e:
                answers.append(e)

        thread = threading.Thread(target=run_send, name="screen-wake", daemon=True)
        thread.start()
        thread.join(self.timeout_seconds)
        if thread.is_alive():
            logger.warning(
                "The chat messenger has not answered in {}s messaging agent {} about the Screen; the message may not have landed",
                self.timeout_seconds,
                agent_name or agent_id,
            )
            return
        if not answers:
            # The send died of something other than an OSError; the thread's excepthook already printed it.
            logger.warning(
                "The send to agent {} about the Screen failed; the message may not have landed", agent_name or agent_id
            )
            return
        returncode = answers[0]
        if isinstance(returncode, OSError):
            logger.warning("Could not message agent {} about the Screen ({})", agent_name or agent_id, returncode)
            return
        if returncode != message_chat.EXIT_DELIVERED:
            logger.warning(
                "the chat messenger answered {} messaging agent {} about the Screen; the message may not have landed",
                returncode,
                agent_name or agent_id,
            )
