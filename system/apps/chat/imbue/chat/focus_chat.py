"""Showing a chat the Mind app asked for, when the user opens the chat's notification.

The Mind app sends the workspace ``minds:focus-chat`` with the chat's id; the shell posts it here, because this
app's manifest registers the type (``[[message_handlers]]``), with the client whose page received it. The shell
chooses the window itself: this app only says which of its paths show the chat, through one ``show`` op
(desktop-interface contracts.md section 8). The chat root with the chat selected is where the chat lands; the
chat's own page counts as already showing it, and a subagent view of it does not.
"""

from typing import Any
from typing import Final

from flask import Flask
from flask import Response
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.chat.auto_open import chat_root_path
from imbue.chat.models import ErrorResponse
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import CHAT_APP_NAME
from imbue.chat.primitives import ChatId
from imbue.chat.request_helpers import json_response
from imbue.chat.request_helpers import parse_request_body
from imbue.chat.shell_client import ShellUnreachableError
from imbue.chat.shell_client import post_layout_op
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr

# The route the manifest's ``minds:focus-chat`` handler names.
FOCUS_CHAT_ROUTE: Final[str] = "/api/focus-chat"

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_BAD_GATEWAY: Final[int] = 502

# How much of the shell's refusal the answer quotes.
_REFUSAL_DETAIL_LIMIT: Final[int] = 300


class FocusChatRequest(FrozenModel):
    """What the shell posts for ``minds:focus-chat``: the message's fields and the client whose page received it."""

    # The relay adds nothing a newer Mind app could not, so an unknown field is ignored rather than refused.
    model_config = ConfigDict(frozen=True, extra="ignore")

    client_id: NonEmptyStr = Field(description="The client whose page received the message")
    chat_id: str = Field(alias="chatId", description="The chat to show")


def chat_page_path(chat_id: ChatId) -> str:
    """The chat's own page: one chat, no list beside it."""
    return f"/{chat_id}"


def show_chat_op_body(chat_id: ChatId, client_id: str) -> dict[str, Any]:
    """The ``show`` op that puts the chat on the client's screen: the chat root with the chat selected, with the
    chat's own page counting as already showing it."""
    return {
        "op": "show",
        "args": {
            "app": CHAT_APP_NAME,
            "path": chat_root_path(chat_id),
            "showing": [chat_page_path(chat_id)],
            "client": client_id,
        },
        "requester": {"app": CHAT_APP_NAME, "marker": ""},
    }


def _error(detail: str, status_code: int) -> Response:
    return json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def focus_chat_endpoint() -> Response:
    """``POST /api/focus-chat``: ask the shell to show the chat to the client. Answers the shell's ``shown`` and
    window; 400 for a chat id of the wrong shape, 403 in a secondary chat (it opens no windows), and 502 when the
    shell could not be reached or refused."""
    focus_request = parse_request_body(FocusChatRequest)
    if not AGENT_ID_PATTERN.fullmatch(focus_request.chat_id):
        return _error(f"{focus_request.chat_id!r} is not a chat id", HTTP_BAD_REQUEST)
    if get_state().is_secondary:
        return _error("A secondary chat opens no windows", HTTP_FORBIDDEN)
    chat_id = ChatId(focus_request.chat_id)
    try:
        answer = post_layout_op(show_chat_op_body(chat_id, focus_request.client_id))
    except ShellUnreachableError as e:
        logger.warning("Could not show chat {} to client {}: {}", chat_id, focus_request.client_id, e)
        return _error(str(e), HTTP_BAD_GATEWAY)
    if answer.is_error:
        detail = answer.text.strip()[:_REFUSAL_DETAIL_LIMIT]
        logger.warning(
            "The shell refused to show chat {} to client {} ({}): {}",
            chat_id,
            focus_request.client_id,
            answer.status_code,
            detail,
        )
        return _error(f"The shell refused to show the chat ({answer.status_code}): {detail}", HTTP_BAD_GATEWAY)
    shown = answer.json()
    logger.info("Showed chat {} to client {} ({})", chat_id, focus_request.client_id, shown.get("shown"))
    return json_response({"shown": shown.get("shown"), "window_id": shown.get("window_id")})


def register_routes(application: Flask) -> None:
    """Wire ``POST /api/focus-chat`` onto the Flask application."""
    application.add_url_rule(FOCUS_CHAT_ROUTE, view_func=focus_chat_endpoint, methods=["POST"])
