"""HTTP endpoints for `/api/chats/<chat_id>/cloud-connections`: the permission card's offer of the account's own
cloud connections, and taking it.

The card asks the GET whether to make the offer for the service its request names
(``cloud_connections.offer_for``). Taking it is the POST: the workspace's switch for its
agents' cloud connections is turned on, the permission request is withdrawn from the gateway, the
chat's agent is restarted (an agent reads the switch only as it starts), and it is told what
happened in a notice the page draws no row for. The switch is written first and stays written
whatever follows, so a restart that fails here still takes effect on the agent's next start. Every
other agent in the workspace gets the connections of its own account when it next starts too.

Kept out of server.py like the other endpoint modules; what it needs from the router -- a chat's
agent, restarting it, and sending it a message -- comes through the ``CloudConnectionsChatBridge``
(``cloud_connections_bridge``) the router puts on the app state.
"""

from __future__ import annotations

import os
from typing import Final

import httpx
from app_schema.api_schema import AlternateResponse
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from flask import request
from latchkey_access.gateway import GatewayAccess
from loguru import logger as _loguru_logger
from pydantic import Field
from workspace_layout.identity import IDENTITY_HEADER

from imbue.chat.cloud_connections import CloudConnectionsOffer
from imbue.chat.cloud_connections import NO_OFFER
from imbue.chat.cloud_connections import is_workspace_shared
from imbue.chat.cloud_connections import offer_for
from imbue.chat.cloud_connections import provider_name_of
from imbue.chat.cloud_connections_bridge import ChatSendRefusedError
from imbue.chat.cloud_connections_bridge import CloudConnectionsChatBridge
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.message_display import format_cloud_connections_notice
from imbue.chat.harnesses.registry import build_cloud_connections
from imbue.chat.identity import forbid_unless_owner
from imbue.chat.identity import is_owner_identity
from imbue.chat.latchkey_endpoints import resolve_scope_service_names
from imbue.chat.models import AgentRestartError
from imbue.chat.models import ConvergingResponse
from imbue.chat.state import get_state
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

OFFER_ROUTE: Final[str] = "/api/chats/<chat_id>/cloud-connections"
ENABLE_ROUTE: Final[str] = "/api/chats/<chat_id>/cloud-connections/enable"
_SCOPE_PARAMETER: Final[str] = "scope"
# A request filed for any of the user's devices is addressed the same way when it is withdrawn.
_DEVICE_HEADER: Final[str] = "X-Latchkey-Device"
_ANY_DEVICE: Final[str] = "*"


class EnableCloudConnectionsBody(FrozenModel):
    """Request body for POST /api/chats/{chat_id}/cloud-connections/enable."""

    # The id goes into the gateway path that withdraws the request, so it is held to an id's shape.
    request_id: str = Field(
        min_length=1, pattern=r"^[A-Za-z0-9_-]+$", description="The permission request the offer was made on"
    )


class EnableCloudConnectionsResponse(FrozenModel):
    """What taking the offer did, beyond turning the workspace's switch on and restarting the chat's agent."""

    is_request_withdrawn: bool = Field(description="Whether the permission request was removed from the gateway")
    is_notice_delivered: bool = Field(description="Whether the restarted agent was told what happened")


class CloudConnectionsBridgeMissingError(RuntimeError):
    """Raised when a route runs on an app whose router never attached its bridge."""


def _bridge() -> CloudConnectionsChatBridge:
    bridge = get_state().cloud_connections_bridge
    if bridge is None:
        raise CloudConnectionsBridgeMissingError("The cloud-connection routes have no chat bridge attached")
    return bridge


def _chat_not_found(chat_id: str) -> Response:
    return error_response(f"Chat '{chat_id}' not found", 404)


def _service_names_of(scope: str) -> tuple[str, ...]:
    """What the gateway's catalog calls the service behind ``scope``; nothing when it cannot be asked."""
    gateway = GatewayAccess.from_environ(os.environ)
    if gateway is None:
        return ()
    state = get_state()
    try:
        with state.latchkey_lock:
            return resolve_scope_service_names(
                state.latchkey_http_client, gateway, scope, state.latchkey_catalog_cache
            )
    except httpx.HTTPError as e:
        logger.warning("Could not look up the service of scope {!r} for a cloud-connections offer: {}", scope, e)
        return ()


def get_offer(chat_id: str) -> Response:
    """GET /api/chats/{chat_id}/cloud-connections?scope=... -- whether the card of a request for ``scope`` makes the offer.

    Never offered to a visitor, on a secondary chat, or for a scope the gateway's catalog does not know.
    """
    agent_info = _bridge().find_agent(chat_id)
    if agent_info is None:
        return _chat_not_found(chat_id)
    scope = request.args.get(_SCOPE_PARAMETER, "").strip()
    if not scope or get_state().is_secondary or not is_owner_identity(request.headers.get(IDENTITY_HEADER)):
        return model_response(NO_OFFER)
    return model_response(offer_for(agent_info, _service_names_of(scope), is_workspace_shared()))


def _withdraw_permission_request(request_id: str) -> bool:
    """Remove a pending permission request from the gateway, which tells Imbue Studio it is gone; False when it stays."""
    gateway = GatewayAccess.from_environ(os.environ)
    if gateway is None:
        logger.warning("Could not withdraw permission request {}: no latchkey gateway is configured", request_id)
        return False
    headers = {**gateway.headers(), _DEVICE_HEADER: _ANY_DEVICE}
    try:
        response = get_state().latchkey_http_client.delete(
            gateway.url(f"/permission-requests/{request_id}"), headers=headers
        )
    except httpx.HTTPError as e:
        logger.warning("Could not withdraw permission request {}: {}", request_id, e)
        return False
    if response.is_success:
        return True
    logger.warning(
        "Could not withdraw permission request {}: the gateway answered {}", request_id, response.status_code
    )
    return False


def _send(chat_id: str, text: str, what: str) -> bool:
    try:
        _bridge().send(chat_id, text)
    except ChatSendRefusedError as e:
        logger.warning("{} did not reach chat {} after its cloud connections were turned on: {}", what, chat_id, e)
        return False
    return True


def enable(chat_id: str) -> Response:
    """POST /api/chats/{chat_id}/cloud-connections/enable -- take the offer the card of ``request_id`` made.

    403 for a visitor and in a shared workspace; 404 for an unknown chat; 400 for a chat whose harness or account
    has no cloud connections; 409 for a secondary chat, a chat already loading them, and a chat that is switching
    agents; 500 when the switch could not be written or the agent would not restart.
    """
    forbidden = forbid_unless_owner()
    if forbidden is not None:
        return forbidden
    body = parse_request_body(EnableCloudConnectionsBody)
    if get_state().is_secondary:
        return error_response("This is a preview of the chat; turn the connections on in the live chat.", 409)
    bridge = _bridge()
    agent_info = bridge.find_agent(chat_id)
    if agent_info is None:
        return _chat_not_found(chat_id)
    connections = build_cloud_connections(agent_info.harness)
    provider_name = provider_name_of(agent_info)
    if connections is None or provider_name is None:
        return error_response("This chat's account has no cloud connections to turn on.", 400)
    if is_workspace_shared():
        return error_response("Cloud connections cannot be turned on in a shared workspace.", 403)
    try:
        is_enabled = connections.is_enabled(agent_info.agent_state_dir)
    except CloudConnectionsError as e:
        logger.warning("Could not tell whether chat {} loads its cloud connections: {}", chat_id, e)
        return error_response("The chat's cloud connections could not be turned on.", 500)
    if is_enabled:
        return error_response("This chat already uses its account's cloud connections.", 409)
    refusal = bridge.refuse_restart(chat_id, agent_info)
    if refusal is not None:
        return refusal

    try:
        connections.enable(agent_info.agent_state_dir)
    except CloudConnectionsError as e:
        logger.warning("Could not turn on the cloud connections of chat {}: {}", chat_id, e)
        return error_response("The chat's cloud connections could not be turned on.", 500)
    logger.info("Turned on the {} cloud connections of chat {} (agent {})", provider_name, chat_id, agent_info.name)
    is_request_withdrawn = _withdraw_permission_request(body.request_id)
    try:
        queued_block = bridge.restart(chat_id, agent_info)
    except AgentRestartError as e:
        return error_response(str(e), 500)
    notice = format_cloud_connections_notice(provider_name, body.request_id)
    is_notice_delivered = _send(chat_id, notice, "The notice")
    if queued_block:
        _send(chat_id, queued_block, "The queued messages")
    return model_response(
        EnableCloudConnectionsResponse(
            is_request_withdrawn=is_request_withdrawn, is_notice_delivered=is_notice_delivered
        )
    )


ROUTE_TABLE: Final[RouteTable] = {
    ("GET", "/api/chats/{chat_id}/cloud-connections"): RouteModels(response_model=CloudConnectionsOffer),
    ("POST", "/api/chats/{chat_id}/cloud-connections/enable"): RouteModels(
        request_model=EnableCloudConnectionsBody,
        response_model=EnableCloudConnectionsResponse,
        alternate_responses=(AlternateResponse(status=409, model=ConvergingResponse),),
    ),
}


def register_routes(application: Flask) -> None:
    """Wire the cloud-connection endpoints onto the Flask application.

    The router attaches its :class:`CloudConnectionsChatBridge` to the app state before either serves a request.
    """
    application.add_url_rule(OFFER_ROUTE, view_func=get_offer, methods=["GET"])
    application.add_url_rule(ENABLE_ROUTE, view_func=enable, methods=["POST"])
