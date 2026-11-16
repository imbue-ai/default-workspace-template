"""What the chat app tells the shell beyond a client's activity (the multiplayer spec, sections 6.4 and 6.5): every
message somebody sends and every notification a chat sends the user, for the taskbar's tray and its speech bubbles,
and what each chat is doing, which the tray's agent card lists. Both are best-effort loopback posts, as the
client-activity report is: the shell is a separate app, and a send or a chat-list push never waits on it."""

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from pydantic import Field

from workspace_layout.primitives import BubbleKind
from workspace_layout.primitives import ChatActivityKind
from workspace_layout.primitives import ChatActivityStatus
from workspace_layout.primitives import ParticipantKey


class BubblePost(FrozenModel):
    """The body of ``POST /api/bubbles``: one message somebody just said, or a chat's notification to the user, for
    the tray entry of whoever said it."""

    speaker: ParticipantKey = Field(description="Who said it: a person, a visiting agent, or the workspace agent")
    chat_id: NonEmptyStr = Field(description="The chat it was said in; the bubble's click opens it")
    chat_title: str = Field(description="What that chat is called")
    text: str = Field(description="What was said, as the bubble shows it (the sender cuts it to the bubble's length)")
    message_id: str = Field(description="The message's id, so a page can tell one message's bubble from the next's")
    mentions: tuple[ParticipantKey, ...] = Field(
        default=(), description="The people the message mentions, whose clients raise the bubble as a mention"
    )
    kind: BubbleKind = Field(
        default=BubbleKind.MESSAGE, description="A message somebody sent, or a chat's notification to the user"
    )


class ChatActivity(FrozenModel):
    """One chat as the tray's agent card lists it: what it is called and what it is doing."""

    chat_id: NonEmptyStr = Field(description="The chat")
    title: str = Field(description="What the chat is called")
    status: ChatActivityStatus = Field(description="What the chat is doing")
    kind: ChatActivityKind = Field(description="A chat with an agent, or a room")


class ChatsActivityReport(FrozenModel):
    """The body of ``POST /api/chats-activity``: every chat, posted whenever the chat list changes."""

    chats: tuple[ChatActivity, ...] = Field(description="Every chat the chat app lists, in its order")
