"""Offering a provider account's own cloud connections in place of a latchkey permission request.

A user may already have connected a service on their Anthropic or OpenAI account (a claude.ai
connector, a ChatGPT app). The workspace keeps its agents from loading those, since they reach the
service without the latchkey gateway and the permissions granted there
(``harnesses/cloud_connections.py``). When an agent asks for permission to a service the user's
account already has connected, the permission card offers the way around: turn the workspace's
one switch for them on, which every agent reads as it starts, and restart the chat's agent.

A request names its service one of two ways, and a connector is looked for accordingly. A request
for a service the gateway's catalog knows is matched by what the catalog calls the service. A
request for a custom service names only a domain (and, when it connects the service's own MCP
server, that server's endpoint), so it is matched by address where the provider tells a
connector's, and otherwise by the words of the domain.

Which connectors an account has is asked once, when the account signs in, on a thread of its own,
and kept in the account's folder; a failure leaves nothing there and no offer is ever made for the
account. Whether an agent already loads them is read off the switch and the agent's start each time.

Nothing is offered in a shared workspace: the connections are the account owner's, and an agent
there takes messages from people who are not.
"""

import re
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import ValidationError
from share_grants.agent_grants import AGENT_GRANTS_FILE
from share_grants.agent_grants import AgentGrantsError
from share_grants.agent_grants import load_active_agent_grants
from share_grants.grants import GrantsError
from share_grants.grants import load_grants

from imbue.chat import accounts
from imbue.chat.account_labels import account_id_of
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.cloud_connections import CloudConnector
from imbue.chat.harnesses.lanes import get_lane
from imbue.chat.harnesses.registry import build_cloud_connections
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.file_utils import atomic_write

logger = _loguru_logger

# Kept in the account's folder, beside the stamp of when it signed in.
CONNECTED_FILENAME: Final = ".cloud_connections.json"
# Relative to the workspace root every supervised program runs from.
DEFAULT_SHARE_GRANTS_PATH: Final = Path("data/.secrets/share_grants.toml")
DEFAULT_AGENT_GRANTS_PATH: Final = AGENT_GRANTS_FILE

# A name this short is part of too many others to mean anything by being found in one.
_MIN_MATCHED_NAME_LENGTH: Final = 3
# The transport a catalog key or a display name is suffixed with: "notion-mcp", "Notion (MCP)", "GitHub (REST API)".
_KEY_TRANSPORT_SUFFIX: Final = "-mcp"
_PARENTHESIZED_SUFFIX_RE: Final = re.compile(r"\s*\([^)]*\)\s*$")
_WORD_RE: Final = re.compile(r"[a-z0-9]+")
# Words of a connector's name that say nothing about which service it is for: "Monday.com", "Linear App", "Notion MCP".
_GENERIC_WORDS: Final = frozenset(
    ("ai", "api", "apis", "app", "apps", "cloud", "co", "com", "connector", "dev", "io", "mcp", "net", "org", "www")
)


class ConnectedConnectors(FrozenModel):
    """The connectors an account had connected on its provider's side when it last signed in."""

    connectors: tuple[CloudConnector, ...] = Field(description="The connectors, as the provider lists them")


class RequestedService(FrozenModel):
    """What a permission request asks to reach, as far as finding a connector for it goes.

    A request for a service the gateway's catalog knows carries ``names``; a request for a custom
    service carries ``domain``, and ``mcp_endpoint`` when it connects the service's own MCP server.
    """

    names: tuple[str, ...] = Field(
        default=(),
        description="The catalog's key for the service, then its display names "
        "(latchkey_endpoints.resolve_scope_service_names)",
    )
    domain: str | None = Field(default=None, description="The domain a custom service request names")
    mcp_endpoint: str | None = Field(
        default=None, description="The MCP server a custom service request connects (the mcp login flow)"
    )


class CloudConnectionsOffer(FrozenModel):
    """Whether a chat's permission card offers the account's own connections, whose they are, and for which one."""

    is_offered: bool = Field(description="Whether to offer turning the account's cloud connections on")
    provider_name: str | None = Field(
        default=None, description="The provider the account is with (Anthropic, OpenAI); None when nothing is offered"
    )
    connector_name: str | None = Field(
        default=None,
        description="The connector on the account taken to be for the requested service, as the provider "
        "spells it; None when nothing is offered",
    )


NO_OFFER: Final = CloudConnectionsOffer(is_offered=False)


def _connected_path(account_id: str, home: Path | None) -> Path:
    return accounts.account_dir(account_id, home) / CONNECTED_FILENAME


def read_connected(account_id: str, home: Path | None = None) -> tuple[CloudConnector, ...]:
    """The connectors recorded for the account; none for an account whose list was never read."""
    path = _connected_path(account_id, home)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ()
    except OSError as e:
        logger.warning("Could not read the cloud connections recorded at {}: {}", path, e)
        return ()
    try:
        return ConnectedConnectors.model_validate_json(text).connectors
    except ValidationError as e:
        logger.warning("The cloud connections recorded at {} are unreadable: {}", path, e)
        return ()


def record_connected(account: accounts.Account, connections: CloudConnections, home: Path | None = None) -> None:
    """Ask the provider which connectors the account has connected and keep the answer; a failure keeps nothing."""
    try:
        connectors = connections.list_connected(accounts.account_dir(account.id, home))
    except CloudConnectionsError as e:
        logger.info("Did not record the cloud connections of account {}: {}", account.id, e)
        return
    path = _connected_path(account.id, home)
    try:
        atomic_write(path, ConnectedConnectors(connectors=connectors).model_dump_json())
    except OSError as e:
        logger.warning("Could not record the cloud connections of account {} at {}: {}", account.id, path, e)
        return
    logger.info("Recorded {} cloud connection(s) of account {}", len(connectors), account.id)


def record_connected_in_background(account: accounts.Account) -> None:
    """Record the account's connectors on a thread of its own: the sign-in that calls this waits for nothing."""
    harness = accounts.harness_for(account)
    connections = build_cloud_connections(harness) if harness is not None else None
    if connections is None:
        return
    threading.Thread(
        target=record_connected,
        args=(account, connections),
        name=f"cloud-connections-{account.id[:8]}",
        daemon=True,
    ).start()


@pure
def _matchable(name: str) -> str:
    return _PARENTHESIZED_SUFFIX_RE.sub("", name).strip().lower()


@pure
def service_match_names(service_names: Sequence[str]) -> tuple[str, ...]:
    """What to look for in a connector's name, from what the service is called.

    ``service_names`` is the catalog's key for the service followed by its display names
    (``latchkey_endpoints.resolve_scope_service_names``). The key reads as words (``google-gmail``
    as "google gmail") without the transport it may end in, and a display name loses a
    parenthesized transport, so "Notion (MCP)" is looked for as "notion". The whole name is
    kept: "google" alone would find every Google connector for any Google service.
    """
    if not service_names:
        return ()
    key, *display_names = service_names
    names = [_matchable(key.removesuffix(_KEY_TRANSPORT_SUFFIX).replace("-", " "))]
    names.extend(_matchable(display_name) for display_name in display_names)
    return tuple(dict.fromkeys(name for name in names if len(name) >= _MIN_MATCHED_NAME_LENGTH))


@pure
def find_service_connector(service_names: Sequence[str], connectors: Sequence[CloudConnector]) -> str | None:
    """The first connector whose name contains one of the names the service goes by, whatever the case."""
    match_names = service_match_names(service_names)
    for connector in connectors:
        if any(match_name in connector.name.lower() for match_name in match_names):
            return connector.name
    return None


@pure
def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.rstrip(".").lower() if host else None


@pure
def _domain_words(domain: str) -> frozenset[str]:
    """The labels of a domain that can name a service: all but the last, which is only where it is registered."""
    labels = [label for label in domain.strip().rstrip(".").lower().split(".") if label]
    return frozenset(labels[:-1])


@pure
def _connector_words(name: str) -> tuple[str, ...]:
    """The words of a connector's name that say which service it is for."""
    return tuple(word for word in _WORD_RE.findall(_matchable(name)) if word not in _GENERIC_WORDS)


@pure
def _is_named_like_domain(connector_name: str, domain_words: frozenset[str]) -> bool:
    """Whether every word of the connector's name is a label of the domain, or its words run together are one.

    "ClickUp" is for ``api.clickup.com`` and "Google Drive" for ``drive.google.com``, but "Google Calendar" is
    not: a connector that names more than the domain does is for something else of the same company's. Whole
    labels only, so "Box" is not found in ``api.dropbox.com``.
    """
    words = _connector_words(connector_name)
    joined = "".join(words)
    if len(joined) < _MIN_MATCHED_NAME_LENGTH:
        return False
    return joined in domain_words or all(word in domain_words for word in words)


@pure
def find_custom_service_connector(
    domain: str, mcp_endpoint: str | None, connectors: Sequence[CloudConnector]
) -> str | None:
    """The connector for a custom service: one served from the address the request names, else one named like its domain.

    The address is the surer sign, and all there is to go by when the domain says nothing of the service
    (``mcp.example-cdn.net``); only a provider that tells its connectors' addresses can be matched by it.
    """
    requested_hosts = {host for host in (domain.strip().rstrip(".").lower(), _host_of(mcp_endpoint)) if host}
    for connector in connectors:
        if _host_of(connector.url) in requested_hosts:
            return connector.name
    domain_words = _domain_words(domain)
    for connector in connectors:
        if _is_named_like_domain(connector.name, domain_words):
            return connector.name
    return None


@pure
def find_connector(requested: RequestedService, connectors: Sequence[CloudConnector]) -> str | None:
    """The name of the connector taken to be for the requested service, or None when the account has none."""
    if requested.names:
        return find_service_connector(requested.names, connectors)
    if requested.domain:
        return find_custom_service_connector(requested.domain, requested.mcp_endpoint, connectors)
    return None


def is_workspace_shared(
    share_grants_path: Path = DEFAULT_SHARE_GRANTS_PATH,
    agent_grants_path: Path = DEFAULT_AGENT_GRANTS_PATH,
) -> bool:
    """Whether anyone but the owner may be here: a person granted the workspace or one of its apps, or a visiting agent.

    A grants document that cannot be read counts as shared, the way the gateway reading it admits nobody.
    """
    if share_grants_path.exists():
        try:
            grants = load_grants(share_grants_path)
        except GrantsError as e:
            logger.warning("Treating the workspace as shared: {} is unreadable: {}", share_grants_path, e)
            return True
        for grant_list in (grants.workspace, *grants.services.values()):
            if grant_list.users or grant_list.emails or grant_list.email_domains:
                return True
    try:
        agent_grants = load_active_agent_grants(agent_grants_path, datetime.now(timezone.utc))
    except AgentGrantsError as e:
        logger.warning("Treating the workspace as shared: {} is unreadable: {}", agent_grants_path, e)
        return True
    return bool(agent_grants)


def provider_name_of(agent_info: AgentInfo, home: Path | None = None) -> str | None:
    """The provider the agent's account is with, or None for an agent on no account the index still has."""
    account_id = account_id_of(agent_info.harness, agent_info.labels)
    if account_id is None:
        return None
    try:
        account = accounts.resolve_account(account_id, home)
    except accounts.AccountError as e:
        logger.debug("Agent {} runs on no account to offer cloud connections of: {}", agent_info.name, e)
        return None
    return get_lane(account.lane).provider_name


def is_enabled_for(agent_info: AgentInfo, connections: CloudConnections) -> bool:
    """Whether the agent already loads its account's connections; an agent whose switch cannot be read counts as
    loading them, so nothing is offered for it."""
    try:
        return connections.is_enabled(agent_info.agent_state_dir)
    except CloudConnectionsError as e:
        logger.warning("Could not tell whether agent {} loads its cloud connections: {}", agent_info.name, e)
        return True


def offer_for(
    agent_info: AgentInfo,
    requested: RequestedService,
    is_shared: bool,
    home: Path | None = None,
) -> CloudConnectionsOffer:
    """What the permission card of a request for a service offers on the chat this agent runs.

    Offered only when the agent's harness has cloud connections it does not load yet, the workspace is not
    shared, and the agent's account has a connector for the service (``find_connector``).
    """
    connections = build_cloud_connections(agent_info.harness)
    if connections is None or is_shared:
        return NO_OFFER
    account_id = account_id_of(agent_info.harness, agent_info.labels)
    provider_name = provider_name_of(agent_info, home)
    if account_id is None or provider_name is None:
        return NO_OFFER
    if is_enabled_for(agent_info, connections):
        return NO_OFFER
    connector_name = find_connector(requested, read_connected(account_id, home))
    if connector_name is None:
        return NO_OFFER
    return CloudConnectionsOffer(is_offered=True, provider_name=provider_name, connector_name=connector_name)
