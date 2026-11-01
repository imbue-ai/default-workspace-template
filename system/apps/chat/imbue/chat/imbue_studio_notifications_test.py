"""The read call and the mention notification to the Imbue Studio app: their shape and headers, and that every
failure is dropped quietly."""

from collections.abc import Generator
from contextlib import contextmanager

from pydantic import SecretStr

from imbue.chat.imbue_studio_notifications import ImbueStudioNotificationsClient
from imbue.chat.latchkey_gateway import GatewayAccess
from imbue.chat.primitives import ChatId
from imbue.chat.testing import RecordingGateway
from imbue.chat.testing import serve_app
from imbue.system_interface.testing import find_free_port

_FUTURE_TIMEOUT_SECONDS = 15.0


def _client(base_url: str, override: str | None, agent_by_chat: dict[str, str]) -> ImbueStudioNotificationsClient:
    return ImbueStudioNotificationsClient(
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
) -> Generator[tuple[RecordingGateway, ImbueStudioNotificationsClient], None, None]:
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


def test_notify_mention_posts_the_title_the_message_and_a_focus_destination_as_the_chats_own_agent() -> None:
    with _served_client(200, {"agent-lounge": "agent-lounge"}) as (gateway, client):
        client.notify_mention(ChatId("agent-lounge"), "Bob mentioned you in Lounge", "@Josh look").result(
            timeout=_FUTURE_TIMEOUT_SECONDS
        )

    (received,) = gateway.received
    assert received.path == "/minds-api-proxy/api/v1/agents/agent-lounge/notifications"
    assert received.body == {
        "title": "Bob mentioned you in Lounge",
        "message": "@Josh look",
        "destination": {"type": "minds:focus-chat", "chatId": "agent-lounge"},
    }
    assert received.headers["X-Latchkey-Gateway-Password"] == "gateway-password-7731"


def test_no_mention_is_posted_for_a_chat_with_no_agent() -> None:
    with _served_client(200, {}) as (gateway, client):
        assert (
            client.notify_mention(ChatId("agent-gone"), "title", "message").result(timeout=_FUTURE_TIMEOUT_SECONDS)
            is None
        )
    assert gateway.received == []


def test_nothing_is_posted_for_a_chat_with_no_agent_or_without_a_gateway() -> None:
    without_gateway = ImbueStudioNotificationsClient(gateway=None, resolve_agent_id=lambda _chat_id: "agent-chat1")
    try:
        with _served_client(200, {}) as (gateway, client):
            client.mark_chat_read(ChatId("agent-provisional")).result(timeout=_FUTURE_TIMEOUT_SECONDS)
            without_gateway.mark_chat_read(ChatId("agent-chat1")).result(timeout=_FUTURE_TIMEOUT_SECONDS)
    finally:
        without_gateway.shutdown()
    assert gateway.received == []
