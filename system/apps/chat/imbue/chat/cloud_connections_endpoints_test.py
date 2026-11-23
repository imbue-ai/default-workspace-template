"""Tests for the cloud-connection routes: the card's offer, and taking it -- the agent's switch, the withdrawn
permission request, the restart, and the notice -- over a recording bridge in place of the router's and a
scripted gateway."""

import os
from pathlib import Path

import httpx
import pytest
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import register_api_schema_route
from flask import Flask
from flask.testing import FlaskClient
from workspace_layout.identity import IDENTITY_HEADER

from imbue.chat import accounts
from imbue.chat import cloud_connections_endpoints
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.cloud_connections import CONNECTED_FILENAME
from imbue.chat.cloud_connections import ConnectedConnectors
from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnector
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.message_display import classify_user_message
from imbue.chat.mock_cloud_connections_bridge_test import RecordingCloudConnectionsBridge
from imbue.chat.state import attach_state
from imbue.chat.testing import build_test_state

_CHAT = "agent-00000000000000000000000000000001"
_VISITOR = '{"owner": false, "user_id": "u-2", "email": "visitor@example.com"}'
_GMAIL_CATALOG = [{"scope": "google-gmail-api", "display_name": "Gmail", "permissions": []}]


def _gateway(requests: list[httpx.Request], delete_status: int = 200) -> httpx.Client:
    """The gateway's catalog for Gmail and its permission-request delete, recording every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(delete_status, json={})
        if request.url.path == "/permissions/available/google-gmail":
            return httpx.Response(200, json=_GMAIL_CATALOG)
        return httpx.Response(404, json={"detail": "unknown service"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _claude_agent(tmp_path: Path, connected: tuple[str | CloudConnector, ...]) -> AgentInfo:
    """A claude agent whose host turns the connectors off, on an account with ``connected`` recorded."""
    account_id, account_path = accounts.mint_account_dir()
    accounts.commit_account(account_id, "anthropic", "Anthropic")
    connectors = tuple(
        connector if isinstance(connector, CloudConnector) else CloudConnector(name=connector)
        for connector in connected
    )
    (account_path / CONNECTED_FILENAME).write_text(ConnectedConnectors(connectors=connectors).model_dump_json())
    host_dir = tmp_path / "host"
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    (host_dir / "env").write_text("ENABLE_CLAUDEAI_MCP_SERVERS=false\n")
    (agent_state_dir / "env").write_text(f"MNGR_HOST_DIR={host_dir}\n")
    return AgentInfo(
        id="agent-1",
        name="chat-1",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=account_path,
        labels={"account": account_id},
        harness=HarnessType.CLAUDE,
    )


def _switch(agent_info: AgentInfo) -> str:
    """The workspace's switch, as the host's env file holds it."""
    return (agent_info.agent_state_dir.parent.parent / "env").read_text()


def _start(agent_info: AgentInfo) -> None:
    """What the agent's harness leaves behind each time its process starts."""
    (agent_info.agent_state_dir / ClaudeCloudConnections.process_started_marker_filename).touch()


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge: RecordingCloudConnectionsBridge,
    gateway_requests: list[httpx.Request],
    is_secondary: bool = False,
    delete_status: int = 200,
) -> FlaskClient:
    # The grants documents are read relative to the workspace root the app runs from.
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.invalid")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "secret")
    application = Flask(__name__)
    state = build_test_state(latchkey_http_client=_gateway(gateway_requests, delete_status), is_secondary=is_secondary)
    state.cloud_connections_bridge = bridge
    attach_state(application, state)
    register_api_schema_route(
        application,
        DEFAULT_API_SCHEMA_PATH,
        title="Chat",
        description="The cloud-connection routes alone",
        route_table=cloud_connections_endpoints.ROUTE_TABLE,
    )
    cloud_connections_endpoints.register_routes(application)
    return application.test_client()


def _share_workspace(tmp_path: Path) -> None:
    secrets = tmp_path / "data" / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "share_grants.toml").write_text('[workspace]\nusers = ["user-erin-0001"]\n')


def _offer(client: FlaskClient, scope: str = "google-gmail-api", headers: dict[str, str] | None = None) -> dict:
    response = client.get(f"/api/chats/{_CHAT}/cloud-connections?scope={scope}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_the_offer_is_made_for_a_scope_whose_service_the_account_has_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = _claude_agent(tmp_path, ("Gmail", "Slack"))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info})
    client = _client(tmp_path, monkeypatch, bridge, [])

    assert _offer(client) == {"is_offered": True, "provider_name": "Anthropic", "connector_name": "Gmail"}
    assert _offer(client, scope="slack-api") == {"is_offered": False, "provider_name": None, "connector_name": None}


def test_the_offer_is_made_for_a_custom_service_by_its_domain_or_its_mcp_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom service request names no scope: the card asks by the domain, and by the endpoint of an MCP server."""
    connected = ("ClickUp", CloudConnector(name="Widgets", url="https://mcp.acme-cdn.net/mcp"))
    agent_info = _claude_agent(tmp_path, connected)
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info})
    gateway_requests: list[httpx.Request] = []
    client = _client(tmp_path, monkeypatch, bridge, gateway_requests)
    path = f"/api/chats/{_CHAT}/cloud-connections"

    by_domain = client.get(path, query_string={"domain": "api.clickup.com"}).get_json()
    by_endpoint = client.get(
        path, query_string={"domain": "acme.com", "mcp_endpoint": "https://mcp.acme-cdn.net/mcp"}
    ).get_json()
    unknown = client.get(path, query_string={"domain": "api.example.com"}).get_json()

    assert by_domain == {"is_offered": True, "provider_name": "Anthropic", "connector_name": "ClickUp"}
    assert by_endpoint == {"is_offered": True, "provider_name": "Anthropic", "connector_name": "Widgets"}
    assert unknown == {"is_offered": False, "provider_name": None, "connector_name": None}
    # A domain is matched as it stands: the gateway's catalog is asked about scopes alone.
    assert gateway_requests == []


def test_no_offer_to_a_visitor_in_a_shared_workspace_on_a_preview_or_without_a_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info})
    client = _client(tmp_path, monkeypatch, bridge, [])
    assert _offer(client, headers={IDENTITY_HEADER: _VISITOR})["is_offered"] is False
    assert _offer(client, scope="")["is_offered"] is False
    assert client.get("/api/chats/agent-unknown/cloud-connections?scope=google-gmail-api").status_code == 404

    _share_workspace(tmp_path)
    assert _offer(client)["is_offered"] is False

    preview = _client(tmp_path / "preview", monkeypatch, bridge, [], is_secondary=True)
    assert _offer(preview)["is_offered"] is False


def test_taking_the_offer_switches_the_workspace_withdraws_the_request_restarts_and_tells_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info}, queued_block="and one more thing")
    gateway_requests: list[httpx.Request] = []
    client = _client(tmp_path, monkeypatch, bridge, gateway_requests)

    response = client.post(f"/api/chats/{_CHAT}/cloud-connections/enable", json={"request_id": "req-7"})

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"is_request_withdrawn": True, "is_notice_delivered": True}
    assert _switch(agent_info) == "ENABLE_CLAUDEAI_MCP_SERVERS=true\n"
    (withdrawal,) = gateway_requests
    assert (withdrawal.method, withdrawal.url.path) == ("DELETE", "/permission-requests/req-7")
    assert withdrawal.headers["x-latchkey-device"] == "*"
    assert withdrawal.headers["x-latchkey-gateway-password"] == "secret"
    assert bridge.restarted == [_CHAT]
    (notice, queued) = bridge.sent
    assert queued == (_CHAT, "and one more thing")
    assert "enable the cloud MCP connections of their Anthropic account" in notice[1]
    # The page draws no row for the notice, and flips the card of the request it names.
    display = classify_user_message(notice[1])
    assert display is not None
    assert (display.display.value, display.resolution, display.request_id) == (
        "permission_resolution",
        "cloud_connections",
        "req-7",
    )
    # Once the agent has started with them there is nothing left to offer, or to take a second time.
    _start(agent_info)
    assert _offer(client)["is_offered"] is False
    again = client.post(f"/api/chats/{_CHAT}/cloud-connections/enable", json={"request_id": "req-7"})
    assert again.status_code == 409


def test_the_switch_stays_set_when_the_request_cannot_be_withdrawn_or_the_notice_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info}, is_send_failing=True)
    client = _client(tmp_path, monkeypatch, bridge, [], delete_status=404)

    response = client.post(f"/api/chats/{_CHAT}/cloud-connections/enable", json={"request_id": "req-7"})

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"is_request_withdrawn": False, "is_notice_delivered": False}
    assert bridge.restarted == [_CHAT]
    assert _switch(agent_info) == "ENABLE_CLAUDEAI_MCP_SERVERS=true\n"


def test_a_restart_that_fails_is_a_500_with_the_switch_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info}, is_restart_failing=True)
    client = _client(tmp_path, monkeypatch, bridge, [])

    response = client.post(f"/api/chats/{_CHAT}/cloud-connections/enable", json={"request_id": "req-7"})

    assert response.status_code == 500
    assert "would not start" in response.get_json()["detail"]
    assert bridge.sent == []
    assert _switch(agent_info) == "ENABLE_CLAUDEAI_MCP_SERVERS=true\n"


def test_taking_the_offer_is_refused_where_it_is_never_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A visitor, a shared workspace, a preview, a chat that is switching, and a harness with no cloud connections:
    each is refused before anything is written, withdrawn, or restarted."""
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    pi_agent = AgentInfo(
        id="agent-2",
        name="chat-2",
        state="RUNNING",
        agent_state_dir=tmp_path / "pi",
        claude_config_dir=tmp_path / "config",
        labels=agent_info.labels,
        harness=HarnessType.PI_CODING,
    )
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info, "agent-pi": pi_agent})
    gateway_requests: list[httpx.Request] = []
    client = _client(tmp_path, monkeypatch, bridge, gateway_requests)
    path = f"/api/chats/{_CHAT}/cloud-connections/enable"
    body = {"request_id": "req-7"}

    assert client.post(path, json=body, headers={IDENTITY_HEADER: _VISITOR}).status_code == 403
    assert client.post("/api/chats/agent-unknown/cloud-connections/enable", json=body).status_code == 404
    assert client.post("/api/chats/agent-pi/cloud-connections/enable", json=body).status_code == 400
    assert client.post(path, json={}).status_code == 400
    assert client.post(path, json={"request_id": "../approve/req-7"}).status_code == 400
    bridge.is_restart_refused = True
    assert client.post(path, json=body).status_code == 409
    bridge.is_restart_refused = False
    preview = _client(tmp_path / "preview", monkeypatch, bridge, gateway_requests, is_secondary=True)
    assert preview.post(path, json=body).status_code == 409
    _share_workspace(tmp_path / "preview")
    assert client.post(path, json=body).status_code == 403

    assert _switch(agent_info) == "ENABLE_CLAUDEAI_MCP_SERVERS=false\n"
    assert gateway_requests == []
    assert bridge.restarted == []
    assert bridge.sent == []


def test_a_chat_that_started_before_the_switch_was_turned_on_is_still_offered_it_and_only_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another chat turned the workspace's switch on while this one was running: its agent has none of the
    connections yet, so its card still offers them, and taking the offer restarts it without rewriting the switch."""
    agent_info = _claude_agent(tmp_path, ("Gmail",))
    _start(agent_info)
    host_env = agent_info.agent_state_dir.parent.parent / "env"
    host_env.write_text("ENABLE_CLAUDEAI_MCP_SERVERS=true\n")
    switched_at = (agent_info.agent_state_dir / ClaudeCloudConnections.process_started_marker_filename).stat().st_mtime + 60
    os.utime(host_env, (switched_at, switched_at))
    bridge = RecordingCloudConnectionsBridge(agents_by_chat_id={_CHAT: agent_info})
    client = _client(tmp_path, monkeypatch, bridge, [])
    assert _offer(client)["is_offered"] is True

    response = client.post(f"/api/chats/{_CHAT}/cloud-connections/enable", json={"request_id": "req-7"})

    assert response.status_code == 200, response.get_json()
    assert bridge.restarted == [_CHAT]
    assert host_env.stat().st_mtime == switched_at
