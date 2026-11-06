from pathlib import Path

import pytest
from workspace_bare_scripts import message_chat

from routines.mock_http_server_test import FakeHttpServer
from routines_app.errors import FixChatError
from routines_app.fix_chat import ChatAppFixChatStarter


def _starter(tmp_path: Path, http_server: FakeHttpServer) -> ChatAppFixChatStarter:
    registry = tmp_path / "apps.toml"
    registry.write_text(f'[[apps]]\nname = "chat"\nurl = "{http_server.url}"\n')
    return ChatAppFixChatStarter(repo_root=tmp_path, environ={message_chat.ENV_APPS_FILE: str(registry)})


def test_the_fix_chat_is_created_by_the_chat_app_on_the_brief_and_opens_itself(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    http_server.answer(
        "POST",
        message_chat.CREATE_CHAT_PATH,
        (200, {"chat_id": "agent-fix", "name": "fix-news-sync", "display_name": "Fix News sync"}),
    )

    chat_id = _starter(tmp_path, http_server).start("The News sync fails.", "Fix News sync")

    assert chat_id == "agent-fix"
    [request] = http_server.requests
    assert request.body == {
        "name": "Fix News sync",
        "message": "The News sync fails.",
        "labels": {"auto_open": "true"},
        "should_wait": True,
    }


def test_a_create_the_chat_app_refused_is_a_fix_chat_error_with_its_reason(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    http_server.answer("POST", message_chat.CREATE_CHAT_PATH, (500, {"detail": "sign in to a provider first"}))

    with pytest.raises(FixChatError, match="sign in to a provider first"):
        _starter(tmp_path, http_server).start("The News sync fails.", "Fix News sync")
