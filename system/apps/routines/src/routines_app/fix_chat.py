"""Opening the Fix chat: the brief goes to the chat app's create route through the workspace's chat script."""

import json
import subprocess
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from routines_app.errors import FixChatError

MESSAGE_CHAT_SCRIPT: Final[Path] = Path("system/scripts/message_chat.py")
# The create makes an agent and waits on the chat app; a couple of minutes is the ceiling.
CREATE_TIMEOUT_SECONDS: Final[float] = 120.0


class FixChatStarterInterface(MutableModel, ABC):
    """What the app's fix route calls: the brief and the chat's name in, the chat's id out."""

    @abstractmethod
    def start(self, brief: str, name: str) -> str:
        """Open the chat on the brief and answer its id; raises FixChatError when it could not be opened."""


class SubprocessFixChatStarter(FixChatStarterInterface):
    """The starter the served app uses: the workspace's chat script, run from the repo root."""

    repo_root: Path = Field(frozen=True, description="The checkout the chat script is run from")

    def start(self, brief: str, name: str) -> str:
        argv = [
            "python3",
            str(self.repo_root / MESSAGE_CHAT_SCRIPT),
            "--create",
            "--name",
            name,
            "--label",
            "auto_open=true",
            "-m",
            brief,
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=CREATE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise FixChatError(f"The chat could not be started ({e}).") from e
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            logger.warning("Starting a fix chat failed (rc={}): {}", completed.returncode, detail)
            raise FixChatError(detail or "The chat could not be started.")
        chat_id = chat_id_from_output(completed.stdout)
        if chat_id is None:
            logger.warning("The fix chat create printed no chat id: {}", completed.stdout.strip()[:200])
            raise FixChatError("The chat was started but its id could not be read.")
        return chat_id


def chat_id_from_output(stdout: str) -> str | None:
    """The chat id from the create's JSON line (the script always prints one on success); None when it is missing."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            logger.trace("Skipping a line of the create's output that is not JSON: {}", line)
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("chat_id"), str) and parsed["chat_id"]:
            return parsed["chat_id"]
    return None
