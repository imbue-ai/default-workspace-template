"""Claude's cloud connections: the claude.ai connectors of a subscription account.

Claude Code fetches the connectors of the claude.ai account it is signed in to and loads each as
an MCP server Anthropic hosts, unless ``ENABLE_CLAUDEAI_MCP_SERVERS`` says otherwise in its
environment when it starts. The workspace sets that to ``false`` in the host's env file
(``.mngr/settings.toml``), so an agent loads them only once its own env file says ``true``.

The list is read from the endpoint Claude Code itself reads it from, with the access token a
subscription sign-in (``claude auth login``) leaves in the account folder. An account signed in
any other way (an API key, a long-lived token) holds no such token and has no connectors to list.
"""

import json
from pathlib import Path
from typing import Any
from typing import Final

import httpx

from imbue.chat.harnesses.claude.agent_env import read_agent_env_var
from imbue.chat.harnesses.claude.agent_env import rewrite_agent_env_var
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError

CLAUDEAI_MCP_SERVERS_ENV_VAR: Final = "ENABLE_CLAUDEAI_MCP_SERVERS"
# The values Claude Code reads as "off"; anything else, and the variable unset, leaves the connectors on.
_DISABLING_VALUES: Final = frozenset(("0", "false", "no", "off"))

_CREDENTIALS_FILENAME: Final = ".credentials.json"
_CONNECTORS_URL: Final = "https://api.anthropic.com/v1/mcp_servers"
_CONNECTORS_BETA: Final = "mcp-servers-2025-12-04"
_ANTHROPIC_VERSION: Final = "2023-06-01"
# Without this scope the endpoint refuses the token, and Claude Code itself skips the fetch.
_CONNECTORS_SCOPE: Final = "user:mcp_servers"
_PAGE_SIZE: Final = 1000
# An account's connectors fit one page; the cap only bounds a server that keeps answering with a next page.
_MAX_PAGES: Final = 5
_REQUEST_TIMEOUT_SECONDS: Final = 30.0


def _read_access_token(account_dir: Path) -> str:
    """The claude.ai access token a subscription sign-in left in the account folder."""
    credentials_path = account_dir / _CREDENTIALS_FILENAME
    try:
        text = credentials_path.read_text()
    except OSError as e:
        raise CloudConnectionsError(f"the account holds no claude.ai sign-in at {credentials_path}: {e}") from e
    try:
        credentials = json.loads(text)
    except json.JSONDecodeError as e:
        raise CloudConnectionsError(f"{credentials_path} is not JSON: {e}") from e
    oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    if not isinstance(oauth, dict):
        raise CloudConnectionsError(f"{credentials_path} holds no claude.ai sign-in")
    token = oauth.get("accessToken")
    scopes = oauth.get("scopes")
    if not isinstance(token, str) or not token:
        raise CloudConnectionsError(f"{credentials_path} holds no access token")
    if not isinstance(scopes, list) or _CONNECTORS_SCOPE not in scopes:
        raise CloudConnectionsError(f"the sign-in at {credentials_path} cannot read the account's connectors")
    return token


def _connector_names(page: Any) -> list[str]:
    """The display names one page of the connector list carries."""
    entries = page.get("data") if isinstance(page, dict) else None
    if not isinstance(entries, list):
        raise CloudConnectionsError("the connector list is not shaped as expected")
    return [
        entry["display_name"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("display_name"), str) and entry["display_name"]
    ]


class ClaudeCloudConnections(CloudConnections):
    """The claude.ai connectors of the account, and the env line that makes one agent load them."""

    # What the list is fetched over; None for httpx's own, the network. A test hands in a scripted one.
    _transport: httpx.BaseTransport | None = None

    @classmethod
    def over_transport(cls, transport: httpx.BaseTransport) -> "ClaudeCloudConnections":
        connections = cls()
        connections._transport = transport
        return connections

    def list_connected(self, account_dir: Path) -> tuple[str, ...]:
        headers = {
            "Authorization": f"Bearer {_read_access_token(account_dir)}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": _CONNECTORS_BETA,
        }
        names: list[str] = []
        params: dict[str, str] = {"limit": str(_PAGE_SIZE)}
        with httpx.Client(transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for _ in range(_MAX_PAGES):
                try:
                    response = client.get(_CONNECTORS_URL, headers=headers, params=params)
                except httpx.HTTPError as e:
                    raise CloudConnectionsError(f"the connector list could not be fetched: {e}") from e
                if response.status_code != 200:
                    raise CloudConnectionsError(f"the connector list answered {response.status_code}")
                try:
                    page = response.json()
                except json.JSONDecodeError as e:
                    raise CloudConnectionsError(f"the connector list is not JSON: {e}") from e
                names.extend(_connector_names(page))
                next_page = page.get("next_page")
                if not isinstance(next_page, str) or not next_page:
                    break
                params = {"limit": str(_PAGE_SIZE), "page": next_page}
        return tuple(dict.fromkeys(names))

    def is_enabled(self, agent_state_dir: Path) -> bool:
        try:
            value = read_agent_env_var(agent_state_dir, CLAUDEAI_MCP_SERVERS_ENV_VAR)
        except OSError as e:
            raise CloudConnectionsError(f"the env files of the agent at {agent_state_dir} are unreadable: {e}") from e
        return value is None or value.strip().lower() not in _DISABLING_VALUES

    def enable(self, agent_state_dir: Path) -> None:
        try:
            rewrite_agent_env_var(agent_state_dir, CLAUDEAI_MCP_SERVERS_ENV_VAR, "true")
        except OSError as e:
            raise CloudConnectionsError(
                f"the env file of the agent at {agent_state_dir} could not be written: {e}"
            ) from e
