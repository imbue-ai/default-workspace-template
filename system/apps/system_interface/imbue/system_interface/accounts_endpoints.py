"""HTTP endpoints for the provider chooser: `/api/lanes` and `/api/accounts/*`.

Kept out of server.py for the same reason the claude-auth handlers are: the modal's logic
does not belong in the router.

The `AuthFlowService` holds the live PTY, so it is created once in `create_application` and
read back through `get_state()` here -- the subprocess has to survive between the POST that
starts a flow and the polls that advance it.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface import accounts
from imbue.system_interface.app_context import get_state
from imbue.system_interface.harnesses.auth_flows import FlowError
from imbue.system_interface.harnesses.auth_flows import flow_shape
from imbue.system_interface.harnesses.lanes import LANES
from imbue.system_interface.harnesses.lanes import LaneNotFoundError
from imbue.system_interface.harnesses.lanes import PasteMethod
from imbue.system_interface.harnesses.lanes import account_label
from imbue.system_interface.harnesses.lanes import get_lane
from imbue.system_interface.models import ErrorResponse

logger = _loguru_logger


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
        key = (account.display, lane.harness.value)
        shown[key] = shown.get(key, 0) + 1
        rows.append(
            {
                "id": account.id,
                "lane": account.lane,
                "harness": lane.harness.value,
                "label": account_label(account.display, lane.harness, shown[key]),
            }
        )
    return _json_response({"accounts": rows, "mru": index.mru})


def start_flow() -> Response:
    payload = request.get_json(silent=True) or {}
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
    payload = request.get_json(silent=True) or {}
    service = get_state().auth_flows
    try:
        if "code" in payload:
            status = service.submit_code(flow_id, str(payload["code"]).strip())
        elif "api_key" in payload:
            status = service.submit_key(
                flow_id, str(payload["api_key"]).strip(), payload.get("key_provider") or None
            )
        else:
            return _error_response("expected a code or an api_key")
    except FlowError as e:
        return _error_response(str(e))
    return _json_response(status.model_dump())


def abort_flow(flow_id: str) -> Response:
    get_state().auth_flows.abort(flow_id)
    return _json_response({"status": "ok"})


def delete_account(account_id: str) -> Response:
    """Remove an account. Chats bound to it keep their transcripts and stop being able to
    take a turn -- the confirmation that says so is the client's job."""
    try:
        accounts.delete_account(account_id)
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
