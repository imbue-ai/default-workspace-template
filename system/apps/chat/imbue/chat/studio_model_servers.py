"""Model servers on the user's own computer, reached through the Imbue Studio app.

A workspace that does not run on the user's computer has no route to a model server there
(Ollama, LM Studio, llama.cpp). The Imbue Studio app has: it lists the servers it finds on that
computer, and opens a tunnel that makes one of them answer on a loopback port inside this
workspace. Both are routes of the app, called through the latchkey gateway's ``minds-api-proxy``
as this workspace's own agent; the model traffic itself goes through the tunnel, not the gateway.

Reaching the computer is a permission the user grants once per workspace. A workspace without it
is refused (403), and :func:`find_studio_model_servers` then files the ``local-model`` permission
request, which the app shows the user as its approval dialog. Nothing here can tell whether a
request filed earlier is still waiting on the user (a workspace's gateway gives each request an
id of its own and does not list them back), so the caller decides when to ask again.
"""

from enum import auto
from typing import Any
from typing import Final

import httpx
from latchkey_access.gateway import GatewayAccess
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model import LocalModelServer
from imbue.chat.harnesses.pi_coding.local_model import MIN_CONTEXT_WINDOW
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModel
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModelChoice
from imbue.chat.harnesses.pi_coding.local_model_catalog import group_model_choices
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

_PERMISSION_REQUEST_TYPE: Final = "local-model"
_PERMISSION_RATIONALE: Final = "Run this workspace's chats on a model server on your computer."

# The app looks at each known port of the computer before it answers.
_SERVERS_TIMEOUT_SECONDS: Final = 20.0
# Opening the tunnel may have to open a connection into the workspace first.
_TUNNEL_TIMEOUT_SECONDS: Final = 45.0

# What the app or the gateway answers when neither has heard of these routes or this request
# type: an app from before them, which is treated as an app that cannot be asked at all.
_UNSUPPORTED_STATUSES: Final = frozenset({400, 404, 405, 501})

STUDIO_UNREACHABLE_MESSAGE: Final = (
    "Imbue Studio on your computer did not answer. Check that the computer is awake and Studio is open."
)


class StudioLink(UpperCaseStrEnum):
    """Where this workspace stands with the Imbue Studio app's model servers."""

    # The app cannot be asked: no gateway, or an app from before it could.
    UNAVAILABLE = auto()
    # The app answered with the servers on the user's computer.
    CONNECTED = auto()
    # The user has not yet let this workspace reach their computer.
    AWAITING_APPROVAL = auto()
    # The app did not answer: the computer is asleep or offline, or the app is closed.
    UNREACHABLE = auto()


class StudioModelServers(FrozenModel):
    """What asking the Imbue Studio app for the user's model servers came to."""

    link: StudioLink = Field(description="Whether the app answered, and if not, why")
    servers: tuple[LocalModelServer, ...] = Field(default=(), description="The servers on the user's computer")


def _agent_route(gateway: GatewayAccess, agent_id: str, route: str) -> str:
    return gateway.url(f"/minds-api-proxy/api/v1/agents/{agent_id}/local-model/{route}")


def _to_described_models(entries: Any) -> tuple[LocalModel, ...] | None:
    """What the app says of a server's models; None from an app that says nothing, or for a server that is not Ollama."""
    if not isinstance(entries, list):
        return None
    try:
        return tuple(LocalModel.model_validate(entry) for entry in entries)
    except ValidationError as e:
        logger.warning("Imbue Studio described a server's models in a form this workspace cannot read: {}", e)
        return None


def _to_server(entry: Any) -> LocalModelServer | None:
    fields = entry if isinstance(entry, dict) else {}
    port, name, models = fields.get("port"), fields.get("name"), fields.get("models")
    if not isinstance(port, int) or not isinstance(name, str) or not isinstance(models, list):
        logger.warning(
            "Skipped a model server Imbue Studio listed in a form this workspace cannot read: {}", repr(entry)[:200]
        )
        return None
    model_ids = tuple(model for model in models if isinstance(model, str))
    described = _to_described_models(fields.get("described_models"))
    return LocalModelServer(
        base_url=f"http://127.0.0.1:{port}/v1",
        name=name,
        models=model_ids,
        choices=group_model_choices(described, MIN_CONTEXT_WINDOW)
        if described is not None
        else tuple(LocalModelChoice(model_id=model_id) for model_id in model_ids),
        studio_port=port,
    )


def _request_access(client: httpx.Client, gateway: GatewayAccess, agent_id: str) -> StudioLink:
    """File the permission request, and say where that leaves the workspace."""
    try:
        response = client.post(
            gateway.url("/permission-requests"),
            json={
                "agent_id": agent_id,
                "type": _PERMISSION_REQUEST_TYPE,
                "payload": {},
                "rationale": _PERMISSION_RATIONALE,
            },
            headers=gateway.headers(),
            timeout=_SERVERS_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.debug("Could not ask Imbue Studio for the model server permission: {}", e)
        return StudioLink.UNREACHABLE
    if response.is_success:
        return StudioLink.AWAITING_APPROVAL
    logger.debug(
        "The gateway answered {} to the model server permission request: {}", response.status_code, response.text[:200]
    )
    return StudioLink.UNAVAILABLE if response.status_code in _UNSUPPORTED_STATUSES else StudioLink.UNREACHABLE


def find_studio_model_servers(
    client: httpx.Client, gateway: GatewayAccess | None, agent_id: str, is_requesting_access: bool
) -> StudioModelServers:
    """Ask the Imbue Studio app which model servers run on the user's computer.

    A workspace the user has not let reach their computer gets no servers; with
    ``is_requesting_access`` the request for that is filed first, so the app is showing its approval
    dialog by the time this returns.
    """
    if gateway is None or not agent_id:
        return StudioModelServers(link=StudioLink.UNAVAILABLE)
    try:
        response = client.get(
            _agent_route(gateway, agent_id, "servers"), headers=gateway.headers(), timeout=_SERVERS_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as e:
        logger.debug("Could not ask Imbue Studio for its model servers: {}", e)
        return StudioModelServers(link=StudioLink.UNREACHABLE)
    if response.status_code == 403:
        if not is_requesting_access:
            return StudioModelServers(link=StudioLink.AWAITING_APPROVAL)
        return StudioModelServers(link=_request_access(client, gateway, agent_id))
    if response.status_code in _UNSUPPORTED_STATUSES:
        return StudioModelServers(link=StudioLink.UNAVAILABLE)
    if not response.is_success:
        logger.debug("Imbue Studio answered {} to the model servers listing", response.status_code)
        return StudioModelServers(link=StudioLink.UNREACHABLE)
    try:
        payload = response.json()
    except ValueError as e:
        logger.warning("Imbue Studio's model servers listing is not JSON: {}", e)
        return StudioModelServers(link=StudioLink.UNREACHABLE)
    entries = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        logger.warning("Imbue Studio's model servers listing names no servers: {}", response.text[:200])
        return StudioModelServers(link=StudioLink.UNREACHABLE)
    servers = tuple(server for server in map(_to_server, entries) if server is not None)
    # A listing none of whose entries can be read is not a computer that runs no model server.
    if entries and not servers:
        return StudioModelServers(link=StudioLink.UNREACHABLE)
    return StudioModelServers(link=StudioLink.CONNECTED, servers=servers)


def open_studio_tunnel(client: httpx.Client, gateway: GatewayAccess | None, agent_id: str, studio_port: int) -> str:
    """Have the Imbue Studio app make the model server on ``studio_port`` of the user's computer answer
    inside this workspace, and return the address it answers on here.

    Raises LocalModelError, worded for the user, when the app refuses or cannot be reached.
    """
    if gateway is None or not agent_id:
        raise LocalModelError("This workspace has no connection to Imbue Studio.")
    try:
        response = client.post(
            _agent_route(gateway, agent_id, "tunnel"),
            json={"port": studio_port},
            headers=gateway.headers(),
            timeout=_TUNNEL_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise LocalModelError(STUDIO_UNREACHABLE_MESSAGE) from e
    if response.status_code == 403:
        raise LocalModelError(
            "This workspace is no longer allowed to reach the model server on your computer. "
            "Choose Local model again to ask for it."
        )
    try:
        payload = response.json() if response.is_success else None
    except ValueError as e:
        logger.warning("Imbue Studio's answer to opening the model tunnel is not JSON: {}", e)
        payload = None
    workspace_port = payload.get("workspace_port") if isinstance(payload, dict) else None
    if not isinstance(workspace_port, int):
        logger.debug(
            "Imbue Studio answered {} to opening the model tunnel: {}", response.status_code, response.text[:200]
        )
        raise LocalModelError(
            "Imbue Studio could not connect this workspace to the model server on your computer. "
            "Check that the computer is awake and Studio is open, then try again."
        )
    return f"http://127.0.0.1:{workspace_port}/v1"
