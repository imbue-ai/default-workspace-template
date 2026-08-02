"""Tunnel / hostname naming conventions shared by auth and forwarding.

Names are ``<user_id_prefix>--<agent-prefix>`` (tunnels) and
``<service>--<agent-prefix>--<user_id_prefix>.<domain>`` (hostnames).
"""

from imbue.remote_service_connector.errors import InvalidTunnelComponentError
from imbue.remote_service_connector.errors import TunnelComponentTooLongError
from imbue.remote_service_connector.errors import TunnelOwnershipError

TUNNEL_NAME_SEP = "--"


_MAX_USER_ID_PREFIX_LENGTH = 22
_MAX_SERVICE_NAME_LENGTH = 21
_AGENT_ID_PREFIX_LENGTH = 16


def truncate_agent_id(agent_id: str) -> str:
    """Truncate an agent ID to a short prefix for use in hostnames.

    Strips the "agent-" prefix (if present) and takes the first 16 hex chars.
    16 chars of hex provides sufficient uniqueness per user.
    """
    raw = agent_id.removeprefix("agent-")
    return raw[:_AGENT_ID_PREFIX_LENGTH]


def _validate_user_id_prefix(user_id_prefix: str) -> None:
    if TUNNEL_NAME_SEP in user_id_prefix:
        raise InvalidTunnelComponentError("User ID prefix", user_id_prefix, TUNNEL_NAME_SEP)
    if len(user_id_prefix) > _MAX_USER_ID_PREFIX_LENGTH:
        raise TunnelComponentTooLongError("User ID prefix", user_id_prefix, _MAX_USER_ID_PREFIX_LENGTH)


def _validate_service_name(service_name: str) -> None:
    if TUNNEL_NAME_SEP in service_name:
        raise InvalidTunnelComponentError("Service name", service_name, TUNNEL_NAME_SEP)
    if len(service_name) > _MAX_SERVICE_NAME_LENGTH:
        raise TunnelComponentTooLongError("Service name", service_name, _MAX_SERVICE_NAME_LENGTH)


def make_tunnel_name(user_id_prefix: str, agent_id: str) -> str:
    _validate_user_id_prefix(user_id_prefix)
    short_id = truncate_agent_id(agent_id)
    return f"{user_id_prefix}{TUNNEL_NAME_SEP}{short_id}"


def make_hostname(service_name: str, agent_id: str, user_id_prefix: str, domain: str) -> str:
    _validate_service_name(service_name)
    short_id = truncate_agent_id(agent_id)
    return f"{service_name}--{short_id}--{user_id_prefix}.{domain}"


def extract_agent_id_prefix(tunnel_name: str, user_id_prefix: str) -> str:
    """Extract the truncated agent ID prefix from a tunnel name."""
    prefix = f"{user_id_prefix}{TUNNEL_NAME_SEP}"
    if not tunnel_name.startswith(prefix):
        raise TunnelOwnershipError(tunnel_name, user_id_prefix)
    return tunnel_name[len(prefix) :]


def extract_service_name(hostname: str, agent_id_prefix: str, user_id_prefix: str, domain: str) -> str | None:
    expected_suffix = f"--{agent_id_prefix}--{user_id_prefix}.{domain}"
    if not hostname.endswith(expected_suffix):
        return None
    return hostname[: -len(expected_suffix)]


def extract_user_id_prefix_from_tunnel_name(tunnel_name: str) -> str:
    """Extract the user-id-prefix portion from a tunnel name."""
    parts = tunnel_name.split(TUNNEL_NAME_SEP, 1)
    return parts[0]
