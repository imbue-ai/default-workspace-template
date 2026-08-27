"""HTTP endpoint handlers for `/api/claude-auth/*`.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. Every successful auth path hands the welcome
resender's `check_and_resend_welcome` to the service as the completion
hook, so the welcome-resend check runs exactly once per successful login
-- after the restarted chat agent is back up (or inline on the no-restart
subscription fast path).

The `ClaudeAuthService` (which holds the in-flight PTY auth subprocess)
and the `WelcomeResender` are created once in `create_application` and
stored on the app's `SystemInterfaceState`; each handler reads them via
`get_state()` so the subprocess survives between the `/setup-token/start`
call and the subsequent `/setup-token/poll` / `/setup-token/submit-code`
calls.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface.app_context import get_state
from imbue.system_interface.accounts import AccountError
from imbue.system_interface.harnesses.auth_flows import FlowError
from imbue.system_interface.harnesses.claude import auth
from imbue.system_interface.models import ClaudeAuthCredentialsRequest
from imbue.system_interface.models import ClaudeAuthStatusResponse
from imbue.system_interface.models import ClaudeOAuthLoginStartRequest
from imbue.system_interface.models import ClaudeSetupTokenPollRequest
from imbue.system_interface.models import ClaudeSetupTokenPollResponse
from imbue.system_interface.models import ClaudeSetupTokenStartResponse
from imbue.system_interface.models import ClaudeSetupTokenSubmitCodeRequest
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.welcome_resend import WelcomeResender

logger = _loguru_logger


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _status_to_response(status: auth.AuthStatus) -> ClaudeAuthStatusResponse:
    # Both models share the same field names and types; validating directly
    # off the AuthStatus dump keeps the conversion automatic so adding a
    # field to one side only needs the matching field added to the other,
    # not a third edit here.
    return ClaudeAuthStatusResponse.model_validate(status.model_dump())


def _error_response(detail: str, status_code: int = 400) -> Response:
    # Every auth-flow failure funnels through here; without this log the
    # container's service log shows only the access line for the 4xx/5xx,
    # leaving no server-side trace of what actually went wrong.
    logger.warning("Returning claude-auth error response ({}): {}", status_code, detail)
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def get_status() -> Response:
    """GET /api/claude-auth/status -- current auth state."""
    service: auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        status = service.get_auth_status()
    except auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())





def submit_credentials() -> Response:
    """POST /api/claude-auth/submit-credentials -- adopt a pasted credential as an account.

    Kept as its own endpoint because it is a cross-repo contract: the Electron chrome POSTs
    here after the user visits the Imbue keys page, and mngr's deployment test drives it.
    What changed is the destination -- the paste now mints an account of its own instead of
    overwriting the workspace's shared login, so the account existing is the signed-in-with-
    Imbue flag and no running agent has to be restarted to see it.

    The strict parse still rejects unmanaged keys and mixed-mode pastes with a 400 before
    anything is written.
    """
    try:
        body = ClaudeAuthCredentialsRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    pasted = body.credentials.get_secret_value().strip()
    if not pasted:
        return _error_response("credentials must be a non-empty string")
    try:
        account = get_state().auth_flows.adopt_claude_credentials(pasted)
    except auth.CredentialPasteError as e:
        return _error_response(str(e), status_code=400)
    except (AccountError, FlowError) as e:
        return _error_response(str(e), status_code=500)
    return _json_response({"account_id": account.id, "display": account.display, "logged_in": True})






def register_routes(application: Flask) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the Flask application.

    The handlers read the `ClaudeAuthService` / `WelcomeResender` from the
    app's `SystemInterfaceState`; `create_application` is responsible for
    placing them there before the app serves requests.
    """
    application.add_url_rule("/api/claude-auth/status", view_func=get_status, methods=["GET"])
    application.add_url_rule("/api/claude-auth/submit-credentials", view_func=submit_credentials, methods=["POST"])
