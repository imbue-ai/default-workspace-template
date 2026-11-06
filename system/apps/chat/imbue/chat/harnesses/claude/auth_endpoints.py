"""HTTP endpoint handlers for `/api/claude-auth/*`: the auth status and the pasted-credential submit.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. The status route reads the `ClaudeAuthService`
(read-only: it reports claude's auth state and changes nothing); the
submit route hands the pasted credential to the `AuthFlowService`, which
adopts it as an account. Both are created once in
`main.build_production_state` (or by the test state builder) and stored
on the app's `ChatAppState`, which each handler reads via `get_state()`.
"""

from __future__ import annotations

from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger

from imbue.chat.accounts import AccountError
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import claude_env_from_paste
from imbue.chat.harnesses.claude import auth
from imbue.chat.models import ClaudeAuthCredentialsRequest
from imbue.chat.models import ClaudeAuthCredentialsResponse
from imbue.chat.models import ClaudeAuthStatusResponse
from imbue.chat.state import get_state

logger = _loguru_logger

STATUS_ROUTE: Final[str] = "/api/claude-auth/status"
SUBMIT_CREDENTIALS_ROUTE: Final[str] = "/api/claude-auth/submit-credentials"


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
    return error_response(detail, status_code)


def get_status() -> Response:
    """GET /api/claude-auth/status -- current auth state."""
    service: auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        status = service.get_auth_status()
    except auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return model_response(_status_to_response(status))


def submit_credentials() -> Response:
    """POST /api/claude-auth/submit-credentials -- adopt a pasted credential as an account.

    Kept as its own endpoint because it is a cross-repo contract: the Electron chrome POSTs
    here after the user visits the Imbue keys page, and mngr's deployment test drives it.
    The paste mints an account of its own, so the account existing is the signed-in-with-
    Imbue flag and no running agent has to be restarted to see it.

    The strict parse rejects unmanaged keys and mixed-mode pastes with a 400 before
    anything is written.
    """
    body = parse_request_body(ClaudeAuthCredentialsRequest)
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
    return model_response(
        ClaudeAuthCredentialsResponse(
            account_id=account.id,
            display=account.display,
            logged_in=True,
            auth_mode=auth.derive_auth_mode(claude_env_from_paste(pasted)).value,
        )
    )


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", STATUS_ROUTE): RouteModels(response_model=ClaudeAuthStatusResponse),
    ("POST", SUBMIT_CREDENTIALS_ROUTE): RouteModels(
        request_model=ClaudeAuthCredentialsRequest, response_model=ClaudeAuthCredentialsResponse
    ),
}


def register_routes(application: Flask) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the Flask application.

    The handlers read the `ClaudeAuthService` from the
    app's `ChatAppState`; `main.build_production_state` (or the test state
    builder) places them there before the app serves requests.
    """
    application.add_url_rule(STATUS_ROUTE, view_func=get_status, methods=["GET"])
    application.add_url_rule(SUBMIT_CREDENTIALS_ROUTE, view_func=submit_credentials, methods=["POST"])
