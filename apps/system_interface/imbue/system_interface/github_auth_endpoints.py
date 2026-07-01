"""HTTP endpoint handlers for `/api/github-auth/*`.

Backend half of the in-UI GitHub login modal, so a user whose `gh`
credentials didn't sync into the mind can recover (and push) without
dropping into the ttyd terminal.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. The `GitHubAuthService` (which holds the
in-flight web/device-flow subprocess) is created once in
`create_application` and stored on the app's `SystemInterfaceState`; each
handler reads it via `get_state()` so the login subprocess survives
between the `/start` and `/submit-code` calls.

Every handler is loopback-guarded: these routes handle GitHub
credentials (PAT paste, device flow) and there is no authentication
between callers and the system interface inside the container, so they
must only be reachable from the local Electron frontend. Each
`add_url_rule` also sets an explicit namespaced `endpoint=` name --
several handlers here are named `get_status` / `submit_code` (matching
the claude and inspiration modules), and Flask derives the endpoint from
`view_func.__name__`, so without an explicit name the app fails to build
with an endpoint collision.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface import github_auth
from imbue.system_interface.app_context import get_state
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import GitHubAuthRawTokenRequest
from imbue.system_interface.models import GitHubAuthStartRequest
from imbue.system_interface.models import GitHubAuthStartResponse
from imbue.system_interface.models import GitHubAuthStatusResponse
from imbue.system_interface.models import GitHubAuthSubmitCodeRequest

logger = _loguru_logger

# Re-declared locally (rather than imported from server.py) to avoid an
# import cycle: server.py imports this module, so this module must not
# import from server.py. Keep in sync with server._LOOPBACK_CLIENT_HOSTS.
_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _error_response(detail: str, status_code: int = 400) -> Response:
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def _require_loopback() -> Response | None:
    """Return a 403 response for any non-loopback caller, else None.

    These routes handle GitHub credentials and trigger pushes, so they
    must only be reachable from the local frontend.
    """
    if (request.remote_addr or "") not in _LOOPBACK_CLIENT_HOSTS:
        return _error_response("github-auth is only callable from loopback", status_code=403)
    return None


def _status_to_response(status: github_auth.GitHubAuthStatus) -> GitHubAuthStatusResponse:
    # Both models share the same field names and types; validating directly
    # off the GitHubAuthStatus dump keeps the conversion automatic so adding
    # a field to one side only needs the matching field added to the other,
    # not a third edit here.
    return GitHubAuthStatusResponse.model_validate(status.model_dump())


def get_status() -> Response:
    """GET /api/github-auth/status -- current gh auth state."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: github_auth.GitHubAuthService = get_state().github_auth_service
    try:
        status = service.get_auth_status()
    except github_auth.GitHubAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())


def start_web() -> Response:
    """POST /api/github-auth/start -- spawn the `gh auth login --web` device flow."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: github_auth.GitHubAuthService = get_state().github_auth_service
    try:
        body = GitHubAuthStartRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        result = service.start_web_login(body.host)
    except github_auth.GitHubAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(
        GitHubAuthStartResponse(
            session_id=result.session_id,
            user_code=result.user_code,
            verification_url=result.verification_url,
        ).model_dump()
    )


def submit_code() -> Response:
    """POST /api/github-auth/submit-code -- complete the in-flight web/device flow."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: github_auth.GitHubAuthService = get_state().github_auth_service
    try:
        body = GitHubAuthSubmitCodeRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        status = service.submit_code(body.session_id)
    except github_auth.GitHubAuthError as e:
        return _error_response(str(e), status_code=400)
    return _json_response(_status_to_response(status).model_dump())


def submit_raw_token() -> Response:
    """POST /api/github-auth/submit-raw-token -- log in with a pasted PAT."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: github_auth.GitHubAuthService = get_state().github_auth_service
    try:
        body = GitHubAuthRawTokenRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    if not body.token.get_secret_value().strip():
        return _error_response("token must be a non-empty string")
    try:
        status = service.submit_raw_token(body.token, body.host)
    except github_auth.GitHubAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())


def require_auth() -> Response:
    """POST /api/github-auth/require -- ask the frontend to open the GitHub login modal.

    Broadcasts `{"type": "github_auth_required"}` over the WS broadcaster
    so the `/publish-inspiration` skill can prompt the user to log in when
    its own `gh auth status` check fails.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    get_state().broadcaster.broadcast({"type": "github_auth_required"})
    return _json_response({"status": "ok"})


def abort_login() -> Response:
    """POST /api/github-auth/abort -- drop the in-flight web/device-flow subprocess."""
    guard = _require_loopback()
    if guard is not None:
        return guard
    get_state().github_auth_service.abort_login()
    return _json_response({"status": "ok"})


def register_routes(application: Flask) -> None:
    """Wire `/api/github-auth/*` endpoints onto the Flask application.

    The handlers read the `GitHubAuthService` from the app's
    `SystemInterfaceState`; `create_application` is responsible for
    placing it there before the app serves requests. Each rule uses an
    explicit namespaced `endpoint=` to avoid the `get_status` /
    `submit_code` collisions with the claude and inspiration modules.
    """
    application.add_url_rule(
        "/api/github-auth/status", view_func=get_status, methods=["GET"], endpoint="github_auth_get_status"
    )
    application.add_url_rule(
        "/api/github-auth/start", view_func=start_web, methods=["POST"], endpoint="github_auth_start"
    )
    application.add_url_rule(
        "/api/github-auth/submit-code", view_func=submit_code, methods=["POST"], endpoint="github_auth_submit_code"
    )
    application.add_url_rule(
        "/api/github-auth/submit-raw-token",
        view_func=submit_raw_token,
        methods=["POST"],
        endpoint="github_auth_submit_raw_token",
    )
    application.add_url_rule(
        "/api/github-auth/require", view_func=require_auth, methods=["POST"], endpoint="github_auth_require"
    )
    application.add_url_rule(
        "/api/github-auth/abort", view_func=abort_login, methods=["POST"], endpoint="github_auth_abort"
    )
