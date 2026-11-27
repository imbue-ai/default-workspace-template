"""A recording :class:`CloudConnectionsChatBridge`: a fixed set of chats, every restart and every send kept."""

from app_schema.routes import error_response
from flask import Response
from pydantic import Field

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.cloud_connections_bridge import ChatSendRefusedError
from imbue.chat.cloud_connections_bridge import CloudConnectionsChatBridge
from imbue.chat.models import AgentRestartError


class RecordingCloudConnectionsBridge(CloudConnectionsChatBridge):
    """The router's capabilities as a test double."""

    agents_by_chat_id: dict[str, AgentInfo] = Field(default_factory=dict, description="The chats that resolve")
    is_restart_refused: bool = Field(default=False, description="True refuses every restart with a 409")
    is_restart_failing: bool = Field(default=False, description="True makes every restart raise")
    is_send_failing: bool = Field(default=False, description="True makes every send raise")
    queued_block: str = Field(default="", description="What a restart finds queued for the agent")
    restarted: list[str] = Field(default_factory=list, description="Every chat id restarted")
    sent: list[tuple[str, str]] = Field(default_factory=list, description="Every (chat_id, text) sent")

    def find_agent(self, chat_id: str) -> AgentInfo | None:
        return self.agents_by_chat_id.get(chat_id)

    def refuse_restart(self, chat_id: str, agent_info: AgentInfo) -> Response | None:
        return error_response("the chat is switching", 409) if self.is_restart_refused else None

    def restart(self, chat_id: str, agent_info: AgentInfo) -> str:
        if self.is_restart_failing:
            raise AgentRestartError(f"Failed to restart agent '{agent_info.name}': it would not start")
        self.restarted.append(chat_id)
        return self.queued_block

    def send(self, chat_id: str, text: str) -> None:
        if self.is_send_failing:
            raise ChatSendRefusedError("the agent is away")
        self.sent.append((chat_id, text))
