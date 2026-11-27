"""Claude's cloud connections: the claude.ai connectors of a subscription account.

Claude Code fetches the connectors of the claude.ai account it is signed in to and loads each as
an MCP server Anthropic hosts, unless ``ENABLE_CLAUDEAI_MCP_SERVERS`` says otherwise in its
environment when it starts: the workspace's switch (``harnesses/cloud_connections.py``).

The list is read from the endpoint Claude Code itself reads it from, with the access token a
subscription sign-in (``claude auth login``) leaves in the account folder. An account signed in
any other way (an API key, a long-lived token) holds no such token and has no connectors to list.
"""

import json
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final

import httpx

from imbue.chat.harnesses.claude.tap import CLAUDE_PROCESS_STARTED_MARKER_FILENAME
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.cloud_connections import CloudConnector

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


def _connectors(page: Any) -> list[CloudConnector]:
    """The connectors one page of the list carries: each one's display name, and its server's address when given."""
    entries = page.get("data") if isinstance(page, dict) else None
    if not isinstance(entries, list):
        raise CloudConnectionsError("the connector list is not shaped as expected")
    connectors: list[CloudConnector] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("display_name")
        if not isinstance(name, str) or not name:
            continue
        url = entry.get("url")
        connectors.append(CloudConnector(name=name, url=url if isinstance(url, str) and url else None))
    return connectors


class ClaudeCloudConnections(CloudConnections):
    """The claude.ai connectors of the account."""

    process_started_marker_filename: ClassVar[str] = CLAUDE_PROCESS_STARTED_MARKER_FILENAME
    # Claude Code loads the connectors unless the variable turns them off.
    is_on_when_unset: ClassVar[bool] = True

    # What the list is fetched over; None for httpx's own, the network. A test hands in a scripted one.
    _transport: httpx.BaseTransport | None = None

    @classmethod
    def over_transport(cls, transport: httpx.BaseTransport) -> "ClaudeCloudConnections":
        connections = cls()
        connections._transport = transport
        return connections

    def list_connected(self, account_dir: Path) -> tuple[CloudConnector, ...]:
        headers = {
            "Authorization": f"Bearer {_read_access_token(account_dir)}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": _CONNECTORS_BETA,
        }
        connectors: list[CloudConnector] = []
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
                connectors.extend(_connectors(page))
                next_page = page.get("next_page")
                if not isinstance(next_page, str) or not next_page:
                    break
                params = {"limit": str(_PAGE_SIZE), "page": next_page}
        # One per display name: the first one listed.
        first_by_name: dict[str, CloudConnector] = {}
        for connector in connectors:
            first_by_name.setdefault(connector.name, connector)
        return tuple(first_by_name.values())
