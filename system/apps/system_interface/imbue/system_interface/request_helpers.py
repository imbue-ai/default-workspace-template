"""The error handler the shell's routes share: an unhandled exception as a 500 ``ErrorResponse``."""

import traceback

from app_schema.routes import error_response
from flask import Response
from flask import request
from loguru import logger
from werkzeug.exceptions import HTTPException


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
