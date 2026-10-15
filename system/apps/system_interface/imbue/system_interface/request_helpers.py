"""The request and response helpers the shell's routes share."""

import html
import json
import traceback
from typing import Any
from typing import Final

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


# What a page load that failed shows in the workspace frame: a sentence of ours and a Try again, with the server's
# own text behind "See details" rather than a JSON body filling the frame.
_PAGE_FAILED_TEMPLATE: Final[str] = """<!doctype html>
<html><head><meta charset="utf-8"><title>This workspace couldn't load</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;background:#14161a;color:#e6e8eb}}main{{max-width:34rem;padding:2rem}}
p{{line-height:1.5;color:#b6bcc4}}button{{font:inherit;padding:0.4rem 1rem;color:#e6e8eb;background:#2a2f37;
border:1px solid #3a414b;border-radius:6px;cursor:pointer}}button:hover{{background:#333a44}}
details{{margin-top:1.5rem;font-size:0.85em;color:#8a929c}}summary{{cursor:pointer}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style></head>
<body><main><p>This workspace couldn't load. Try again. If this persists, report a bug.</p>
<p><button type="button" onclick="location.reload()">Try again</button></p>
<details><summary>See details</summary><pre>{details}</pre></details></main></body></html>
"""


def _is_page_request() -> bool:
    """Whether the failed request was a page load (a browser navigating to the workspace) rather than a call that
    reads the answer as JSON."""
    if request.path.startswith("/api/"):
        return False
    return request.accept_mimetypes.best_match(["application/json", "text/html"]) == "text/html"


def handle_unhandled_exception(exc: Exception) -> Response | HTTPException:
    # Let werkzeug's own HTTP errors (404 routing, 405, etc.) render normally;
    # only genuine unhandled exceptions become a 500. Returning the
    # exception (not re-raising it) is how Flask keeps the real status code --
    # a raise from inside the handler re-enters handle_exception and comes out
    # as a 500.
    if isinstance(exc, HTTPException):
        return exc
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.path, exc, "".join(tb))
    detail = f"Internal server error: {exc}"
    if _is_page_request():
        page = _PAGE_FAILED_TEMPLATE.format(details=html.escape(detail))
        return Response(page, status=500, mimetype="text/html", headers={"Cache-Control": "no-store"})
    return json_response({"detail": detail}, status_code=500)
