"""HTTP endpoints for `/api/secret-requests`: filing a secret request, and answering it from the card.

The request script (``.agents/skills/connect-external-service/scripts/request_secret.py``)
files through the POST and prints what it gets back; the chat page's secret card submits
or declines through the two verb routes and hydrates itself through the GET after a
reload. The values a submit carries are handed to the store for the file write and
appear nowhere else: not in a log line, not in an error body, not in the notice the agent
receives.

Kept out of server.py like the other endpoint modules; the two things it needs from the
router -- whether a chat id names a chat, and how to put a notice into that chat's
transcript -- come through the ``SecretRequestChatBridge`` the router puts on the app
state, so the module never imports the router.
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

from imbue.chat.harnesses.message_display import SecretResolutionVerdict
from imbue.chat.harnesses.message_display import format_secret_resolution_notice
from imbue.chat.secret_requests import ChatLookup
from imbue.chat.secret_requests import FiledSecretRequest
from imbue.chat.secret_requests import FiledSecretRequestResponse
from imbue.chat.secret_requests import InvalidSecretRequestError
from imbue.chat.secret_requests import NoticeDeliveryError
from imbue.chat.secret_requests import SecretFileWriteError
from imbue.chat.secret_requests import SecretRequest
from imbue.chat.secret_requests import SecretRequestChatBridge
from imbue.chat.secret_requests import SecretRequestError
from imbue.chat.secret_requests import SecretRequestNotPendingError
from imbue.chat.secret_requests import SecretRequestResponse
from imbue.chat.secret_requests import SecretValuesMismatchError
from imbue.chat.secret_requests import UnknownSecretRequestError
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger


class FileSecretRequestBody(FrozenModel):
    """Request body for POST /api/secret-requests: what an agent asks the person for."""

    chat_id: str = Field(min_length=1, description="The chat whose agent is asking")
    file: str = Field(description="The <file> of data/.secrets/<file>.env the values go to")
    variables: tuple[str, ...] = Field(description="The variable names asked for, in order")
    rationale: str = Field(description="Why the agent needs them, shown on the card")


class SubmitSecretRequestBody(FrozenModel):
    """Request body for POST /api/secret-requests/{request_id}/submit."""

    values: dict[str, str] = Field(description="The values, by variable name; written to the file and nowhere else")


class DeclineSecretRequestBody(FrozenModel):
    """Request body for POST /api/secret-requests/{request_id}/decline."""

    note: str | None = Field(default=None, description="A note for the agent on why; None for none")


class SecretRequestBridgeMissingError(SecretRequestError, RuntimeError):
    """Raised when a route runs on an app whose router never attached its bridge."""


def _bridge() -> SecretRequestChatBridge:
    bridge = get_state().secret_request_bridge
    if bridge is None:
        raise SecretRequestBridgeMissingError("The secret-request routes have no chat bridge attached")
    return bridge


def _error_response(detail: str, status_code: int = 400) -> Response:
    logger.warning("Returning secret-request error response ({}): {}", status_code, detail)
    return error_response(detail, status_code)


def _refusal_on_a_secondary_chat() -> Response | None:
    """A 409 when this app is a secondary chat (a preview), which answers no secret card.

    A secondary runs from an editing worktree over a scratch copy of the requests, so a
    submit would write the file into the worktree's data/.secrets and tell the live agent a
    file it cannot read was stored.
    """
    if not get_state().is_secondary:
        return None
    return _error_response(
        "This is a preview of the chat, which cannot answer secret requests; answer it in the live chat.", 409
    )


def _request_response(request: SecretRequest, is_notice_delivered: bool | None) -> Response:
    return model_response(SecretRequestResponse.of(request, is_notice_delivered))


def _deliver(request: SecretRequest, verdict: SecretResolutionVerdict) -> bool:
    """Put the resolution notice into the request's chat; False (and a warning) when the chat could not take it."""
    notice = format_secret_resolution_notice(
        verdict, request.request_id, request.env_path, request.variables, request.note
    )
    try:
        _bridge().deliver_notice(request.chat_id, notice)
    except NoticeDeliveryError as e:
        logger.warning(
            "The {} notice for secret request {} did not reach chat {}: {}",
            verdict,
            request.request_id,
            request.chat_id,
            e,
        )
        return False
    return True


def _notify_superseded_in_other_chats(filed: FiledSecretRequest) -> None:
    """A superseded request in ANOTHER chat learns of it only through a notice; the same chat's transcript walk sees the newer request itself."""
    for superseded in filed.superseded:
        if superseded.chat_id != filed.request.chat_id:
            _deliver(superseded, SecretResolutionVerdict.SUPERSEDED)


def file_request() -> Response:
    """File a request for secret values (``file``, ``variables``, ``rationale``) from a chat, for the person to answer.

    An agent's ``request_secret.py`` posts it; the chat shows the person a card, and the filed request is answered.
    """
    body = parse_request_body(FileSecretRequestBody)
    match _bridge().lookup_chat(body.chat_id):
        case ChatLookup.NOT_READY:
            return _error_response("The chat app has not read its agent list from mngr yet; try again shortly.", 503)
        case ChatLookup.UNKNOWN:
            return _error_response(f"Chat '{body.chat_id}' not found", 404)
        case ChatLookup.KNOWN:
            pass
    try:
        filed = get_state().secret_requests.file_request(body.chat_id, body.file, body.variables, body.rationale)
    except InvalidSecretRequestError as e:
        return _error_response(str(e))
    except SecretFileWriteError as e:
        # Filing reads the file already there to report what a submit would replace.
        return _error_response(str(e), 500)
    _notify_superseded_in_other_chats(filed)
    return model_response(filed.as_response(), 201)


def get_request(request_id: str) -> Response:
    """One secret request as filed: pending, stored, declined, or superseded."""
    request = get_state().secret_requests.get(request_id)
    if request is None:
        return _error_response(f"No secret request with id {request_id!r}", 404)
    return _request_response(request, None)


def submit(request_id: str) -> Response:
    """Answer a secret request with the values (``values``, by variable name); they are written to the secrets file."""
    refusal = _refusal_on_a_secondary_chat()
    if refusal is not None:
        return refusal
    body = parse_request_body(SubmitSecretRequestBody)
    try:
        stored = get_state().secret_requests.submit(request_id, body.values)
    except UnknownSecretRequestError as e:
        return _error_response(str(e), 404)
    except SecretRequestNotPendingError as e:
        return _error_response(str(e), 409)
    except SecretValuesMismatchError as e:
        return _error_response(str(e))
    except SecretFileWriteError as e:
        return _error_response(str(e), 500)
    return _request_response(stored, _deliver(stored, SecretResolutionVerdict.STORED))


def decline(request_id: str) -> Response:
    """Decline a secret request, with an optional ``note`` for the agent."""
    refusal = _refusal_on_a_secondary_chat()
    if refusal is not None:
        return refusal
    body = parse_request_body(DeclineSecretRequestBody)
    try:
        declined = get_state().secret_requests.decline(request_id, body.note)
    except UnknownSecretRequestError as e:
        return _error_response(str(e), 404)
    except SecretRequestNotPendingError as e:
        return _error_response(str(e), 409)
    except InvalidSecretRequestError as e:
        return _error_response(str(e))
    return _request_response(declined, _deliver(declined, SecretResolutionVerdict.DECLINED))


ROUTE_TABLE: Final[RouteTable] = {
    ("POST", "/api/secret-requests"): RouteModels(
        request_model=FileSecretRequestBody, response_model=FiledSecretRequestResponse, success_status=201
    ),
    ("GET", "/api/secret-requests/{request_id}"): RouteModels(response_model=SecretRequestResponse),
    ("POST", "/api/secret-requests/{request_id}/submit"): RouteModels(
        request_model=SubmitSecretRequestBody, response_model=SecretRequestResponse
    ),
    ("POST", "/api/secret-requests/{request_id}/decline"): RouteModels(
        request_model=DeclineSecretRequestBody, response_model=SecretRequestResponse
    ),
}


def register_routes(application: Flask) -> None:
    """Wire `/api/secret-requests` onto the Flask application.

    The router attaches its :class:`SecretRequestChatBridge` to the app state before any
    of these serve a request. The file is written whether or not the notice lands: a
    value the user submitted is never dropped because the agent happened to be
    unreachable.
    """
    application.add_url_rule("/api/secret-requests", view_func=file_request, methods=["POST"])
    application.add_url_rule("/api/secret-requests/<request_id>", view_func=get_request, methods=["GET"])
    application.add_url_rule("/api/secret-requests/<request_id>/submit", view_func=submit, methods=["POST"])
    application.add_url_rule("/api/secret-requests/<request_id>/decline", view_func=decline, methods=["POST"])
