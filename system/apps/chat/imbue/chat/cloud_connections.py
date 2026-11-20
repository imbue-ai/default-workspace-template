"""Offering a provider account's own cloud connections in place of a latchkey permission request.

A user may already have connected a service on their Anthropic or OpenAI account (a claude.ai
connector, a ChatGPT app). The workspace keeps its agents from loading those, since they reach the
service without the latchkey gateway and the permissions granted there
(``harnesses/cloud_connections.py``). When an agent asks for permission to a service the user's
account already has connected, the permission card offers the way around: turn the workspace's
one switch for them on, which every agent reads as it starts, and restart the chat's agent.

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


class ConnectedConnectors(FrozenModel):
    """The connectors an account had connected on its provider's side when it last signed in."""

    names: tuple[str, ...] = Field(description="The connectors' display names, as the provider spells them")


class CloudConnectionsOffer(FrozenModel):
    """Whether a chat's permission card offers the account's own connections, and whose they are."""

    is_offered: bool = Field(description="Whether to offer turning the account's cloud connections on")
    provider_name: str | None = Field(
        default=None, description="The provider the account is with (Anthropic, OpenAI); None when nothing is offered"
    )


NO_OFFER: Final = CloudConnectionsOffer(is_offered=False)


def _connected_path(account_id: str, home: Path | None) -> Path:
    return accounts.account_dir(account_id, home) / CONNECTED_FILENAME


def read_connected(account_id: str, home: Path | None = None) -> tuple[str, ...]:
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
        return ConnectedConnectors.model_validate_json(text).names
    except ValidationError as e:
        logger.warning("The cloud connections recorded at {} are unreadable: {}", path, e)
        return ()


def record_connected(account: accounts.Account, connections: CloudConnections, home: Path | None = None) -> None:
    """Ask the provider which connectors the account has connected and keep the answer; a failure keeps nothing."""
    try:
        names = connections.list_connected(accounts.account_dir(account.id, home))
    except CloudConnectionsError as e:
        logger.info("Did not record the cloud connections of account {}: {}", account.id, e)
        return
    path = _connected_path(account.id, home)
    try:
        atomic_write(path, ConnectedConnectors(names=names).model_dump_json())
    except OSError as e:
        logger.warning("Could not record the cloud connections of account {} at {}: {}", account.id, path, e)
        return
    logger.info("Recorded {} cloud connection(s) of account {}", len(names), account.id)


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
def is_service_connected(service_names: Sequence[str], connected_names: Sequence[str]) -> bool:
    """Whether any connector's name contains one of the names the service goes by, whatever the case."""
    match_names = service_match_names(service_names)
    return any(match_name in connected.lower() for connected in connected_names for match_name in match_names)


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
    service_names: Sequence[str],
    is_shared: bool,
    home: Path | None = None,
) -> CloudConnectionsOffer:
    """What the permission card of a request for a service offers on the chat this agent runs.

    Offered only when the agent's harness has cloud connections it does not load yet, the workspace is not
    shared, and the agent's account has a connector named like the service.
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
    if not is_service_connected(service_names, read_connected(account_id, home)):
        return NO_OFFER
    return CloudConnectionsOffer(is_offered=True, provider_name=provider_name)
