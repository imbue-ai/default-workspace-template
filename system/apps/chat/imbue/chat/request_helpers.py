"""The request and response helpers the chat app's routes share."""

import json
import traceback
from typing import Any
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


def json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


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
    return json_response({"detail": f"Internal server error: {exc}"}, status_code=500)
