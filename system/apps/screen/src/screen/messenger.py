"""Waking a queued agent: a message to its chat through the chat app (``system/scripts/message_chat.py``), the path
the browser fleet's wake-ups took."""

import subprocess
import sys
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from screen.interfaces import AgentMessengerInterface

MESSAGE_CHAT_SCRIPT: Final[Path] = Path("system") / "scripts" / "message_chat.py"
_MESSAGE_TIMEOUT_SECONDS: Final[float] = 60.0


class ChatAgentMessenger(AgentMessengerInterface):
    """Runs the chat messenger script from the repo root; the ``--browser-fleet`` flag renders the nudge as a
    collapsed chip in the transcript rather than a user bubble."""

    repo_root: Path = Field(frozen=True, description="Where the script and the ``mngr`` shim resolve from")

    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        try:
            completed = subprocess.run(
                [sys.executable, str(MESSAGE_CHAT_SCRIPT), agent_id, "--browser-fleet", "--message", text],
                cwd=self.repo_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_MESSAGE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Could not message agent {} about the Screen ({})", agent_name or agent_id, e)
            return
        if completed.returncode != 0:
            logger.warning(
                "message_chat.py exited {} messaging agent {} about the Screen; the message may not have landed",
                completed.returncode,
                agent_name or agent_id,
            )
