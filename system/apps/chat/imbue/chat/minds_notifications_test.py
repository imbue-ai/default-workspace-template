"""The read call to the Imbue Studio app: its shape and headers, and that every failure is dropped quietly."""

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import SecretStr

from imbue.chat.latchkey_gateway import GatewayAccess
from imbue.chat.minds_notifications import MindsNotificationsClient
from imbue.chat.primitives import ChatId
from imbue.chat.testing import RecordingGateway
from imbue.chat.testing import serve_app
from imbue.system_interface.testing import find_free_port

_FUTURE_TIMEOUT_SECONDS = 15.0


def _client(base_url: str, override: str | None, agent_by_chat: dict[str, str]) -> MindsNotificationsClient:
    return MindsNotificationsClient(
        gateway=GatewayAccess(
            base_url=base_url,
            password=SecretStr("gateway-password-7731"),
            permissions_override=SecretStr(override) if override is not None else None,
        ),
        resolve_agent_id=lambda chat_id: agent_by_chat.get(str(chat_id)),
    )


@contextmanager
def _served_client(
    status: int, agent_by_chat: dict[str, str], override: str | None = None, base_url_suffix: str = ""
) -> Iterator[tuple[RecordingGateway, MindsNotificationsClient]]:
    """A client pointed at a served ``RecordingGateway`` answering ``status``; shut down on exit."""
    gateway = RecordingGateway(status=status)
    with serve_app(gateway.application) as served:
        client = _client(served.http_url + base_url_suffix, override, agent_by_chat)
        try:
            yield gateway, client
        finally:
            client.shutdown()


def test_mark_chat_read_posts_an_empty_body_as_the_chats_current_agent_with_the_gateway_headers() -> None:
    with _served_client(
        200, {"agent-chat1": "agent-successor2"}, override="override-jwt-4410", base_url_suffix="/"
    ) as (gateway, client):
        client.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS)

    (received,) = gateway.received
    assert received.path == "/minds-api-proxy/api/v1/agents/agent-successor2/notifications/read"
    assert received.body == {}
    assert received.headers["X-Latchkey-Gateway-Password"] == "gateway-password-7731"
    assert received.headers["X-Latchkey-Gateway-Permissions-Override"] == "override-jwt-4410"


def test_mark_chat_read_sends_no_override_header_to_a_vps_gateway() -> None:
    with _served_client(200, {"agent-chat1": "agent-chat1"}) as (gateway, client):
        client.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS)

    (received,) = gateway.received
    assert "X-Latchkey-Gateway-Permissions-Override" not in received.headers


def test_an_app_without_the_read_route_is_ignored() -> None:
    with _served_client(404, {"agent-chat1": "agent-chat1"}) as (gateway, client):
        assert client.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS) is None
    assert len(gateway.received) == 1


def test_an_unreachable_gateway_is_ignored() -> None:
    client = _client(f"http://127.0.0.1:{find_free_port()}", None, {"agent-chat1": "agent-chat1"})
    try:
        assert client.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS) is None
    finally:
        client.shutdown()


def test_nothing_is_posted_for_a_chat_with_no_agent_or_without_a_gateway() -> None:
    without_gateway = MindsNotificationsClient(gateway=None, resolve_agent_id=lambda _chat_id: "agent-chat1")
    try:
        with _served_client(200, {}) as (gateway, client):
            client.mark_chat_read(ChatId("agent-provisional")).result(timeout=_FUTURE_TIMEOUT_SECONDS)
            without_gateway.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS)
    finally:
        without_gateway.shutdown()
    assert gateway.received == []
