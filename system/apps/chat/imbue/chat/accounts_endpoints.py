"""HTTP endpoints for the provider chooser: `/api/lanes` and `/api/accounts/*`.

Kept out of server.py for the same reason the claude-auth handlers are: the modal's logic
does not belong in the router.

The `AuthFlowService` holds the live PTY, so it is created once in `main.build_production_state` and
read back through `get_state()` here -- the subprocess has to survive between the POST that
starts a flow and the polls that advance it.
"""

from __future__ import annotations

import json
from typing import Final

from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger

from imbue.chat import accounts
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import flow_shape
from imbue.chat.harnesses.claude.auth import ClaudeAuthError
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import HARNESS_LABEL
from imbue.chat.harnesses.lanes import LANES
from imbue.chat.harnesses.lanes import LaneNotFoundError
from imbue.chat.harnesses.lanes import PasteMethod
from imbue.chat.harnesses.lanes import numbered_provider
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.registry import build_resolver
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.models import ErrorResponse
from imbue.chat.models import ModelOptionsResponse
from imbue.chat.request_helpers import parse_json_object_body
from imbue.chat.state import get_state

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
    # An account on a lane this build no longer has is left out rather than a 500, so the user
    # can still see and delete their other accounts (``harness_for`` logs it).
    rows = [
        {
            "id": numbered.account.id,
            "lane": numbered.account.lane,
            "harness": numbered.harness.value,
            # The number rides `provider` rather than `label` alone. Every surface that shows an
            # account renders the provider and the harness as two spans at different sizes, so a
            # number that lives only in the composed string is a number nothing displays -- which
            # is exactly how two "Anthropic (Claude Code)" rows ended up indistinguishable. The
            # composed label serves anything showing one string.
            "provider": numbered_provider(numbered.display, numbered.number),
            "harness_label": HARNESS_LABEL[numbered.harness],
            "seq": numbered.number,
            "name": numbered.account.name,
            "label": numbered.label,
        }
        for numbered in accounts.number_accounts(index.accounts)
    ]
    return _json_response({"accounts": rows, "mru": index.mru, "default": index.default_account})


def is_picker_dynamic(harness: HarnessType) -> bool:
    """Whether the harness's picker options are per agent rather than a catalog."""
    return get_catalog(harness).picker_mode is PickerMode.DYNAMIC


def account_model_options(account_id: str) -> Response:
    """The models a NEW agent on ``account_id`` could run on: what the switch dialog offers a handoff's successor.

    The account-level twin of the per-chat ``/model-options``. A static harness offers its whole
    catalog (``models`` null). A harness whose set is per agent (codex) has no catalog to offer,
    so the answer is the set an existing agent of this account was last offered, read off its
    sidecar, and ``options`` is empty when the account has run no agent yet: the dialog then
    offers only the default. A harness whose model the chat app cannot switch offers nothing.
    """
    try:
        account = accounts.resolve_account(account_id)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    harness = accounts.harness_for(account)
    if harness is None:
        return _error_response(f"Account {account_id} is on a lane this build does not have", status_code=404)
    if get_catalog(harness).switch_mode is SwitchMode.READ_ONLY:
        return _json_response(ModelOptionsResponse(models=None, options=()).model_dump())
    manager = get_state().agent_manager
    persisted: tuple[ModelOption, ...] | None = None
    for agent in manager.get_agents():
        if agent.harness is not harness or agent.labels.get("account") != account_id:
            continue
        agent_info = manager.get_agent_info_by_id(agent.id)
        if agent_info is None:
            continue
        options = build_resolver(agent_info).list_persisted_options()
        if options is None:
            return _json_response(ModelOptionsResponse(models=None).model_dump())
        persisted = options
        if persisted:
            break
    if persisted is None and is_picker_dynamic(harness):
        persisted = ()
    return _json_response(ModelOptionsResponse(models=None, options=persisted).model_dump())


def start_flow() -> Response:
    payload = parse_json_object_body()
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
    payload = parse_json_object_body()
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


def update_account(account_id: str) -> Response:
    """Set or clear an account's user-chosen name (`name`), or pin or unpin it as the account a
    new chat launches on (`is_default`). Either key alone is a complete request."""
    payload = parse_json_object_body()
    if isinstance(payload, Response):
        return payload
    if "name" not in payload and "is_default" not in payload:
        return _error_response("expected a name or is_default")
    is_default = payload.get("is_default")
    if is_default is not None and not isinstance(is_default, bool):
        return _error_response("is_default must be a boolean")
    raw_name = payload.get("name", "")
    # `str(None)` is "None" -- a four-character name the user never typed, under the cap and
    # therefore silently accepted. A null means "clear it", which is the empty string.
    if raw_name is not None and not isinstance(raw_name, str):
        return _error_response("name must be a string")
    name = raw_name or ""
    if len(name) > _MAX_ACCOUNT_NAME:
        return _error_response(f"a name can be at most {_MAX_ACCOUNT_NAME} characters")
    try:
        if "name" in payload:
            accounts.rename_account(account_id, name)
        if is_default is not None:
            accounts.set_default_account(account_id, is_default)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    return _json_response({"status": "ok"})


def register_routes(application: Flask) -> None:
    """Wire the chooser's endpoints onto the Flask application.

    `main.build_production_state` (or the test state builder) puts an `AuthFlowService` on the
    app state before any of these serve a request.
    """
    application.add_url_rule("/api/lanes", view_func=list_lanes, methods=["GET"])
    application.add_url_rule("/api/accounts", view_func=list_accounts, methods=["GET"])
    application.add_url_rule("/api/accounts", view_func=start_flow, methods=["POST"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=poll_flow, methods=["GET"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=submit_flow, methods=["POST"])
    application.add_url_rule("/api/accounts/flow/<flow_id>", view_func=abort_flow, methods=["DELETE"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=delete_account, methods=["DELETE"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=update_account, methods=["PATCH"])
    application.add_url_rule(
        "/api/accounts/<account_id>/model-options", view_func=account_model_options, methods=["GET"]
    )
