"""The mention routes (the multiplayer spec, sections 7.1 and 7.4): the handle table a room's composer offers,
and the mentions the requester has not looked at."""

from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import model_response
from flask import Flask
from flask import Response
from workspace_layout.primitives import ParticipantKey

from imbue.chat.identity import request_identity
from imbue.chat.mention_parser import MentionTable
from imbue.chat.mentions import MentionHandler
from imbue.chat.mentions import PendingMentionsResponse
from imbue.chat.mentions import mention_keys_of
from imbue.chat.state import ChatAppStateError
from imbue.chat.state import get_state

MENTIONS_ROUTE: Final[str] = "/api/mentions"
PENDING_MENTIONS_ROUTE: Final[str] = "/api/mentions/pending"


def mention_handler() -> MentionHandler:
    handler = get_state().mention_handler
    if handler is None:
        raise ChatAppStateError("The mention handler is wired by create_application")
    return handler


def _mentions_table() -> Response:
    """``GET /api/mentions``: the handle table, people first, then agents, then chats by title."""
    return model_response(MentionTable(handles=mention_handler().table()))


def requester_mention_keys() -> tuple[ParticipantKey, ...]:
    """The keys the current request's sender's mentions are kept under (both of the owner's for the owner)."""
    state = get_state()
    return mention_keys_of(request_identity(), state.roster.document())


def _pending_mentions() -> Response:
    """``GET /api/mentions/pending``: the requester's mentions they have not looked at, oldest first."""
    return model_response(PendingMentionsResponse(mentions=get_state().mentions.pending_for(requester_mention_keys())))


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", MENTIONS_ROUTE): RouteModels(response_model=MentionTable),
    ("GET", PENDING_MENTIONS_ROUTE): RouteModels(response_model=PendingMentionsResponse),
}


def register_routes(application: Flask) -> None:
    application.add_url_rule(MENTIONS_ROUTE, view_func=_mentions_table, methods=["GET"])
    application.add_url_rule(PENDING_MENTIONS_ROUTE, view_func=_pending_mentions, methods=["GET"])
