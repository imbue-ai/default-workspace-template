from routines.mock_http_server_test import FakeHttpServer
from routines.notifications import GatewayNotifier
from routines.primitives import RoutineId

DIGEST = RoutineId("digest")


def _environ(http_server: FakeHttpServer) -> dict[str, str]:
    return {
        "LATCHKEY_GATEWAY": http_server.url + "/",
        "LATCHKEY_GATEWAY_PASSWORD": "secret",
        "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE": "jwt",
        "MNGR_AGENT_ID": "agent-services",
    }


def test_a_notification_is_posted_as_the_services_agent_with_its_title_and_the_routines_page(
    http_server: FakeHttpServer,
) -> None:
    path = "/minds-api-proxy/api/v1/agents/agent-services/notifications"
    http_server.answer("POST", path, (200, {}))

    is_taken = GatewayNotifier(environ=_environ(http_server)).notify(
        DIGEST, "Morning digest", "Morning digest needs you."
    )

    assert is_taken
    request = http_server.requests[0]
    assert request.path == path
    assert request.headers["x-latchkey-gateway-password"] == "secret"
    assert request.headers["x-latchkey-gateway-permissions-override"] == "jwt"
    assert request.body == {
        "title": "Morning digest",
        "message": "Morning digest needs you.",
        "destination": {"type": "minds:show-routine", "routine_id": "digest"},
    }


def test_a_refusal_and_an_unreachable_app_are_not_taken(http_server: FakeHttpServer) -> None:
    http_server.answer("POST", "/minds-api-proxy/api/v1/agents/agent-services/notifications", (403, {}))
    assert not GatewayNotifier(environ=_environ(http_server)).notify(DIGEST, "Digest", "x")
    unreachable = {**_environ(http_server), "LATCHKEY_GATEWAY": "http://127.0.0.1:9"}
    assert not GatewayNotifier(environ=unreachable).notify(DIGEST, "Digest", "x")


def test_without_the_gateway_environment_nothing_is_sent(http_server: FakeHttpServer) -> None:
    assert not GatewayNotifier(environ={}).notify(DIGEST, "Digest", "x")
    assert http_server.requests == []
