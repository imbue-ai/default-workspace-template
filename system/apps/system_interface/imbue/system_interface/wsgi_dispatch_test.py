from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

import pytest

from imbue.system_interface.wsgi_dispatch import PathDispatchingFlask
from imbue.system_interface.wsgi_dispatch import is_chat_path

_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "path",
    [
        f"/{_AGENT_ID}",
        f"/{_AGENT_ID}.7f3a2c1e-session",
        "/agent-test-123",
        "/_instances",
        f"/_instances/{_AGENT_ID}/rename",
        "/api/agents/create-chat",
        f"/api/agents/{_AGENT_ID}/events",
        f"/api/agents/{_AGENT_ID}/events/evt-1/detail",
        f"/api/agents/{_AGENT_ID}/stream",
        f"/api/agents/{_AGENT_ID}/message",
        f"/api/agents/{_AGENT_ID}/presence",
        f"/api/agents/{_AGENT_ID}/subagents/abc/events",
        f"/api/agents/{_AGENT_ID}/screen",
        "/api/harnesses",
        "/api/uploads",
        "/api/uploads/2026/photo.png",
        "/api/claude-auth/status",
        "/api/accounts",
        "/api/accounts/flow/abc",
        "/api/lanes",
        "/api/latchkey/scopes/slack-api",
        f"/api/proto-agents/{_AGENT_ID}/logs",
    ],
)
def test_the_chat_document_serves_its_own_paths(path: str) -> None:
    assert is_chat_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/favicon.ico",
        "/assets/index-abc123.js",
        "/_static/app_contract.js",
        "/api/health",
        "/api/agents",
        f"/api/agents/{_AGENT_ID}/destroy",
        f"/api/agents/{_AGENT_ID}/start",
        f"/api/agents/{_AGENT_ID}/stop",
        "/api/ws",
        "/api/projects",
        "/api/terminals",
        "/api/browsers",
        "/api/apps/files/stop",
        "/api/layout/broadcast",
        "/home/user/workspace/data/notes.md",
        "/agentic-browser",
        "/agent-test-123/extra",
        "/_instances_extra",
        "/api/uploads_extra",
    ],
)
def test_the_shell_serves_everything_else(path: str) -> None:
    assert not is_chat_path(path)


def _chat_app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"chat"]


def _shell_index() -> str:
    return "shell"


def _shell_catch_all(path: str) -> str:
    return "shell"


def _shell_app() -> PathDispatchingFlask:
    application = PathDispatchingFlask(__name__, static_folder=None)
    application.add_url_rule("/", view_func=_shell_index, methods=["GET"])
    application.add_url_rule("/<path:path>", view_func=_shell_catch_all, methods=["GET"])
    return application


def test_the_shell_app_hands_each_request_to_one_document() -> None:
    application = _shell_app()
    application.chat_application = _chat_app
    client = application.test_client()
    assert client.get(f"/{_AGENT_ID}").data == b"chat"
    assert client.get("/_instances").data == b"chat"
    assert client.get("/").data == b"shell"
    assert client.get(f"/api/agents/{_AGENT_ID}/destroy").data == b"shell"


def test_every_path_is_the_shells_until_a_chat_app_is_installed() -> None:
    client = _shell_app().test_client()
    assert client.get(f"/{_AGENT_ID}").data == b"shell"
