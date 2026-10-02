"""The workspace's chats, and stopping or starting one, through the chat app's own HTTP API.

The chat app owns what a chat is called, which harness runs it, and the stop and start that keep its own state
consistent (it refuses the primary services agent and a chat mid-handoff), so this app asks it rather than calling
``mngr`` itself. A chat id only ever reaches the chat app for a chat its own list holds, path-quoted.
"""

import json
import urllib.parse
from collections.abc import Sequence
from enum import auto
from typing import Any
from typing import Final

import httpx
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from activity.errors import ChatAppUnavailableError
from app_manifest.primitives import AppName
from app_manifest.registry import RegistryRow
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

CHAT_APP_NAME: Final[AppName] = AppName("chat")
DEFAULT_CHAT_APP_URL: Final[str] = "http://127.0.0.1:8010"
CHAT_LIST_ROUTE: Final[str] = "/api/chats"
CHAT_LIST_TIMEOUT_SECONDS: Final[float] = 5.0
# A stop or start runs ``mngr`` in the chat app, which can take a while for a chat with many processes.
CHAT_ACTION_TIMEOUT_SECONDS: Final[float] = 60.0


class ChatAction(UpperCaseStrEnum):
    """What the page can ask the chat app to do with a chat."""

    STOP = auto()
    START = auto()


class ChatInfo(FrozenModel):
    """One chat, as much of the chat app's snapshot as this app uses."""

    chat_id: str = Field(description="The chat's id")
    title: str = Field(description="The name the user sees")
    status: str = Field(description="The chat app's status: working, idle, attention, stopped, or error")
    harness: str = Field(description="Which harness runs it, as the chat app names it")
    agent_id: str = Field(description="The mngr id of the agent the chat currently runs on")
    agent_ids: tuple[str, ...] = Field(description="Every agent the chat has run on, the active one last")
    last_messaged_at: float | None = Field(description="Epoch seconds of its latest message, or None")


class _WireActiveAgent(FrozenModel):
    """The chat app's ``active_agent`` object, read leniently: a field a newer chat app adds is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    agent_id: str = Field(description="The active agent's mngr id")
    harness: str = Field(description="The harness it runs")


class _WireChat(FrozenModel):
    """One ``ChatSnapshot`` of the chat app's list, read leniently."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    chat_id: str = Field(description="The chat's id")
    title: str = Field(description="Its display title")
    status: str = Field(description="Its status")
    agent_ids: tuple[str, ...] = Field(description="Every agent of the chat, the active one last")
    last_messaged_at: float | None = Field(description="Epoch seconds of its latest message")
    active_agent: _WireActiveAgent = Field(description="The agent it runs on")


class _WireChatList(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    chats: tuple[_WireChat, ...] = Field(description="Every chat")


@pure
def chat_app_url(registry_rows: Sequence[RegistryRow]) -> str:
    """The chat app's in-workspace URL from the app registry, else its default port."""
    for row in registry_rows:
        if row.name == CHAT_APP_NAME:
            return str(row.url).rstrip("/")
    return DEFAULT_CHAT_APP_URL


@pure
def parse_chat_list(body: Any) -> list[ChatInfo]:
    """The chats in a ``GET /api/chats`` body; raises ChatAppUnavailableError for a body of another shape."""
    try:
        chat_list = _WireChatList.model_validate(body)
    except ValidationError as e:
        raise ChatAppUnavailableError(f"the chat app's chat list is not in the expected shape: {e}") from e
    return [
        ChatInfo(
            chat_id=chat.chat_id,
            title=chat.title,
            status=chat.status,
            harness=chat.active_agent.harness,
            agent_id=chat.active_agent.agent_id,
            agent_ids=tuple(dict.fromkeys((*chat.agent_ids, chat.active_agent.agent_id))),
            last_messaged_at=chat.last_messaged_at,
        )
        for chat in chat_list.chats
    ]


def fetch_chats(client: httpx.Client, base_url: str) -> list[ChatInfo]:
    try:
        response = client.get(f"{base_url}{CHAT_LIST_ROUTE}", timeout=CHAT_LIST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ChatAppUnavailableError(f"could not list chats from {base_url}: {e}") from e
    return parse_chat_list(body)


@pure
def chat_action_route(chat_id: str, action: ChatAction) -> str:
    return f"{CHAT_LIST_ROUTE}/{urllib.parse.quote(chat_id, safe='')}/{action.value.lower()}"


@pure
def refusal_reason(response_text: str) -> str:
    """The chat app's ``detail`` from an error body, else the body itself."""
    try:
        body = json.loads(response_text)
    except ValueError:
        return response_text
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail is not None else response_text


def request_chat_action(client: httpx.Client, base_url: str, chat_id: str, action: ChatAction) -> None:
    """Ask the chat app to stop or start a chat; raises ChatAppUnavailableError with the chat app's own reason."""
    url = f"{base_url}{chat_action_route(chat_id, action)}"
    try:
        response = client.post(url, timeout=CHAT_ACTION_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise ChatAppUnavailableError(f"could not reach the chat app at {url}: {e}") from e
    if response.is_error:
        raise ChatAppUnavailableError(
            f"the chat app refused to {action.value.lower()} the chat: {refusal_reason(response.text)}"
        )
