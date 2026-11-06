"""The request and response helpers the chat app's routes share.

A modelled route reads its body through ``app_schema.routes.parse_request_body`` and answers through
``model_response``; ``json_response`` here is for the routes the route table declares schemaless (the events, the
harness catalogs), whose answers are loosely shaped JSON no model describes.
"""

import json
import traceback
from typing import Any
from typing import Final

from app_schema.routes import error_response
from flask import Response
from flask import request
from loguru import logger
from werkzeug.exceptions import HTTPException

from imbue.chat.errors import ChatAppError

# The peer addresses a loopback-only route answers, as the shell's routes of that kind do.
_LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


def json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def require_loopback() -> Response | None:
    """A 403 for a request from a peer that is not this machine, else None."""
    if (request.remote_addr or "") not in _LOOPBACK_CLIENT_HOSTS:
        return error_response("this route is only callable from loopback", 403)
    return None


def answer_chat_app_error(error: ChatAppError) -> Response:
    """The error handler for the chat app's own errors: a 400 with a ``{\"detail\"}`` body."""
    return error_response(str(error), 400)


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
    return error_response(f"Internal server error: {exc}", 500)
