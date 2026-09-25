"""Showing a chat the Mind app asked for, when the user opens the chat's notification.

The Mind app sends the workspace ``minds:focus-chat`` with the chat's id; the shell posts it here, because this
app's manifest registers the type (``[[message_handlers]]``), with the client whose page received it. The shell
chooses the window itself: this app only says which of its paths show the chat and which of its windows may be
pointed at it, through one ``show`` op (desktop-interface contracts.md section 8). The chat root with the chat
selected is where the chat lands; the chat's own page counts as already showing it, a subagent view of it does
not, and a chat root window on screen on another chat is moved to this one.
"""

from typing import Final

from flask import Flask
from flask import Response
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.chat.auto_open import CHAT_ROOT_PAGE
from imbue.chat.auto_open import chat_root_path
from imbue.chat.models import ErrorResponse
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.chat.request_helpers import json_response
from imbue.chat.request_helpers import parse_request_body
from imbue.chat.shell_client import ShellOpError
from imbue.chat.shell_client import ShowRequest
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

    # The relay adds nothing a newer Mind app could not, so an unknown field is ignored rather than refused.
    model_config = ConfigDict(frozen=True, extra="ignore")

    client_id: NonEmptyStr = Field(description="The client whose page received the message")
    chat_id: str = Field(alias="chatId", description="The chat to show")


def chat_page_path(chat_id: ChatId) -> str:
    """The chat's own page: one chat, no list beside it."""
    return f"/{chat_id}"


@pure
def show_chat_request(chat_id: ChatId, client_id: str) -> ShowRequest:
    """The ``show`` that puts the chat on the client's screen: the chat root with the chat selected, the chat's own
    page counting as already showing it, and a chat root window on another chat moved to it."""
    return ShowRequest(
        path=chat_root_path(chat_id),
        showing=(chat_page_path(chat_id),),
        repoint=(CHAT_ROOT_PAGE,),
        client_id=client_id,
    )


def _error(detail: str, status_code: int) -> Response:
    return json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def focus_chat_endpoint() -> Response:
    """``POST /api/focus-chat``: ask the shell to show the chat to the client. Answers the shell's ``shown`` and
    window; 400 for a chat id of the wrong shape, 403 in a secondary chat (it opens no windows), and 502 when the
    shell could not be reached, refused, or answered something that is not a show's answer."""
    focus_request = parse_request_body(FocusChatRequest)
    if not AGENT_ID_PATTERN.fullmatch(focus_request.chat_id):
        return _error(f"{focus_request.chat_id!r} is not a chat id", HTTP_BAD_REQUEST)
    state = get_state()
    if state.is_secondary:
        return _error("A secondary chat opens no windows", HTTP_FORBIDDEN)
    chat_id = ChatId(focus_request.chat_id)
    try:
        answer = state.shell.show(show_chat_request(chat_id, focus_request.client_id))
    except ShellOpError as e:
        logger.warning("Could not show chat {} to client {}: {}", chat_id, focus_request.client_id, e)
        return _error(str(e), HTTP_BAD_GATEWAY)
    logger.info("Showed chat {} to client {} ({})", chat_id, focus_request.client_id, answer.shown)
    return json_response({"shown": answer.shown, "window_id": answer.window_id})


def register_routes(application: Flask) -> None:
    """Wire ``POST /api/focus-chat`` onto the Flask application."""
    application.add_url_rule(FOCUS_CHAT_ROUTE, view_func=focus_chat_endpoint, methods=["POST"])
