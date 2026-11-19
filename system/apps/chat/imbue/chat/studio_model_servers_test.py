"""Tests for asking the Imbue Studio app for the model servers on the user's computer."""

import json
from collections.abc import Callable

import httpx
import pytest
from latchkey_access.gateway import GatewayAccess
from pydantic import SecretStr

from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model import LocalModelServer
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModelChoice
from imbue.chat.studio_model_servers import StudioLink
from imbue.chat.studio_model_servers import find_studio_model_servers
from imbue.chat.studio_model_servers import open_studio_tunnel

_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"
_GATEWAY = GatewayAccess(base_url="http://gateway.test", password=SecretStr("pw"), permissions_override=None)
_SERVERS_PATH = f"/minds-api-proxy/api/v1/agents/{_AGENT_ID}/local-model/servers"
_TUNNEL_PATH = f"/minds-api-proxy/api/v1/agents/{_AGENT_ID}/local-model/tunnel"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_the_servers_on_the_users_computer_are_listed_with_the_port_each_is_reached_by() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"servers": [{"port": 11434, "name": "Ollama", "models": ["qwen3:8b"]}]})

    found = find_studio_model_servers(_client(handler), _GATEWAY, _AGENT_ID, True)

    assert found.link is StudioLink.CONNECTED
    assert found.servers == (
        LocalModelServer(
            base_url="http://127.0.0.1:11434/v1",
            name="Ollama",
            models=("qwen3:8b",),
            choices=(LocalModelChoice(model_id="qwen3:8b"),),
            studio_port=11434,
        ),
    )
    (request,) = seen
    assert (request.method, request.url.path) == ("GET", _SERVERS_PATH)
    assert request.headers["X-Latchkey-Gateway-Password"] == "pw"


def test_what_the_app_says_of_a_servers_models_folds_a_models_sizes_into_it() -> None:
    """An app that describes the models lets the form offer each once, before any tunnel is open."""
    entry = {
        "port": 11434,
        "name": "Ollama",
        "models": ["qwen3:8b", "qwen3-40k:latest", "qwen2.5:7b"],
        "described_models": [
            {"id": "qwen3:8b", "base_id": "qwen3:8b", "max_context_window": 40_960, "parameter_size": "8.2B"},
            {"id": "qwen3-40k:latest", "base_id": "qwen3:8b", "context_window": 40_960, "max_context_window": 40_960},
            {"id": "qwen2.5:7b", "base_id": "qwen2.5:7b", "max_context_window": 32_768, "is_tool_caller": True},
        ],
    }

    found = find_studio_model_servers(
        _client(lambda _request: httpx.Response(200, json={"servers": [entry]})), _GATEWAY, _AGENT_ID, True
    )

    (server,) = found.servers
    assert [(choice.model_id, choice.context_windows) for choice in server.choices] == [
        ("qwen3:8b", (40_960,)),
        ("qwen2.5:7b", ()),
    ]
    assert server.choices[0].unusable_reason is None
    assert "32,768" in (server.choices[1].unusable_reason or "")


def test_a_description_this_workspace_cannot_read_leaves_the_models_as_listed() -> None:
    entry = {"port": 11434, "name": "Ollama", "models": ["qwen3:8b"], "described_models": [{"base_id": "qwen3:8b"}]}

    found = find_studio_model_servers(
        _client(lambda _request: httpx.Response(200, json={"servers": [entry]})), _GATEWAY, _AGENT_ID, True
    )

    assert found.servers[0].choices == (LocalModelChoice(model_id="qwen3:8b"),)


def test_a_computer_running_no_model_server_is_connected_with_nothing_to_offer() -> None:
    found = find_studio_model_servers(
        _client(lambda _request: httpx.Response(200, json={"servers": []})), _GATEWAY, _AGENT_ID, True
    )

    assert (found.link, found.servers) == (StudioLink.CONNECTED, ())


def test_a_workspace_without_the_permission_asks_for_it() -> None:
    """The request names no id of its own: a workspace's gateway assigns one, and refuses a body that carries one."""
    filed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/permission-requests":
            filed.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(403, json={"error": "not permitted"})

    found = find_studio_model_servers(_client(handler), _GATEWAY, _AGENT_ID, True)

    assert (found.link, found.servers) == (StudioLink.AWAITING_APPROVAL, ())
    (request,) = filed
    assert set(request) == {"agent_id", "type", "payload", "rationale"}
    assert request["agent_id"] == _AGENT_ID
    assert request["type"] == "local-model"
    assert request["payload"] == {}
    assert request["rationale"]


def test_a_look_that_only_checks_for_the_permission_does_not_ask_the_user_again() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(403, json={})

    found = find_studio_model_servers(_client(handler), _GATEWAY, _AGENT_ID, False)

    assert found.link is StudioLink.AWAITING_APPROVAL
    assert paths == [_SERVERS_PATH]


@pytest.mark.parametrize("status", [400, 404, 501])
def test_an_app_from_before_these_routes_cannot_be_asked(status: int) -> None:
    found = find_studio_model_servers(
        _client(lambda _request: httpx.Response(status, json={})), _GATEWAY, _AGENT_ID, True
    )

    assert found.link is StudioLink.UNAVAILABLE


def test_a_gateway_that_does_not_know_the_request_type_cannot_be_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400 if request.url.path == "/permission-requests" else 403, json={})

    assert find_studio_model_servers(_client(handler), _GATEWAY, _AGENT_ID, True).link is StudioLink.UNAVAILABLE


def test_a_workspace_with_no_gateway_cannot_ask() -> None:
    assert find_studio_model_servers(_client(_unreachable), None, _AGENT_ID, True).link is StudioLink.UNAVAILABLE
    assert find_studio_model_servers(_client(_unreachable), _GATEWAY, "", True).link is StudioLink.UNAVAILABLE


@pytest.mark.parametrize("status", [502, 503, 504])
def test_an_app_that_does_not_answer_is_unreachable(status: int) -> None:
    by_status = find_studio_model_servers(
        _client(lambda _request: httpx.Response(status, text="bad gateway")), _GATEWAY, _AGENT_ID, True
    )
    by_refusal = find_studio_model_servers(_client(_unreachable), _GATEWAY, _AGENT_ID, True)

    assert by_status.link is StudioLink.UNREACHABLE
    assert by_refusal.link is StudioLink.UNREACHABLE


def test_the_permission_request_failing_to_reach_the_app_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/permission-requests":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(403, json={})

    assert find_studio_model_servers(_client(handler), _GATEWAY, _AGENT_ID, True).link is StudioLink.UNREACHABLE


def test_opening_the_tunnel_answers_the_address_the_server_has_inside_the_workspace() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"workspace_port": 11435})

    base_url = open_studio_tunnel(_client(handler), _GATEWAY, _AGENT_ID, 11434)

    assert base_url == "http://127.0.0.1:11435/v1"
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", _TUNNEL_PATH)
    assert json.loads(request.content) == {"port": 11434}


def test_a_tunnel_the_user_no_longer_allows_says_to_ask_again() -> None:
    with pytest.raises(LocalModelError, match="no longer allowed"):
        open_studio_tunnel(_client(lambda _request: httpx.Response(403, json={})), _GATEWAY, _AGENT_ID, 11434)


def test_a_tunnel_the_app_cannot_open_says_what_to_check() -> None:
    with pytest.raises(LocalModelError, match="Studio is open"):
        open_studio_tunnel(_client(_unreachable), _GATEWAY, _AGENT_ID, 11434)
    with pytest.raises(LocalModelError, match="Studio is open"):
        open_studio_tunnel(_client(lambda _request: httpx.Response(502, text="oops")), _GATEWAY, _AGENT_ID, 11434)
    with pytest.raises(LocalModelError, match="no connection to Imbue Studio"):
        open_studio_tunnel(_client(_unreachable), None, _AGENT_ID, 11434)
