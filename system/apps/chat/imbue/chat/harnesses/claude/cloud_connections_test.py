"""Claude's cloud connections: the connector list read with the account's own sign-in, over a scripted
transport in place of Anthropic's endpoint, and the env line that switches one agent's connectors on."""

import json
from pathlib import Path

import httpx
import pytest

from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError


def _sign_in(account_dir: Path, scopes: list[str]) -> None:
    credentials = {"claudeAiOauth": {"accessToken": "token-of-the-account", "scopes": scopes}}
    (account_dir / ".credentials.json").write_text(json.dumps(credentials))


def _agent(tmp_path: Path, host_env: str | None, agent_env: str) -> Path:
    """An agent's state dir whose env file names its host dir, the way mngr writes it."""
    host_dir = tmp_path / "host"
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    if host_env is not None:
        (host_dir / "env").write_text(host_env)
    (agent_state_dir / "env").write_text(f"MNGR_HOST_DIR={host_dir}\n{agent_env}")
    return agent_state_dir


def test_the_list_is_asked_as_the_account_and_names_each_connector_once(tmp_path: Path) -> None:
    _sign_in(tmp_path, ["user:inference", "user:mcp_servers"])
    requests: list[httpx.Request] = []
    pages = {
        None: {"data": [{"display_name": "Slack"}, {"display_name": "Gmail"}], "next_page": "p2"},
        "p2": {"data": [{"display_name": "Slack"}, {"display_name": ""}, {"url": "https://nameless"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("page")])

    names = ClaudeCloudConnections.over_transport(httpx.MockTransport(handler)).list_connected(tmp_path)

    assert names == ("Slack", "Gmail")
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


@pytest.mark.parametrize(
    ("host_env", "agent_env", "expected"),
    [
        ("OTHER=1\n", "ENABLE_CLAUDEAI_MCP_SERVERS=false\n", False),
        ("OTHER=1\n", "ENABLE_CLAUDEAI_MCP_SERVERS=OFF\n", False),
        ("OTHER=1\n", "ENABLE_CLAUDEAI_MCP_SERVERS=true\n", True),
        ("ENABLE_CLAUDEAI_MCP_SERVERS=true\n", "ENABLE_CLAUDEAI_MCP_SERVERS=false\n", True),
        ("OTHER=1\n", "", True),
        (None, "", True),
    ],
)
def test_an_agent_loads_the_connectors_unless_its_start_environment_says_off(
    tmp_path: Path, host_env: str | None, agent_env: str, expected: bool
) -> None:
    """The host's env file wins over the agent's own, and Claude Code loads the connectors when neither speaks."""
    assert ClaudeCloudConnections().is_enabled(_agent(tmp_path, host_env, agent_env)) is expected


@pytest.mark.parametrize("agent_env", ["", "ENABLE_CLAUDEAI_MCP_SERVERS=true\n"])
def test_the_switch_of_an_agent_whose_host_turns_the_connectors_off_cannot_be_read(
    tmp_path: Path, agent_env: str
) -> None:
    """A host that sets the variable off overrides every agent's own line, so no agent there can be switched on."""
    agent_state_dir = _agent(tmp_path, "ENABLE_CLAUDEAI_MCP_SERVERS=false\n", agent_env)
    with pytest.raises(CloudConnectionsError, match="turns the connectors off for all of its agents"):
        ClaudeCloudConnections().is_enabled(agent_state_dir)


def test_enabling_rewrites_the_agents_own_line_and_leaves_the_rest_of_its_env_alone(tmp_path: Path) -> None:
    agent_state_dir = _agent(
        tmp_path, "OTHER=1\n", "ENABLE_CLAUDEAI_MCP_SERVERS=false\nCLAUDE_CONFIG_DIR=/accounts/a\n"
    )
    connections = ClaudeCloudConnections()
    assert not connections.is_enabled(agent_state_dir)

    connections.enable(agent_state_dir)

    assert connections.is_enabled(agent_state_dir)
    lines = (agent_state_dir / "env").read_text().splitlines()
    assert lines == [
        f"MNGR_HOST_DIR={tmp_path / 'host'}",
        "CLAUDE_CONFIG_DIR=/accounts/a",
        "ENABLE_CLAUDEAI_MCP_SERVERS=true",
    ]
    assert (tmp_path / "host" / "env").read_text() == "OTHER=1\n"
