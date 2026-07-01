"""HTTP endpoint handlers for `/api/inspiration/*`.

Mirrors the shape of `claude_auth_endpoints.py`: the `InspirationService`
(which holds the pending-proposal state and the response-file handshake) is
created once in `create_application` and stored on the app's
`SystemInterfaceState`; each handler reads it via `get_state()`.

Every handler is loopback-guarded: these routes open publish proposals and
drive `gh repo create` + push through the polling skill, so they must only be
callable from inside the container (there is no auth between callers and the
system interface). The guard re-declares the loopback host set locally rather
than importing it from `server.py`, because `server.py` imports this module and
importing back would create a cycle.

Namespaced `endpoint=` names are mandatory: the `get_status` view function
shares its name with the claude-auth and github-auth handlers, and Flask
derives the endpoint from `view_func.__name__`, so without an explicit
`endpoint=` the app fails to build with a collision.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface.app_context import get_state
from imbue.system_interface.inspiration import InspirationError
from imbue.system_interface.inspiration import InspirationService
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import InspirationPublishConfirm
from imbue.system_interface.models import InspirationPublishRequest

logger = _loguru_logger

# Re-declared locally (rather than imported from server.py) to avoid an import
# cycle: server.py imports this module to register its routes.
_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _error_response(detail: str, status_code: int = 400) -> Response:
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def _require_loopback() -> Response | None:
    """Return a 403 response unless the request came from a loopback client."""
    if (request.remote_addr or "") not in _LOOPBACK_CLIENT_HOSTS:
        return _error_response("only callable from loopback", status_code=403)
    return None


def publish_request() -> Response:
    """POST /api/inspiration/publish-request -- record a pending proposal.

    Posted by the /publish-inspiration skill. Stores the pending slug, clears
    any stale response file, and broadcasts `inspiration_publish_requested` so
    the frontend opens the publish modal.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: InspirationService = get_state().inspiration_service
    try:
        body = InspirationPublishRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    service.record_request(body)
    return _json_response({"status": "ok"})


def publish_confirm() -> Response:
    """POST /api/inspiration/publish-confirm -- confirm the user-edited proposal.

    Posted by the frontend modal on Publish. Writes the confirmed (server-side
    sanitized) values to the response file so the polling skill unblocks and
    proceeds with `gh repo create` + push.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: InspirationService = get_state().inspiration_service
    try:
        body = InspirationPublishConfirm.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        response = service.confirm(body)
    except InspirationError as e:
        return _error_response(str(e))
    return _json_response(response.model_dump())


def abort_publish() -> Response:
    """POST /api/inspiration/abort -- abort the pending proposal.

    Writes an aborted response (so the skill's poll unblocks and leaves the
    assembled commit intact) and broadcasts `inspiration_publish_aborted`. The
    optional `slug` in the body scopes the abort to a specific proposal.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: InspirationService = get_state().inspiration_service
    body = request.get_json(silent=True) or {}
    slug = body.get("slug") if isinstance(body, dict) else None
    service.abort(slug)
    return _json_response({"status": "ok"})


def get_status() -> Response:
    """GET /api/inspiration/status -- the currently-pending proposal, if any."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: InspirationService = get_state().inspiration_service
    return _json_response(service.status().model_dump())


def register_routes(application: Flask) -> None:
    """Wire `/api/inspiration/*` endpoints onto the Flask application.

    The handlers read the `InspirationService` from the app's
    `SystemInterfaceState`; `create_application` is responsible for placing it
    there before the app serves requests. Every rule sets an explicit
    namespaced `endpoint=` to avoid a Flask endpoint-name collision with the
    other modules' `get_status` view functions.
    """
    application.add_url_rule(
        "/api/inspiration/publish-request",
        view_func=publish_request,
        methods=["POST"],
        endpoint="inspiration_publish_request",
    )
    application.add_url_rule(
        "/api/inspiration/publish-confirm",
        view_func=publish_confirm,
        methods=["POST"],
        endpoint="inspiration_publish_confirm",
    )
    application.add_url_rule(
        "/api/inspiration/abort",
        view_func=abort_publish,
        methods=["POST"],
        endpoint="inspiration_abort",
    )
    application.add_url_rule(
        "/api/inspiration/status",
        view_func=get_status,
        methods=["GET"],
        endpoint="inspiration_get_status",
    )
