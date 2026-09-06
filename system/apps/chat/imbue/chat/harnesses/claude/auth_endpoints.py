"""HTTP endpoint handlers for `/api/claude-auth/*`: the auth status and the pasted-credential submit.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. The status route reads the `ClaudeAuthService`
(read-only: it reports claude's auth state and changes nothing); the
submit route hands the pasted credential to the `AuthFlowService`, which
adopts it as an account. Both are created once in
`main.build_production_state` (or by the test state builder) and stored
on the app's `ChatState`, which each handler reads via `get_state()`.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.chat.accounts import AccountError
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import claude_env_from_paste
from imbue.chat.harnesses.claude import auth
from imbue.chat.models import ClaudeAuthCredentialsRequest
from imbue.chat.models import ClaudeAuthStatusResponse
from imbue.chat.models import ErrorResponse
from imbue.chat.state import get_state

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
    # `auth_mode` rides along because mngr's deployment test asserts on it: it is how the
    # Imbue path proves the blob it sent was understood as a proxied setup and not as a
    # plain key. Derived from what was pasted, not from a probe.
    #
    # Through `claude_env_from_paste`, which is the SAME function that decided what to write,
    # so the two cannot disagree. `parse_credential_lines` is the strict env-block parser and
    # rejects a bare key outright -- and it ran after the account was already committed and
    # outside the try, so pasting a plain `sk-ant-...` (what the keys page hands you) minted
    # the account, made it the MRU, and then answered 500.
    return _json_response(
        {
            "account_id": account.id,
            "display": account.display,
            "logged_in": True,
            "auth_mode": auth.derive_auth_mode(claude_env_from_paste(pasted)).value,
        }
    )


def register_routes(application: Flask) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the Flask application.

    The handlers read the `ClaudeAuthService` from the
    app's `ChatState`; `main.build_production_state` (or the test state
    builder) places them there before the app serves requests.
    """
    application.add_url_rule("/api/claude-auth/status", view_func=get_status, methods=["GET"])
    application.add_url_rule("/api/claude-auth/submit-credentials", view_func=submit_credentials, methods=["POST"])
