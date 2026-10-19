"""HTTP endpoints for the provider chooser: `/api/lanes` and `/api/accounts/*`.

Kept out of server.py for the same reason the claude-auth handlers are: the modal's logic
does not belong in the router.

The `AuthFlowService` holds the live PTY, so it is created once in `main.build_production_state` and
read back through `get_state()` here -- the subprocess has to survive between the POST that
starts a flow and the polls that advance it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import StrictBool

from imbue.chat import accounts
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import FlowShape
from imbue.chat.harnesses.auth_flows import FlowStart
from imbue.chat.harnesses.auth_flows import FlowStatus
from imbue.chat.harnesses.auth_flows import flow_shape
from imbue.chat.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.chat.harnesses.claude.auth import ClaudeAuthError
from imbue.chat.harnesses.claude.auth import read_managed_auth_env
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import AppServerMethod
from imbue.chat.harnesses.lanes import HARNESS_LABEL
from imbue.chat.harnesses.lanes import LANES
from imbue.chat.harnesses.lanes import LaneNotFoundError
from imbue.chat.harnesses.lanes import PasteMethod
from imbue.chat.harnesses.lanes import get_lane
from imbue.chat.harnesses.lanes import numbered_provider
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.harnesses.registry import list_account_options
from imbue.chat.identity import forbid_unless_owner
from imbue.chat.models import ModelOptionsResponse
from imbue.chat.models import OkResponse
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

# Long enough for "Anthropic personal (work laptop)", short enough that no row can be made to
# push a flyout wider than the card it hangs off.
_MAX_ACCOUNT_NAME: Final = 40


class SignInMethodRow(FrozenModel):
    """One way into a lane, as the chooser offers it."""

    id: str = Field(description="The method's id, named when a flow starts")
    label: str = Field(description="The method's name on its button")
    description: str = Field(description="What the method asks of the user, in a line")
    signup_url: str = Field(description="Where to get a key for a paste method; '' for the others")
    note: str = Field(description="A note shown beside an app-server method; '' for the others")
    shape: FlowShape = Field(description="What the dialog renders for the flow: a link and a code, a code, or a paste")
    is_primary: bool = Field(description="Whether this is the lane's first, recommended method")


class KeyProviderRow(FrozenModel):
    """A provider a bring-your-own-key lane accepts a key for."""

    provider_id: str = Field(description="The provider's id, named on a key submit")
    display: str = Field(description="The provider's name as shown")
    env_var: str = Field(description="The environment variable the key is stored under")
    hint: str = Field(description="What the key looks like, for the paste field")


class LaneRow(FrozenModel):
    """One row of the provider chooser: a provider and the harness it runs on, with its ways in."""

    id: str = Field(description="The lane's id, named when a flow starts")
    provider_name: str = Field(description="The provider's name")
    subtitle: str = Field(description="The row's second line")
    harness: HarnessType = Field(description="The harness a chat on this lane runs on")
    harness_label: str = Field(description="The harness's name as shown to the user")
    methods: tuple[SignInMethodRow, ...] = Field(description="The ways into the lane, the primary one first")
    key_providers: tuple[KeyProviderRow, ...] = Field(
        description="The providers a key may be for; empty for most lanes"
    )


class LanesResponse(FrozenModel):
    """Response from GET /api/lanes."""

    lanes: tuple[LaneRow, ...] = Field(description="The chooser's rows")


class AccountRow(FrozenModel):
    """One signed-in account as the picker shows it."""

    id: str = Field(description="The account's id")
    lane: str = Field(description="The lane the account signed in on")
    harness: HarnessType = Field(description="The harness the account's chats run on")
    provider: str = Field(description="The provider noun, numbered among the accounts that read the same")
    harness_label: str = Field(description="The harness's name as shown to the user")
    seq: int = Field(description="The account's number among those sharing its provider and harness")
    name: str = Field(description="The user's own name for the account; '' for none")
    label: str = Field(description="The provider, the harness and the number as one string")
    reauth_method: str = Field(description="The method 'Sign in again' runs for the account")


class AccountsResponse(FrozenModel):
    """Response from GET /api/accounts."""

    accounts: tuple[AccountRow, ...] = Field(description="Every signed-in account, in index order")
    mru: str | None = Field(description="The account the newest chat was created on, if any")
    default: str | None = Field(description="The account the user pinned for new chats, if any")


class StartFlowRequest(FrozenModel):
    """Request body for POST /api/accounts: sign in to a lane, or sign an account in again."""

    lane_id: str = Field(default="", description="The lane to sign in to")
    method_id: str = Field(default="", description="The lane's method to sign in with")
    account_id: str | None = Field(default=None, description="The account to sign in again; None for a new one")


class SubmitFlowRequest(FrozenModel):
    """Request body for POST /api/accounts/flow/{flow_id}: what the flow's shape asked the user for."""

    code: str | None = Field(default=None, description="The code pasted back from the sign-in page")
    api_key: str | None = Field(default=None, description="The key pasted in")
    key_provider: str | None = Field(default=None, description="Which provider the key is for, on a lane that asks")


class RelayCallbackRequest(FrozenModel):
    """Request body for POST /api/accounts/flow/{flow_id}/callback: the callback the Imbue Studio app received."""

    path_and_query: str = Field(description="The callback's path and query, as the browser requested it")


class FlowCallbackResponse(FlowStatus):
    """The flow's status once the CLI has the callback, plus the provider's name for the page the browser ends on."""

    provider_name: str = Field(description="The provider the flow signs in to")


class UpdateAccountRequest(FrozenModel):
    """Request body for PATCH /api/accounts/{account_id}; either field alone is a complete request."""

    name: str | None = Field(default=None, description="The account's new name; null or '' clears it")
    is_default: StrictBool | None = Field(
        default=None, description="Whether to pin the account as the one a new chat launches on"
    )


def _error_response(detail: str, status_code: int = 400) -> Response:
    # Without this log the service log shows only the access line for the 4xx, leaving no
    # server-side trace of which lane or account the caller was actually asking for.
    logger.warning("Returning accounts error response ({}): {}", status_code, detail)
    return error_response(detail, status_code)


def list_lanes() -> Response:
    """The chooser's rows, and what each one offers as a way in.

    `shape` rides on every method so the modal knows which screen to render
    without having to know anything about harnesses.
    """
    rows = tuple(
        LaneRow(
            id=lane.id,
            provider_name=lane.provider_name,
            subtitle=lane.subtitle,
            harness=lane.harness,
            # Its display form too: the sign-in header states which harness the connection will
            # run on, and "claude" is the id, not something to show a user.
            harness_label=HARNESS_LABEL[lane.harness],
            methods=tuple(
                SignInMethodRow(
                    id=method.id,
                    label=method.label,
                    description=method.description,
                    signup_url=method.signup_url if isinstance(method, PasteMethod) else "",
                    note=method.note if isinstance(method, AppServerMethod) else "",
                    shape=flow_shape(method),
                    is_primary=index == 0,
                )
                for index, method in enumerate(lane.methods)
            ),
            key_providers=tuple(
                KeyProviderRow(provider_id=key.provider_id, display=key.display, env_var=key.env_var, hint=key.hint)
                for key in lane.key_providers
            ),
        )
        for lane in LANES
    )
    return model_response(LanesResponse(lanes=rows))


def list_accounts() -> Response:
    """Every signed-in account, with the label the picker shows.

    The label is composed here rather than client-side only because it needs the lane table
    to turn a harness into "(Claude Code)"; the client would otherwise need a second copy.
    """
    index = accounts.read_index()
    # An account on a lane this build no longer has is left out rather than a 500, so the user
    # can still see and delete their other accounts (``harness_for`` logs it).
    rows = tuple(
        AccountRow(
            id=numbered.account.id,
            lane=numbered.account.lane,
            harness=numbered.harness,
            # The number rides `provider` rather than `label` alone. Every surface that shows an
            # account renders the provider and the harness as two spans at different sizes, so a
            # number that lives only in the composed string is a number nothing displays -- which
            # is exactly how two "Anthropic (Claude Code)" rows ended up indistinguishable. The
            # composed label serves anything showing one string.
            provider=numbered_provider(numbered.display, numbered.number),
            harness_label=HARNESS_LABEL[numbered.harness],
            seq=numbered.number,
            name=numbered.account.name,
            label=numbered.label,
            reauth_method=_reauth_method_id(numbered.account, numbered.harness),
        )
        for numbered in accounts.number_accounts(index.accounts)
    )
    return model_response(AccountsResponse(accounts=rows, mru=index.mru, default=index.default_account))


def _reauth_method_id(account: accounts.Account, harness: HarnessType) -> str:
    """The method "Sign in again" runs for an account: the kind of credential it already holds.

    An API-key account is given a key again, not sent to a browser sign-in it never used.
    """
    lane = get_lane(account.lane)
    folder = accounts.account_dir(account.id)
    match harness:
        case HarnessType.CLAUDE:
            is_key = ANTHROPIC_API_KEY_ENV_VAR in read_managed_auth_env(
                settings_path_override=folder / "settings.json"
            )
        case HarnessType.CODEX:
            is_key = _codex_auth_mode(folder) == "apikey"
        case _:
            is_key = False
    if is_key:
        paste = next((method for method in lane.methods if isinstance(method, PasteMethod)), None)
        if paste is not None:
            return paste.id
    return lane.methods[0].id


def _codex_auth_mode(account_folder: Path) -> str | None:
    path = account_folder / "auth.json"
    try:
        auth = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read codex's {}: {}", path, e)
        return None
    mode = auth.get("auth_mode") if isinstance(auth, dict) else None
    return mode if isinstance(mode, str) else None


def account_model_options(account_id: str) -> Response:
    """The models a switch onto ``account_id`` can pick from: the switch dialog's offer for a handoff or a rebind.

    The account-level twin of the per-chat ``/model-options``. A static harness offers its whole
    catalog (``models`` and ``options`` both null). A harness whose set is account-derived (codex)
    has no catalog to offer, so the account's own codex is asked for it. A harness whose model the
    chat app cannot switch offers nothing (``options`` empty).

    The question is put to the ACCOUNT, never to some other account's agent: the two accounts of one
    provider can be on different subscriptions and genuinely offer different models, so an answer
    borrowed from one would mis-describe the other -- both by hiding a model it has and by offering
    one it cannot run, which a rebind then fails on at its model step.
    """
    try:
        account = accounts.resolve_account(account_id)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    harness = accounts.harness_for(account)
    if harness is None:
        return _error_response(f"Account {account_id} is on a lane this build does not have", status_code=404)
    if get_catalog(harness).switch_mode is SwitchMode.READ_ONLY:
        return model_response(ModelOptionsResponse(models=None, options=()))
    options = list_account_options(harness, accounts.account_dir(account_id))
    return model_response(ModelOptionsResponse(models=None, options=options))


def start_flow() -> Response:
    """Start signing in to a provider (``lane_id``, ``method_id``), or signing an existing account in again.

    Answers the flow's id and what it asks the user for next (a browser to open, a code to paste, a key).
    """
    start_request = parse_request_body(StartFlowRequest)
    try:
        started = get_state().auth_flows.start(
            start_request.lane_id, start_request.method_id, start_request.account_id or None
        )
    except LaneNotFoundError as e:
        return _error_response(str(e), status_code=404)
    # A re-auth names an account, and `start` resolves it through the index rather than the
    # filesystem -- so an unknown or folder-less id arrives here rather than as a 500.
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    except FlowError as e:
        return _error_response(str(e))
    return model_response(started)


def poll_flow(flow_id: str) -> Response:
    """Where a sign-in flow stands: pending, done with its account, or failed with the reason."""
    try:
        return model_response(get_state().auth_flows.poll(flow_id))
    except FlowError as e:
        return _error_response(str(e), status_code=404)


def submit_flow(flow_id: str) -> Response:
    """Accept whatever the flow's shape asks the user for: a pasted code, or a key."""
    submit_request = parse_request_body(SubmitFlowRequest)
    service = get_state().auth_flows
    try:
        if submit_request.code is not None:
            status = service.submit_code(flow_id, submit_request.code.strip())
        elif submit_request.api_key is not None:
            status = service.submit_key(flow_id, submit_request.api_key.strip(), submit_request.key_provider or None)
        else:
            return _error_response("expected a code or an api_key")
    except (FlowError, ClaudeAuthError) as e:
        # ClaudeAuthError too: a paste that fails claude's strict env-block parse raises
        # CredentialPasteError, which is a sibling of FlowError rather than a subclass. Escaping
        # here made a typo'd key a 500, and threw away the one message that says WHICH key was
        # wrong -- the user saw a generic failure instead of "Unsupported keys in paste: ...".
        return _error_response(str(e))
    return model_response(status)


def relay_flow_callback(flow_id: str) -> Response:
    """Deliver the callback the minds desktop app received on this flow's loopback callback port.

    The desktop app listens where the user's browser runs and posts the callback here as
    `{path_and_query}`; the answer is the flow's status once the CLI has it, plus the provider's
    name, which the desktop app turns into the page the browser ends on.
    """
    refusal = forbid_unless_owner()
    if refusal is not None:
        return refusal
    callback = parse_request_body(RelayCallbackRequest)
    service = get_state().auth_flows
    try:
        provider_name = service.provider_name(flow_id)
        status = service.relay_callback(flow_id, callback.path_and_query)
    except FlowError as e:
        return _error_response(str(e), status_code=409)
    return model_response(FlowCallbackResponse(**status.model_dump(), provider_name=provider_name))


def abort_flow(flow_id: str) -> Response:
    """Abandon a sign-in flow (the dialog was closed); always answers ok."""
    # Abort is what a closed modal calls on its way out, so it has to succeed even when the
    # flow it is abandoning is in a bad state -- a folder deleted underneath it, an unreadable
    # index. A 500 here reaches a UI that has already gone, and the user sees a failed request
    # for something they did not ask for.
    try:
        get_state().auth_flows.abort(flow_id)
    except (accounts.AccountError, OSError) as e:
        logger.warning("Aborting sign-in flow {} did not unwind cleanly: {}", flow_id, e)
    return model_response(OkResponse())


def delete_account(account_id: str) -> Response:
    """Remove an account. Chats bound to it keep their transcripts and stop taking messages until
    they move to another provider. The confirmation is the client's job."""
    try:
        accounts.delete_account(account_id)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    return model_response(OkResponse())


def update_account(account_id: str) -> Response:
    """Set or clear an account's user-chosen name (`name`), or pin or unpin it as the account a
    new chat launches on (`is_default`). Either key alone is a complete request."""
    update = parse_request_body(UpdateAccountRequest)
    # A null name means "clear it", which is the empty string; a name left out is not touched.
    is_name_given = "name" in update.model_fields_set
    if not is_name_given and update.is_default is None:
        return _error_response("expected a name or is_default")
    name = update.name or ""
    if len(name) > _MAX_ACCOUNT_NAME:
        return _error_response(f"a name can be at most {_MAX_ACCOUNT_NAME} characters")
    try:
        if is_name_given:
            accounts.rename_account(account_id, name)
        if update.is_default is not None:
            accounts.set_default_account(account_id, update.is_default)
    except accounts.AccountError as e:
        return _error_response(str(e), status_code=404)
    return model_response(OkResponse())


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", "/api/lanes"): RouteModels(response_model=LanesResponse),
    ("GET", "/api/accounts"): RouteModels(response_model=AccountsResponse),
    ("POST", "/api/accounts"): RouteModels(request_model=StartFlowRequest, response_model=FlowStart),
    ("GET", "/api/accounts/flow/{flow_id}"): RouteModels(response_model=FlowStatus),
    ("POST", "/api/accounts/flow/{flow_id}"): RouteModels(request_model=SubmitFlowRequest, response_model=FlowStatus),
    ("DELETE", "/api/accounts/flow/{flow_id}"): RouteModels(response_model=OkResponse),
    ("POST", "/api/accounts/flow/{flow_id}/callback"): RouteModels(
        request_model=RelayCallbackRequest, response_model=FlowCallbackResponse
    ),
    ("DELETE", "/api/accounts/{account_id}"): RouteModels(response_model=OkResponse),
    ("PATCH", "/api/accounts/{account_id}"): RouteModels(
        request_model=UpdateAccountRequest, response_model=OkResponse
    ),
    ("GET", "/api/accounts/{account_id}/model-options"): RouteModels(response_model=ModelOptionsResponse),
}


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
    application.add_url_rule("/api/accounts/flow/<flow_id>/callback", view_func=relay_flow_callback, methods=["POST"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=delete_account, methods=["DELETE"])
    application.add_url_rule("/api/accounts/<account_id>", view_func=update_account, methods=["PATCH"])
    application.add_url_rule(
        "/api/accounts/<account_id>/model-options", view_func=account_model_options, methods=["GET"]
    )
