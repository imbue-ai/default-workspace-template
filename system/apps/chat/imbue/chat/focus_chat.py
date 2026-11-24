"""Showing a chat the Imbue Studio app asked for, when the user opens the chat's notification, or the chat list.

The Imbue Studio app sends the workspace ``minds:focus-chat`` with the chat's id; the shell posts it here, because this
app's manifest registers the type (``[[message_handlers]]``), with the client whose page received it. The shell
chooses the window itself: this app only says which of its paths show the chat and which of its windows may be
pointed at it, through one ``show`` op (desktop-interface contracts.md section 8). The chat root with the chat
selected is where the chat lands; the chat's own page counts as already showing it, a subagent view of it does
not, and a chat root window on screen on another chat is moved to this one.

With no ``chatId`` the ask is for the chat root itself, as it stands: the Imbue Studio app sends it as it enters a
new workspace, so the chat list (and the provider chooser it opens while nothing is signed in) is on screen before
any chat exists. A chat root window on any chat counts as already showing it, so the ask never moves the chat a
window shows (the welcome chat may have been put there first); with none, the pinned window comes up at the root.
"""

from collections.abc import Iterable
from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.errors import ShellOpError
from workspace_layout.ops import ShowArgs
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import WindowPath

from imbue.chat.auto_open import CHAT_ROOT_PAGE
from imbue.chat.auto_open import chat_root_path
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.chat.shell_client import chat_show_args
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

# The route the manifest's ``minds:focus-chat`` handler names.
FOCUS_CHAT_ROUTE: Final[str] = "/api/focus-chat"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_BAD_GATEWAY: Final[int] = 502


class FocusChatRequest(FrozenModel):
    """What the shell posts for ``minds:focus-chat``: the message's fields and the client whose page received it."""

    # The relay adds nothing a newer Imbue Studio app could not, so an unknown field is ignored rather than refused.
    model_config = ConfigDict(frozen=True, extra="ignore")

    client_id: NonEmptyStr = Field(description="The client whose page received the message")
    chat_id: str | None = Field(default=None, alias="chatId", description="The chat to show; None for the chat root")


class FocusChatResponse(FrozenModel):
    """The shell's answer to the show: how it put the chat on screen, and in which window."""

    shown: ShowOutcome = Field(description="How the shell showed the chat, or the chat root")
    window_id: str = Field(description="The window the chat, or the chat root, is shown in")


def chat_page_path(chat_id: ChatId) -> WindowPath:
    """The chat's own page: one chat, no list beside it."""
    return WindowPath(f"/{chat_id}")


@pure
def show_chat_args(chat_id: ChatId, client_id: ClientId) -> ShowArgs:
    """The ``show`` that puts the chat on the client's screen: the chat root with the chat selected, the chat's own
    page counting as already showing it, and a chat root window on another chat moved to it."""
    return chat_show_args(
        chat_root_path(chat_id), showing=(chat_page_path(chat_id),), repoint=(CHAT_ROOT_PAGE,), client_id=client_id
    )


@pure
def show_chat_root_args(known_chat_ids: Iterable[ChatId], client_id: ClientId) -> ShowArgs:
    """The ``show`` that puts the chat root on the client's screen as it stands: a chat root window on any chat the
    app knows counts as already showing it, and no window is moved, so with none at the root or on a chat the pinned
    window comes up at the root."""
    return chat_show_args(
        WindowPath(CHAT_ROOT_PAGE),
        showing=tuple(chat_root_path(chat_id) for chat_id in sorted(set(known_chat_ids))),
        repoint=(),
        client_id=client_id,
    )


def focus_chat_endpoint() -> Response:
    """``POST /api/focus-chat``: ask the shell to show the chat, or with no chat the chat root, to the client. Answers
    the shell's ``shown`` and window; 400 for a chat or client id of the wrong shape, 403 in a secondary chat (it
    opens no windows), and 502 when the shell could not be reached, refused, or answered something that is not a
    show's answer."""
    focus_request = parse_request_body(FocusChatRequest)
    if focus_request.chat_id is not None and not AGENT_ID_PATTERN.fullmatch(focus_request.chat_id):
        return error_response(f"{focus_request.chat_id!r} is not a chat id", HTTP_BAD_REQUEST)
    state = get_state()
    if state.is_secondary:
        return error_response("A secondary chat opens no windows", HTTP_FORBIDDEN)
    try:
        client_id = ClientId(focus_request.client_id)
    except InvalidLayoutValueError as e:
        return error_response(str(e), HTTP_BAD_REQUEST)
    if focus_request.chat_id is None:
        shown_what = "the chat list"
        show_args = show_chat_root_args(state.agent_manager.get_shown_chat_ids(), client_id)
    else:
        shown_what = f"chat {focus_request.chat_id}"
        show_args = show_chat_args(ChatId(focus_request.chat_id), client_id)
    try:
        answer = state.shell.show(show_args)
    except ShellOpError as e:
        logger.warning("Could not show {} to client {}: {}", shown_what, focus_request.client_id, e)
        return error_response(str(e), HTTP_BAD_GATEWAY)
    logger.info("Showed {} to client {} ({})", shown_what, focus_request.client_id, answer.shown)
    return model_response(FocusChatResponse(shown=answer.shown, window_id=str(answer.window_id)))


ROUTE_TABLE: Final[RouteTable] = {
    ("POST", FOCUS_CHAT_ROUTE): RouteModels(request_model=FocusChatRequest, response_model=FocusChatResponse),
}


def register_routes(application: Flask) -> None:
    """Wire ``POST /api/focus-chat`` onto the Flask application."""
    application.add_url_rule(FOCUS_CHAT_ROUTE, view_func=focus_chat_endpoint, methods=["POST"])
