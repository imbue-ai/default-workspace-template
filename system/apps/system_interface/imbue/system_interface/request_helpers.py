"""The request and response helpers the shell's routes share."""

import json
import traceback
from typing import Any

from flask import Response
from flask import request
from loguru import logger
from werkzeug.exceptions import HTTPException


def json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def error_response(detail: str, status_code: int) -> Response:
    """The ``{"detail"}`` error body every shell route answers with."""
    return json_response({"detail": detail}, status_code=status_code)


def parse_json_object_body() -> dict[str, Any] | Response:
    """Parse the request body as a JSON object, or return a 400 error response."""
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        logger.opt(exception=e).warning("Request to {} carried invalid JSON", request.path)
        return error_response("Invalid JSON in request body", 400)
    if not isinstance(body, dict):
        return error_response("Request body must be a JSON object", 400)
    return body


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
