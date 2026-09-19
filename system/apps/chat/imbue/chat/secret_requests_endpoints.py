"""HTTP endpoints for `/api/secret-requests`: filing a secret request, and answering it from the card.

The request script (``.agents/skills/connect-external-service/scripts/request_secret.py``)
files through the POST and prints what it gets back; the chat page's secret card submits
or declines through the two verb routes and hydrates itself through the GET after a
reload. The values a submit carries are handed to the store for the file write and
appear nowhere else: not in a log line, not in an error body, not in the notice the agent
receives.

Kept out of server.py like the other endpoint modules; the two things it needs from the
router -- whether a chat id names a chat, and how to put a notice into that chat's
transcript -- are handed in at registration, so the module never imports the router.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import auto

from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger

from imbue.chat.harnesses.message_display import format_secret_resolution_notice
from imbue.chat.models import ErrorResponse
from imbue.chat.request_helpers import json_response
from imbue.chat.request_helpers import parse_json_object_body
from imbue.chat.secret_requests import FiledSecretRequest
from imbue.chat.secret_requests import InvalidSecretRequestError
from imbue.chat.secret_requests import SecretFileWriteError
from imbue.chat.secret_requests import SecretRequest
from imbue.chat.secret_requests import SecretRequestNotPendingError
from imbue.chat.secret_requests import SecretValuesMismatchError
from imbue.chat.secret_requests import UnknownSecretRequestError
from imbue.chat.state import get_state
from imbue.imbue_common.enums import UpperCaseStrEnum

logger = _loguru_logger


class ChatLookup(UpperCaseStrEnum):
    """What the router knows about a chat id when a request names it."""

    KNOWN = auto()
    UNKNOWN = auto()
    # The agent list has not been read yet, so nothing can be said either way.
    NOT_READY = auto()


class NoticeDeliveryError(Exception):
    """Raised by the router's notice delivery when the chat's agent could not take the message."""


LookupChat = Callable[[str], ChatLookup]
DeliverNotice = Callable[[str, str], None]


def _error_response(detail: str, status_code: int = 400) -> Response:
    logger.warning("Returning secret-request error response ({}): {}", status_code, detail)
    return json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def _request_response(request: SecretRequest, is_notice_delivered: bool | None) -> Response:
    body = {**request.model_dump(mode="json"), "env_path": request.env_path}
    if is_notice_delivered is not None:
        body["is_notice_delivered"] = is_notice_delivered
    return json_response(body)


def _deliver(deliver_notice: DeliverNotice, request: SecretRequest, verdict: str) -> bool:
    """Put the resolution notice into the request's chat; False (and a warning) when the chat could not take it."""
    notice = format_secret_resolution_notice(
        verdict, request.request_id, request.env_path, request.variables, request.note
    )
    try:
        deliver_notice(request.chat_id, notice)
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


def _notify_superseded_in_other_chats(deliver_notice: DeliverNotice, filed: FiledSecretRequest) -> None:
    """A superseded request in ANOTHER chat learns of it only through a notice; the same chat's transcript walk sees the newer request itself."""
    for superseded in filed.superseded:
        if superseded.chat_id != filed.request.chat_id:
            _deliver(deliver_notice, superseded, "superseded")


def _file_request(lookup_chat: LookupChat, deliver_notice: DeliverNotice) -> Callable[[], Response]:
    def file_request() -> Response:
        payload = parse_json_object_body()
        if isinstance(payload, Response):
            return payload
        chat_id = payload.get("chat_id")
        file = payload.get("file")
        variables = payload.get("variables")
        rationale = payload.get("rationale")
        if not isinstance(chat_id, str) or not chat_id:
            return _error_response("chat_id must be a non-empty string")
        if not isinstance(file, str) or not isinstance(rationale, str):
            return _error_response("file and rationale must be strings")
        if not isinstance(variables, list) or not all(isinstance(name, str) for name in variables):
            return _error_response("variables must be a list of strings")
        match lookup_chat(chat_id):
            case ChatLookup.NOT_READY:
                return _error_response(
                    "The chat app has not read its agent list from mngr yet; try again shortly.", 503
                )
            case ChatLookup.UNKNOWN:
                return _error_response(f"Chat '{chat_id}' not found", 404)
            case ChatLookup.KNOWN:
                pass
        try:
            filed = get_state().secret_requests.file_request(chat_id, file, variables, rationale)
        except InvalidSecretRequestError as e:
            return _error_response(str(e))
        _notify_superseded_in_other_chats(deliver_notice, filed)
        return json_response(filed.as_wire(), status_code=201)

    return file_request


def get_request(request_id: str) -> Response:
    request = get_state().secret_requests.get(request_id)
    if request is None:
        return _error_response(f"No secret request with id {request_id!r}", 404)
    return _request_response(request, None)


def _submit(deliver_notice: DeliverNotice) -> Callable[[str], Response]:
    def submit(request_id: str) -> Response:
        payload = parse_json_object_body()
        if isinstance(payload, Response):
            return payload
        values = payload.get("values")
        if not isinstance(values, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in values.items()
        ):
            return _error_response("values must be an object of variable name to string")
        try:
            stored = get_state().secret_requests.submit(request_id, values)
        except UnknownSecretRequestError as e:
            return _error_response(str(e), 404)
        except SecretRequestNotPendingError as e:
            return _error_response(str(e), 409)
        except SecretValuesMismatchError as e:
            return _error_response(str(e))
        except SecretFileWriteError as e:
            return _error_response(str(e), 500)
        return _request_response(stored, _deliver(deliver_notice, stored, "stored"))

    return submit


def _decline(deliver_notice: DeliverNotice) -> Callable[[str], Response]:
    def decline(request_id: str) -> Response:
        payload = parse_json_object_body()
        if isinstance(payload, Response):
            return payload
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            return _error_response("note must be a string")
        try:
            declined = get_state().secret_requests.decline(request_id, note)
        except UnknownSecretRequestError as e:
            return _error_response(str(e), 404)
        except SecretRequestNotPendingError as e:
            return _error_response(str(e), 409)
        except InvalidSecretRequestError as e:
            return _error_response(str(e))
        return _request_response(declined, _deliver(deliver_notice, declined, "declined"))

    return decline


def register_routes(application: Flask, lookup_chat: LookupChat, deliver_notice: DeliverNotice) -> None:
    """Wire `/api/secret-requests` onto the Flask application.

    ``lookup_chat`` says whether a chat id names a chat; ``deliver_notice`` puts a
    resolution notice into that chat's transcript, raising NoticeDeliveryError when the
    chat's agent cannot take it. The file is written either way: a value the user
    submitted is never dropped because the agent happened to be unreachable.
    """
    application.add_url_rule(
        "/api/secret-requests", view_func=_file_request(lookup_chat, deliver_notice), methods=["POST"]
    )
    application.add_url_rule("/api/secret-requests/<request_id>", view_func=get_request, methods=["GET"])
    application.add_url_rule(
        "/api/secret-requests/<request_id>/submit", view_func=_submit(deliver_notice), methods=["POST"]
    )
    application.add_url_rule(
        "/api/secret-requests/<request_id>/decline", view_func=_decline(deliver_notice), methods=["POST"]
    )
