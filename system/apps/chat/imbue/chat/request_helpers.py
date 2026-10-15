"""The request and response helpers the chat app's routes share."""

import json
import traceback
from typing import Any
from typing import Final
from typing import TypeVar

from app_manifest.manifest import describe_validation_error
from flask import Response
from flask import request
from loguru import logger
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from imbue.chat.errors import ChatAppError
from imbue.chat.errors import MalformedRequestError
from imbue.chat.models import ErrorResponse
from imbue.imbue_common.frozen_model import FrozenModel

_RequestModel = TypeVar("_RequestModel", bound=FrozenModel)

# The peer addresses a loopback-only route answers, as the shell's routes of that kind do.
_LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# What a page shows for an exception no route expected. Never "nothing was changed": it can come after part of the
# work was done.
UNEXPECTED_FAILURE_SENTENCE: Final[str] = (
    "Something went wrong in this chat. Try again. If this persists, report a bug."
)
# How a sentence for people that asks for a bug report ends.
_REPORT_A_BUG_ENDING: Final[str] = "If this persists, report a bug."


def json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def refusal_response(
    detail: str, status_code: int, user_facing_message: str | None = None, kind: str | None = None
) -> Response:
    """A refusal's answer. ``detail`` is the developer's reason, which scripts and logs read; ``user_facing_message``,
    when given, is the sentence a page shows in its place (the details keep ``detail``), with ``reportable: true``
    when it asks for a bug report, so the page offers the button for one; ``kind`` names the case for a page that
    offers different controls per case."""
    body: dict[str, str | bool] = {"detail": detail}
    if user_facing_message is not None:
        body["user_facing_message"] = user_facing_message
        if user_facing_message.endswith(_REPORT_A_BUG_ENDING):
            body["reportable"] = True
    if kind is not None:
        body["kind"] = kind
    return json_response(body, status_code=status_code)


def parse_json_object_body() -> dict[str, Any] | Response:
    """Parse the request body as a JSON object, or return a 400 error response."""
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        logger.opt(exception=e).warning("Request to {} carried invalid JSON", request.path)
        error = ErrorResponse(detail="Invalid JSON in request body")
        return json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return json_response(error.model_dump(), status_code=400)
    return body


def parse_request_body(model: type[_RequestModel]) -> _RequestModel:
    """The current request's body as ``model``; a body that is not a JSON object or not the shape raises
    MalformedRequestError (a 400 through ``answer_chat_app_error``)."""
    # force=True: the shell and curl alike may post without a JSON content type.
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise MalformedRequestError("the request body must be a JSON object")
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise MalformedRequestError(describe_validation_error(e)) from e


def require_loopback() -> Response | None:
    """A 403 for a request from a peer that is not this machine, else None."""
    if (request.remote_addr or "") not in _LOOPBACK_CLIENT_HOSTS:
        return json_response(ErrorResponse(detail="this route is only callable from loopback").model_dump(), 403)
    return None


def answer_chat_app_error(error: ChatAppError) -> Response:
    """The error handler for the chat app's own errors: a 400 with a ``{\"detail\"}`` body."""
    return json_response(ErrorResponse(detail=str(error)).model_dump(), status_code=400)


def handle_unhandled_exception(exc: Exception) -> Response | HTTPException:
    # Let werkzeug's own HTTP errors (404 routing, 405, etc.) render normally;
    # only genuine unhandled exceptions become a 500 JSON body. Returning the
    # exception (not re-raising it) is how Flask keeps the real status code --
    # a raise from inside the handler re-enters handle_exception and comes out
    # as a 500.
    if isinstance(exc, HTTPException):
        return exc
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.path, exc, "".join(tb))
    return refusal_response(f"Internal server error: {exc}", 500, user_facing_message=UNEXPECTED_FAILURE_SENTENCE)
