"""HTTP endpoints for the provider chooser: `/api/lanes` and `/api/accounts/*`.

Kept out of server.py for the same reason the claude-auth handlers are: the modal's logic
does not belong in the router.

The `AuthFlowService` holds the live PTY, so it is created once in `create_application` and
read back through `get_state()` here -- the subprocess has to survive between the POST that
starts a flow and the polls that advance it.
"""

from __future__ import annotations

import json
from typing import Any
from typing import Final

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface import accounts
from imbue.system_interface.app_context import get_state
from imbue.system_interface.harnesses.auth_flows import FlowError
from imbue.system_interface.harnesses.claude.auth import ClaudeAuthError
from imbue.system_interface.harnesses.auth_flows import flow_shape
from imbue.system_interface.harnesses.lanes import HARNESS_LABEL
from imbue.system_interface.harnesses.lanes import LANES
from imbue.system_interface.harnesses.lanes import LaneNotFoundError
from imbue.system_interface.harnesses.lanes import PasteMethod
from imbue.system_interface.harnesses.lanes import account_label
from imbue.system_interface.harnesses.lanes import get_lane
from imbue.system_interface.harnesses.lanes import numbered_provider
from imbue.system_interface.models import ErrorResponse

logger = _loguru_logger

# Long enough for "Anthropic personal (work laptop)", short enough that no row can be made to
# push a flyout wider than the card it hangs off.
_MAX_ACCOUNT_NAME: Final = 40


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _error_response(detail: str, status_code: int = 400) -> Response:
    # Without this log the service log shows only the access line for the 4xx, leaving no
    # server-side trace of which lane or account the caller was actually asking for.
    logger.warning("Returning accounts error response ({}): {}", status_code, detail)
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def list_lanes() -> Response:
    """The chooser's rows, and what each one offers as a way in.

    `shape` rides on every method so the modal knows which of the three screens to render
    without having to know anything about harnesses.
    """
    payload = [
        {
            "id": lane.id,
            "provider_name": lane.provider_name,
            "subtitle": lane.subtitle,
            "harness": lane.harness.value,
            # Its display form too: the sign-in header states which harness the connection will
            # run on, and "claude" is the id, not something to show a user.
            "harness_label": HARNESS_LABEL[lane.harness],
            "methods": [
                {
                    "id": method.id,
                    "label": method.label,
                    "description": method.description,
                    "signup_url": method.signup_url if isinstance(method, PasteMethod) else "",
                    "shape": flow_shape(method).value,
                    "is_primary": index == 0,
                }
                for index, method in enumerate(lane.methods)
            ],
            "key_providers": [
                {
                    "provider_id": key.provider_id,
                    "display": key.display,
                    "env_var": key.env_var,
                    "hint": key.hint,
                }
                for key in lane.key_providers
            ],
        }
        for lane in LANES
    ]
    return _json_response({"lanes": payload})


def list_accounts() -> Response:
    """Every signed-in account, with the label the picker shows.

    The label is composed here rather than client-side only because it needs the lane table
    to turn a harness into "(Claude Code)"; the client would otherwise need a second copy.
    """
    index = accounts.read_index()
    rows = []
    # The stored `seq` counts per LANE, but the label names a provider and a harness -- and
    # those do not line up. Two lanes run on pi and can both mint an OpenRouter account, so
    # lane numbering gives two rows reading "OpenRouter (Pi)" with nothing between them;
    # meanwhile a lane that offers many providers numbers its only Groq account "Groq (Pi) 2"
    # because an OpenRouter one came first. Numbering here, over what the label actually
    # says, makes the number mean what the user reads it as.
    shown: dict[tuple[str, str], int] = {}
    for account in index.accounts:
        try:
            lane = get_lane(account.lane)
        except LaneNotFoundError:
            # A row naming a lane this build no longer has. Skip rather than 500 -- the user
            # can still see and delete their other accounts.
            logger.warning("Account {} names unknown lane {}; skipping", account.id, account.lane)
            continue
        # A renamed account is numbered under the name the user gave it, not the provider's.
        # Numbering the hidden name would put a "2" on a row with nothing beside it, and drop
        # the one that two rows reading "work" actually need.
        display = account.name if account.name != "" else account.display
        key = (display, lane.harness.value)
        shown[key] = shown.get(key, 0) + 1
        # The number rides `provider` rather than `label` alone. Every surface that shows an
        # account renders the provider and the harness as two spans at different sizes, so a
        # number that lives only in the composed string is a number nothing displays -- which
        # is exactly how two "Anthropic (Claude Code)" rows ended up indistinguishable.
        numbered = numbered_provider(display, shown[key])
        rows.append(
            {
                "id": account.id,
                "lane": account.lane,
                "harness": lane.harness.value,
                # The composed label ("Groq 2 (Pi)") for anything showing one string, and its
                # parts for the combo card, which renders the provider and the harness at
                # different sizes on one row. Composed here either way, so the numbering rule
                # lives in one place.
                "provider": numbered,
                "harness_label": HARNESS_LABEL[lane.harness],
                "seq": shown[key],
                "name": account.name,
                "label": account_label(display, lane.harness, shown[key]),
            }
        )
    return _json_response({"accounts": rows, "mru": index.mru})


def _json_object_body() -> dict[str, Any] | Response:
    """The request body as a JSON object, or a 400.

    `request.get_json(silent=True) or {}` only rescues a FALSY body, so a non-empty array,
    string or number is truthy and `payload.get(...)` raises AttributeError -- a 500 for a
    malformed request. A twin of `server._parse_json_object_body` rather than a shared import:
    `server` imports this module to register its routes, so importing back is a cycle.
    """
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        # Logged, not just answered: a 400 tells the caller, and this tells us. Swallowing a
        # decode error without a trace is the thing the ratchet exists to stop.
        logger.warning("Request to {} carried invalid JSON: {}", request.path, e)
        return _error_response("Invalid JSON in request body")
    if not isinstance(body, dict):
        return _error_response("Request body must be a JSON object")
    return body


def start_flow() -> Response:
    payload = _json_object_body()
    if isinstance(payload, Response):
        return payload
    lane_id = str(payload.get("lane_id", ""))
    method_id = str(payload.get("method_id", ""))
    account_id = payload.get("account_id") or None
    try:
        started = get_state().auth_flows.start(lane_id, method_id, account_id)
    except LaneNotFoundError as e:
        return _error_response(str(e), status_code=404)
    # A re-auth names an account, and `start` resolves it through the index rather than the
    # filesystem -- so an unknown or folder-less id arrives here rather than as a 500.
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    except FlowError as e:
        return _error_response(str(e))
    return _json_response(started.model_dump())


def poll_flow(flow_id: str) -> Response:
    try:
        return _json_response(get_state().auth_flows.poll(flow_id).model_dump())
    except FlowError as e:
        return _error_response(str(e), status_code=404)


def submit_flow(flow_id: str) -> Response:
    """Accept whatever the flow's shape asks the user for: a pasted code, or a key."""
    payload = _json_object_body()
    if isinstance(payload, Response):
        return payload
    service = get_state().auth_flows
    try:
        if "code" in payload:
            status = service.submit_code(flow_id, str(payload["code"]).strip())
        elif "api_key" in payload:
            # `key_provider` is only ever a string or absent. Passed through raw, a JSON list
            # or object reaches a set membership test and a dict key, both of which raise on an
            # unhashable value -- a 500 for a malformed body rather than a 400.
            raw_provider = payload.get("key_provider")
            if raw_provider is not None and not isinstance(raw_provider, str):
                return _error_response("key_provider must be a string")
            status = service.submit_key(flow_id, str(payload["api_key"]).strip(), raw_provider or None)
        else:
            return _error_response("expected a code or an api_key")
    except (FlowError, ClaudeAuthError) as e:
        # ClaudeAuthError too: a paste that fails claude's strict env-block parse raises
        # CredentialPasteError, which is a sibling of FlowError rather than a subclass. Escaping
        # here made a typo'd key a 500, and threw away the one message that says WHICH key was
        # wrong -- the user saw a generic failure instead of "Unsupported keys in paste: ...".
        return _error_response(str(e))
    return _json_response(status.model_dump())


def abort_flow(flow_id: str) -> Response:
    # Abort is what a closed modal calls on its way out, so it has to succeed even when the
    # flow it is abandoning is in a bad state -- a folder deleted underneath it, an unreadable
    # index. A 500 here reaches a UI that has already gone, and the user sees a failed request
    # for something they did not ask for.
    try:
        get_state().auth_flows.abort(flow_id)
    except (accounts.AccountError, OSError) as e:
        logger.warning("Aborting sign-in flow {} did not unwind cleanly: {}", flow_id, e)
    return _json_response({"status": "ok"})


def delete_account(account_id: str) -> Response:
    """Remove an account. Chats bound to it keep their transcripts; a harness already holding
    the credential keeps working until it restarts. The confirmation is the client's job."""
    try:
        accounts.delete_account(account_id)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    return _json_response({"status": "ok"})


def rename_account(account_id: str) -> Response:
    """Set or clear an account's user-chosen name. Display only -- see `accounts.rename_account`."""
    payload = _json_object_body()
    if isinstance(payload, Response):
        return payload
    raw_name = payload.get("name", "")
    # `str(None)` is "None" -- a four-character name the user never typed, under the cap and
    # therefore silently accepted. A null means "clear it", which is the empty string.
    if raw_name is not None and not isinstance(raw_name, str):
        return _error_response("name must be a string")
    name = raw_name or ""
    if len(name) > _MAX_ACCOUNT_NAME:
        return _error_response(f"a name can be at most {_MAX_ACCOUNT_NAME} characters")
    try:
        accounts.rename_account(account_id, name)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    return _json_response({"status": "ok"})


def register_routes(application: Flask) -> None:
    """Wire the chooser's endpoints onto the Flask application.

    `create_application` is responsible for putting an `AuthFlowService` on the app state
    before any of these serve a request.
    """
    application.add_url_rule("/api/lanes", view_func=list_lanes, methods=["GET"])
    application.add_url_rule("/api/accounts", view_func=list_accounts, methods=["GET"])
    application.add_url_rule("/api/accounts", view_func=start_flow, methods=["POST"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=poll_flow, methods=["GET"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=submit_flow, methods=["POST"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=abort_flow, methods=["DELETE"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=delete_account, methods=["DELETE"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=rename_account, methods=["PATCH"])
