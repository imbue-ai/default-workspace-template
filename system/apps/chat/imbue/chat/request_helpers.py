"""The request and response helpers the chat app's routes share.

A modelled route reads its body through ``app_schema.routes.parse_request_body`` and answers through
``model_response``; ``json_response`` here is for the routes the route table declares schemaless (the events, the
harness catalogs), whose answers are loosely shaped JSON no model describes.
"""

import json
import traceback
from typing import Any
from typing import Final

from app_schema.api_schema import ErrorResponse
from app_schema.routes import error_response
from app_schema.routes import model_response
from flask import Response
from flask import request
from loguru import logger
from werkzeug.exceptions import HTTPException

from imbue.chat.errors import ChatAppError

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


def refusal_answer(detail: str, user_facing_message: str | None = None, kind: str | None = None) -> ErrorResponse:
    """A refusal's answer. ``detail`` is the developer's reason, which scripts and logs read; ``user_facing_message``,
    when given, is the sentence a page shows in its place (the details keep ``detail``), with ``reportable: true``
    when it asks for a bug report, so the page offers the button for one; ``kind`` names the case for a page that
    offers different controls per case."""
    is_reportable = user_facing_message is not None and user_facing_message.endswith(_REPORT_A_BUG_ENDING)
    return ErrorResponse(
        detail=detail, user_facing_message=user_facing_message, reportable=True if is_reportable else None, kind=kind
    )


def refusal_response(
    detail: str, status_code: int, user_facing_message: str | None = None, kind: str | None = None
) -> Response:
    """``refusal_answer`` as the response under ``status_code``."""
    return model_response(refusal_answer(detail, user_facing_message, kind), status_code)


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
    return refusal_response(f"Internal server error: {exc}", 500, user_facing_message=UNEXPECTED_FAILURE_SENTENCE)
