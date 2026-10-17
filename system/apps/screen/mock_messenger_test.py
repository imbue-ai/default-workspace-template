from collections.abc import Sequence

from pydantic import Field

from screen.interfaces import AgentMessengerInterface
from screen.interfaces import ProcessLauncherInterface


class RecordingMessenger(AgentMessengerInterface):
    """Keeps every message instead of delivering it."""

    messages: list[tuple[str, str]] = Field(default_factory=list, description="(agent id, text) in order")

    def message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        self.messages.append((agent_id, text))


class RecordingLauncher(ProcessLauncherInterface):
    """Answers a made-up pid and keeps the commands."""

    commands: list[tuple[str, ...]] = Field(default_factory=list, description="Every command, in order")

    def launch(self, command: Sequence[str]) -> int:
        self.commands.append(tuple(command))
        return 4242 + len(self.commands)
