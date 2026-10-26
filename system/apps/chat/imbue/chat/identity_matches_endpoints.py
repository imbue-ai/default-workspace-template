"""HTTP endpoints for `/api/identity-matches`: filing a look-up's candidates, and answering the popup.

The onboarding agent's look-up script files through the POST; the chat page's "Is this you?" popup
confirms a candidate or rejects them all through the two verb routes. Every change is broadcast to the
chat pages (``identity_match_updated``), and a page that connects later is sent the pending ones, so the
popup appears whether or not the chat was open when the look-up finished.

The user's answer reaches the agent as an ordinary message in the user's words, sent through the same
router bridge the secret-request routes use (``SecretRequestChatBridge``): whether a chat id names a chat,
and how to put a message into that chat.
"""

from __future__ import annotations

from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.identity_matches import IdentityCandidate
from imbue.chat.identity_matches import IdentityMatch
from imbue.chat.identity_matches import IdentityMatchNotPendingError
from imbue.chat.identity_matches import InvalidIdentityMatchError
from imbue.chat.identity_matches import REJECTION_MESSAGE
from imbue.chat.identity_matches import UnknownIdentityMatchError
from imbue.chat.identity_matches import confirmation_message
from imbue.chat.secret_requests import ChatLookup
from imbue.chat.secret_requests import NoticeDeliveryError
from imbue.chat.secret_requests import SecretRequestChatBridge
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger


class FileIdentityMatchBody(FrozenModel):
    """Request body for POST /api/identity-matches: who a look-up found for the user."""

    chat_id: str = Field(min_length=1, description="The chat whose agent ran the look-up")
    searched_for: str = Field(description="What was searched for, in the user's words")
    candidates: tuple[IdentityCandidate, ...] = Field(description="Who the look-up found, best match first")


class ConfirmIdentityMatchBody(FrozenModel):
    """Request body for POST /api/identity-matches/{match_id}/confirm."""

    candidate_index: int = Field(ge=0, description="The candidate the user said is them, by position")


class IdentityMatchResponse(IdentityMatch):
    """A match as the routes answer it: its fields, and whether the user's answer reached the chat."""

    is_reply_delivered: bool | None = Field(
        default=None, description="Whether the answer reached the chat; null on a filing or a read, which send none"
    )


class IdentityMatchBridgeMissingError(RuntimeError):
    """Raised when a route runs on an app whose router never attached its chat bridge."""


def _bridge() -> SecretRequestChatBridge:
    bridge = get_state().secret_request_bridge
    if bridge is None:
        raise IdentityMatchBridgeMissingError("The identity-match routes have no chat bridge attached")
    return bridge


def _error_response(detail: str, status_code: int = 400) -> Response:
    logger.warning("Returning identity-match error response ({}): {}", status_code, detail)
    return error_response(detail, status_code)


def _refusal_on_a_secondary_chat() -> Response | None:
    """A 409 when this app is a secondary chat (a preview), which answers no popup for the live agent."""
    if not get_state().is_secondary:
        return None
    return _error_response(
        "This is a preview of the chat, which cannot answer a look-up; answer it in the live chat.", 409
    )


def _match_response(match: IdentityMatch, is_reply_delivered: bool | None, status_code: int = 200) -> Response:
    return model_response(
        IdentityMatchResponse(**match.model_dump(), is_reply_delivered=is_reply_delivered), status_code
    )


def _broadcast(match: IdentityMatch) -> None:
    get_state().agent_manager.broadcaster.broadcast_identity_match_updated(match)


def _deliver(match: IdentityMatch, text: str) -> bool:
    """Put the user's answer into the match's chat; False (and a warning) when the chat could not take it."""
    try:
        _bridge().deliver_notice(match.chat_id, text)
    except NoticeDeliveryError as e:
        logger.warning("The answer to identity match {} did not reach chat {}: {}", match.match_id, match.chat_id, e)
        return False
    return True


def file_match() -> Response:
    """File a look-up's candidates (``searched_for``, ``candidates``) for a chat, shown to the person as a popup."""
    body = parse_request_body(FileIdentityMatchBody)
    match _bridge().lookup_chat(body.chat_id):
        case ChatLookup.NOT_READY:
            return _error_response("The chat app has not read its agent list from mngr yet; try again shortly.", 503)
        case ChatLookup.UNKNOWN:
            return _error_response(f"Chat '{body.chat_id}' not found", 404)
        case ChatLookup.KNOWN:
            pass
    try:
        filed = get_state().identity_matches.file_match(body.chat_id, body.searched_for, body.candidates)
    except InvalidIdentityMatchError as e:
        return _error_response(str(e))
    for superseded in filed.superseded:
        _broadcast(superseded)
    _broadcast(filed.match)
    return _match_response(filed.match, None, 201)


def get_match(match_id: str) -> Response:
    """One identity match as filed: pending, confirmed, rejected, or superseded."""
    match = get_state().identity_matches.get(match_id)
    if match is None:
        return _error_response(f"No identity match with id {match_id!r}", 404)
    return _match_response(match, None)


def confirm(match_id: str) -> Response:
    """Answer a look-up with the candidate the person says is them (``candidate_index``)."""
    refusal = _refusal_on_a_secondary_chat()
    if refusal is not None:
        return refusal
    body = parse_request_body(ConfirmIdentityMatchBody)
    try:
        confirmed = get_state().identity_matches.confirm(match_id, body.candidate_index)
    except UnknownIdentityMatchError as e:
        return _error_response(str(e), 404)
    except IdentityMatchNotPendingError as e:
        return _error_response(str(e), 409)
    except InvalidIdentityMatchError as e:
        return _error_response(str(e))
    _broadcast(confirmed)
    return _match_response(confirmed, _deliver(confirmed, confirmation_message(confirmed)))


def reject(match_id: str) -> Response:
    """Answer a look-up with "none of these are me"."""
    refusal = _refusal_on_a_secondary_chat()
    if refusal is not None:
        return refusal
    try:
        rejected = get_state().identity_matches.reject(match_id)
    except UnknownIdentityMatchError as e:
        return _error_response(str(e), 404)
    except IdentityMatchNotPendingError as e:
        return _error_response(str(e), 409)
    _broadcast(rejected)
    return _match_response(rejected, _deliver(rejected, REJECTION_MESSAGE))


ROUTE_TABLE: Final[RouteTable] = {
    ("POST", "/api/identity-matches"): RouteModels(
        request_model=FileIdentityMatchBody, response_model=IdentityMatchResponse, success_status=201
    ),
    ("GET", "/api/identity-matches/{match_id}"): RouteModels(response_model=IdentityMatchResponse),
    ("POST", "/api/identity-matches/{match_id}/confirm"): RouteModels(
        request_model=ConfirmIdentityMatchBody, response_model=IdentityMatchResponse
    ),
    ("POST", "/api/identity-matches/{match_id}/reject"): RouteModels(response_model=IdentityMatchResponse),
}


def register_routes(application: Flask) -> None:
    """Wire `/api/identity-matches` onto the Flask application; the router attaches its chat bridge first."""
    application.add_url_rule("/api/identity-matches", view_func=file_match, methods=["POST"])
    application.add_url_rule("/api/identity-matches/<match_id>", view_func=get_match, methods=["GET"])
    application.add_url_rule("/api/identity-matches/<match_id>/confirm", view_func=confirm, methods=["POST"])
    application.add_url_rule("/api/identity-matches/<match_id>/reject", view_func=reject, methods=["POST"])
