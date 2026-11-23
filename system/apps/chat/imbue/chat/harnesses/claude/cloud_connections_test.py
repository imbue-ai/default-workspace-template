"""Claude's cloud connections: the connector list read with the account's own sign-in, over a scripted
transport in place of Anthropic's endpoint."""

import json
from pathlib import Path

import httpx
import pytest

from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.cloud_connections import CloudConnector


def _sign_in(account_dir: Path, scopes: list[str]) -> None:
    credentials = {"claudeAiOauth": {"accessToken": "token-of-the-account", "scopes": scopes}}
    (account_dir / ".credentials.json").write_text(json.dumps(credentials))


def test_the_list_is_asked_as_the_account_and_holds_each_connector_once_with_its_address(tmp_path: Path) -> None:
    _sign_in(tmp_path, ["user:inference", "user:mcp_servers"])
    requests: list[httpx.Request] = []
    pages = {
        None: {
            "data": [{"display_name": "Slack", "url": "https://mcp.slack.com/mcp"}, {"display_name": "Gmail"}],
            "next_page": "p2",
        },
        "p2": {"data": [{"display_name": "Slack"}, {"display_name": ""}, {"url": "https://nameless"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("page")])

    connectors = ClaudeCloudConnections.over_transport(httpx.MockTransport(handler)).list_connected(tmp_path)

    assert connectors == (CloudConnector(name="Slack", url="https://mcp.slack.com/mcp"), CloudConnector(name="Gmail"))
    assert [request.url.path for request in requests] == ["/v1/mcp_servers", "/v1/mcp_servers"]
    assert requests[0].headers["authorization"] == "Bearer token-of-the-account"
    assert requests[0].headers["anthropic-beta"] == "mcp-servers-2025-12-04"


def test_an_account_with_no_subscription_sign_in_is_never_asked_about(tmp_path: Path) -> None:
    """An API key or a long-lived token leaves no claude.ai sign-in in the folder, and a sign-in without the
    connector scope would only be refused: neither sends the token anywhere."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    connections = ClaudeCloudConnections.over_transport(httpx.MockTransport(handler))
    with pytest.raises(CloudConnectionsError, match="holds no claude.ai sign-in"):
        connections.list_connected(tmp_path)
    _sign_in(tmp_path, ["user:inference"])
    with pytest.raises(CloudConnectionsError, match="cannot read the account's connectors"):
        connections.list_connected(tmp_path)


def test_a_refusal_from_the_endpoint_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    _sign_in(tmp_path, ["user:mcp_servers"])
    transport = httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": "expired"}))
    with pytest.raises(CloudConnectionsError, match="401"):
        ClaudeCloudConnections.over_transport(transport).list_connected(tmp_path)
