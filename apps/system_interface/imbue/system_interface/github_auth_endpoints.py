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
import threading
from collections.abc import Callable
from typing import Any

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
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


class GitHubAuthRequiredNotice(MutableModel):
    """Tracks whether a `github_auth_required` prompt is still awaiting the user.

    The `/api/github-auth/require` broadcast is fire-and-forget: a client that
    is not live on `/api/ws` at that instant never sees the login modal. This
    notice makes the prompt durable -- `require` marks it, and the `/api/ws`
    connect handler replays `{"type": "github_auth_required"}` to every newly
    connecting client while it is still marked. It is cleared as soon as the
    need is resolved: a successful login (PAT or web/device flow), a status
    check that reports the user as already logged in, or an explicit abort
    (the modal's dismiss).

    One instance is created per application in `create_application` and
    injected into `register_routes` (and the WebSocket connect handler),
    matching the dependency-injection style of the other service seams.
    Thread-safe: request threads and WebSocket handler threads both touch it.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_required: bool = PrivateAttr(default=False)

    def mark_required(self) -> None:
        with self._lock:
            self._is_required = True

    def clear(self) -> None:
        with self._lock:
            self._is_required = False

    def is_required(self) -> bool:
        with self._lock:
            return self._is_required

    def pending_event(self) -> dict[str, Any] | None:
        """The `github_auth_required` event to replay on WS connect, or None."""
        if self.is_required():
            return {"type": "github_auth_required"}
        return None


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


def get_status(auth_required_notice: GitHubAuthRequiredNotice) -> Response:
    """GET /api/github-auth/status -- current gh auth state.

    A logged-in status resolves any pending `github_auth_required` notice
    (the user may have logged in out-of-band, e.g. in a terminal), so the
    login modal stops being replayed to newly-connecting WebSocket clients.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    service: github_auth.GitHubAuthService = get_state().github_auth_service
    try:
        status = service.get_auth_status()
    except github_auth.GitHubAuthError as e:
        logger.warning("GET /api/github-auth/status failed: {}", e)
        return _error_response(str(e), status_code=500)
    if status.logged_in:
        auth_required_notice.clear()
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
        # The only server-side record of *why* the device flow failed to spawn
        # -- the client only ever sees the `detail` string in the 500 body, so
        # without this log line a failure here is undiagnosable from the
        # container's logs alone.
        logger.warning("POST /api/github-auth/start failed: {}", e)
        return _error_response(str(e), status_code=500)
    return _json_response(
        GitHubAuthStartResponse(
            session_id=result.session_id,
            user_code=result.user_code,
            verification_url=result.verification_url,
        ).model_dump()
    )


def submit_code(auth_required_notice: GitHubAuthRequiredNotice) -> Response:
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
        logger.warning("POST /api/github-auth/submit-code failed: {}", e)
        return _error_response(str(e), status_code=400)
    if status.logged_in:
        auth_required_notice.clear()
    return _json_response(_status_to_response(status).model_dump())


def submit_raw_token(auth_required_notice: GitHubAuthRequiredNotice) -> Response:
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
        logger.warning("POST /api/github-auth/submit-raw-token failed: {}", e)
        return _error_response(str(e), status_code=500)
    if status.logged_in:
        auth_required_notice.clear()
    return _json_response(_status_to_response(status).model_dump())


def require_auth(
    auth_required_notice: GitHubAuthRequiredNotice,
    get_ws_client_count: Callable[[], int],
) -> Response:
    """POST /api/github-auth/require -- ask the frontend to open the GitHub login modal.

    Broadcasts `{"type": "github_auth_required"}` over the WS broadcaster
    so the `/publish-inspiration` skill can prompt the user to log in when
    its own `gh auth status` check fails. Also marks the durable notice so
    the event is replayed to WebSocket clients that connect later.

    The 200 reply carries `ws_client_count`: the number of currently-connected
    `/api/ws` clients at broadcast time, so the caller can skip (or shorten)
    its wait for the user when nobody was listening.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    auth_required_notice.mark_required()
    get_state().broadcaster.broadcast({"type": "github_auth_required"})
    return _json_response({"status": "ok", "ws_client_count": get_ws_client_count()})


def abort_login(auth_required_notice: GitHubAuthRequiredNotice) -> Response:
    """POST /api/github-auth/abort -- drop the in-flight web/device-flow subprocess.

    Also resolves any pending `github_auth_required` notice: the user
    explicitly dismissed the login modal, so it must not be replayed to the
    next connecting WebSocket client.
    """
    guard = _require_loopback()
    if guard is not None:
        return guard
    auth_required_notice.clear()
    get_state().github_auth_service.abort_login()
    return _json_response({"status": "ok"})


def register_routes(
    application: Flask,
    auth_required_notice: GitHubAuthRequiredNotice,
    get_ws_client_count: Callable[[], int],
) -> None:
    """Wire `/api/github-auth/*` endpoints onto the Flask application.

    The handlers read the `GitHubAuthService` from the app's
    `SystemInterfaceState`; `create_application` is responsible for
    placing it there before the app serves requests.
    `auth_required_notice` and `get_ws_client_count` are injected by
    `create_application` (the former shared with the `/api/ws` connect
    handler for replay, the latter bound to the WS connection counter).
    Each rule uses an explicit namespaced `endpoint=` to avoid the
    `get_status` / `submit_code` collisions with the claude and
    inspiration modules (the explicit names also let lambda views
    register without a `__name__`).
    """
    application.add_url_rule(
        "/api/github-auth/status",
        view_func=lambda: get_status(auth_required_notice),
        methods=["GET"],
        endpoint="github_auth_get_status",
    )
    application.add_url_rule(
        "/api/github-auth/start", view_func=start_web, methods=["POST"], endpoint="github_auth_start"
    )
    application.add_url_rule(
        "/api/github-auth/submit-code",
        view_func=lambda: submit_code(auth_required_notice),
        methods=["POST"],
        endpoint="github_auth_submit_code",
    )
    application.add_url_rule(
        "/api/github-auth/submit-raw-token",
        view_func=lambda: submit_raw_token(auth_required_notice),
        methods=["POST"],
        endpoint="github_auth_submit_raw_token",
    )
    application.add_url_rule(
        "/api/github-auth/require",
        view_func=lambda: require_auth(auth_required_notice, get_ws_client_count),
        methods=["POST"],
        endpoint="github_auth_require",
    )
    application.add_url_rule(
        "/api/github-auth/abort",
        view_func=lambda: abort_login(auth_required_notice),
        methods=["POST"],
        endpoint="github_auth_abort",
    )
