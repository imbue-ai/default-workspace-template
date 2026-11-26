"""Pointing a person at the chat list's New chat button, when another app's page asks for it.

The onboarding checklist's "Work in parallel by starting a new chat" entry sends the workspace
``onboarding:show-new-chat-pointer``; the shell posts it here, because this app's manifest registers the type
(``[[message_handlers]]``), with the client whose page sent it. The button is the chat root's, so only the chat root
can point at it: this app asks the shell, through one ``show`` op, to put the chat root on that client's screen at a
path that tells the root to draw its coach mark (``open=new-chat-pointer``, which the root reads and then drops from
the path it reports). The shell chooses the window: a chat root window on screen is pointed at the path and comes
forward, whatever chat it shows, and the root keeps that chat; with none on screen the pinned window comes up.
"""

from typing import Final

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

from imbue.app_sdk.api_schema import RouteModels
from imbue.app_sdk.api_schema import RouteTable
from imbue.app_sdk.routes import error_response
from imbue.app_sdk.routes import model_response
from imbue.app_sdk.routes import parse_request_body
from imbue.chat.auto_open import CHAT_ROOT_PAGE
from imbue.chat.shell_client import chat_show_args
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure

# The route the manifest's ``onboarding:show-new-chat-pointer`` handler names.
NEW_CHAT_POINTER_ROUTE: Final[str] = "/api/show-new-chat-pointer"

# The chat root's path that asks it to draw the coach mark at its New chat button (``root/selection.ts``).
NEW_CHAT_POINTER_PATH: Final[WindowPath] = WindowPath(f"{CHAT_ROOT_PAGE}?open=new-chat-pointer")

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_BAD_GATEWAY: Final[int] = 502


class NewChatPointerRequest(FrozenModel):
    """What the shell posts for ``onboarding:show-new-chat-pointer``: the client whose page sent the message."""

    # The message carries no fields of its own today, so one a newer sender adds is ignored rather than refused.
    model_config = ConfigDict(frozen=True, extra="ignore")

    client_id: NonEmptyStr = Field(description="The client whose page sent the message")


class NewChatPointerResponse(FrozenModel):
    """The shell's answer to the show: how it put the chat root on screen, and in which window."""

    shown: ShowOutcome = Field(description="How the shell showed the chat root")
    window_id: str = Field(description="The window the chat root is shown in")


@pure
def show_new_chat_pointer_args(client_id: ClientId) -> ShowArgs:
    """The ``show`` that puts the chat root on the client's screen asking for the pointer: a chat root window on
    screen is pointed at the asking path, and no other page of the app counts as already showing it."""
    return chat_show_args(NEW_CHAT_POINTER_PATH, showing=(), repoint=(CHAT_ROOT_PAGE,), client_id=client_id)


def show_new_chat_pointer_endpoint() -> Response:
    """``POST /api/show-new-chat-pointer``: ask the shell to bring the chat root forward for the client, asking it
    to point at its New chat button. Answers the shell's ``shown`` and window; 400 for a client id of the wrong
    shape, 403 in a secondary chat (it opens no windows), and 502 when the shell could not be reached, refused, or
    answered something that is not a show's answer."""
    pointer_request = parse_request_body(NewChatPointerRequest)
    state = get_state()
    if state.is_secondary:
        return error_response("A secondary chat opens no windows", HTTP_FORBIDDEN)
    try:
        client_id = ClientId(pointer_request.client_id)
    except InvalidLayoutValueError as e:
        return error_response(str(e), HTTP_BAD_REQUEST)
    try:
        answer = state.shell.show(show_new_chat_pointer_args(client_id))
    except ShellOpError as e:
        logger.warning("Could not show the New chat pointer to client {}: {}", pointer_request.client_id, e)
        return error_response(str(e), HTTP_BAD_GATEWAY)
    logger.info("Showed the New chat pointer to client {} ({})", pointer_request.client_id, answer.shown)
    return model_response(NewChatPointerResponse(shown=answer.shown, window_id=str(answer.window_id)))


ROUTE_TABLE: Final[RouteTable] = {
    ("POST", NEW_CHAT_POINTER_ROUTE): RouteModels(
        request_model=NewChatPointerRequest, response_model=NewChatPointerResponse
    ),
}


def register_routes(application: Flask) -> None:
    """Wire ``POST /api/show-new-chat-pointer`` onto the Flask application."""
    application.add_url_rule(NEW_CHAT_POINTER_ROUTE, view_func=show_new_chat_pointer_endpoint, methods=["POST"])
