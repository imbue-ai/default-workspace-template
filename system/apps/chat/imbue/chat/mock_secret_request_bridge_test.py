"""A recording :class:`SecretRequestChatBridge`: a fixed set of known chats, and every notice kept."""

from pydantic import Field

from imbue.chat.secret_requests import ChatLookup
from imbue.chat.secret_requests import NoticeDeliveryError
from imbue.chat.secret_requests import SecretRequestChatBridge


class RecordingSecretRequestBridge(SecretRequestChatBridge):
    """The router's two capabilities as a test double."""

    known_chat_ids: frozenset[str] = Field(default=frozenset(), description="The chat ids that resolve")
    is_ready: bool = Field(default=True, description="False answers every lookup with NOT_READY")
    is_delivery_failing: bool = Field(default=False, description="True makes every delivery raise")
    delivered: list[tuple[str, str]] = Field(default_factory=list, description="Every (chat_id, notice) delivered")

    def lookup_chat(self, chat_id: str) -> ChatLookup:
        if not self.is_ready:
            return ChatLookup.NOT_READY
        return ChatLookup.KNOWN if chat_id in self.known_chat_ids else ChatLookup.UNKNOWN

    def deliver_notice(self, chat_id: str, text: str) -> None:
        if self.is_delivery_failing:
            raise NoticeDeliveryError("the agent is away")
        self.delivered.append((chat_id, text))
