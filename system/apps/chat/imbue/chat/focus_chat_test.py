"""``POST /api/focus-chat``: the chat app turns the Mind app's ask to show a chat into one ``show`` op on the shell, over
a real loopback server standing in for the shell."""

import json
from typing import Any

import pytest

from imbue.chat.server import create_application
from imbue.chat.testing import RecordingLayoutOpShell
from imbue.chat.testing import build_test_state
from imbue.chat.testing import serve_app
from imbue.system_interface.testing import find_free_port

_CHAT_ID = "agent-5f0c2e"
_SHOWN_ANSWER = {"ok": True, "shown": "navigated", "window_id": "win-0123456789abcdef", "desktop_id": "home"}


def _focus_chat(body: dict[str, Any], is_secondary: bool = False) -> Any:
    client = create_application(build_test_state(is_secondary=is_secondary)).test_client()
    return client.post("/api/focus-chat", json=body)


def _forwarded(chat_id: str = _CHAT_ID) -> dict[str, str]:
    """The body the shell posts for ``minds:focus-chat``: the message's own fields and the client."""
    return {"type": "minds:focus-chat", "client_id": "client-1", "chatId": chat_id}


def test_a_focus_chat_asks_the_shell_to_show_the_chat_root_on_the_chat_counting_the_chats_own_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = RecordingLayoutOpShell(200, json.dumps(_SHOWN_ANSWER))
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded())

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": "navigated", "window_id": "win-0123456789abcdef"}
    assert shell.received == [
        {
            "op": "show",
            "args": {
                "app": "chat",
                "path": f"/?chat={_CHAT_ID}",
                "showing": [f"/{_CHAT_ID}"],
                "client": "client-1",
            },
            "requester": {"app": "chat", "marker": ""},
        }
    ]


@pytest.mark.parametrize("chat_id", ["", "not-a-chat", "agent-1/../x", "agent-1?x=1", "agent-1.agent-2.sess-3"])
def test_a_focus_chat_for_something_that_is_not_a_chat_id_is_a_400_and_asks_the_shell_nothing(
    monkeypatch: pytest.MonkeyPatch, chat_id: str
) -> None:
    shell = RecordingLayoutOpShell(200, json.dumps(_SHOWN_ANSWER))
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded(chat_id))

    assert answered.status_code == 400
    assert shell.received == []


def test_a_shell_that_refuses_the_show_is_a_502_quoting_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = RecordingLayoutOpShell(404, json.dumps({"detail": "No client 'client-1'"}))
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded())

    assert answered.status_code == 502
    assert "404" in answered.get_json()["detail"] and "No client" in answered.get_json()["detail"]
    assert len(shell.received) == 1


def test_a_shell_that_cannot_be_reached_is_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", f"http://127.0.0.1:{find_free_port()}")

    answered = _focus_chat(_forwarded())

    assert answered.status_code == 502
    assert "Could not reach the shell" in answered.get_json()["detail"]


def test_a_secondary_chat_refuses_to_open_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = RecordingLayoutOpShell(200, json.dumps(_SHOWN_ANSWER))
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded(), is_secondary=True)

    assert answered.status_code == 403
    assert shell.received == []


def test_a_shown_the_chat_app_does_not_know_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = RecordingLayoutOpShell(200, json.dumps({**_SHOWN_ANSWER, "shown": "tiled"}))
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded())

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": "tiled", "window_id": "win-0123456789abcdef"}


@pytest.mark.parametrize("body", ["not json", "[]"])
def test_a_2xx_from_the_shell_that_is_not_an_object_is_a_502(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    shell = RecordingLayoutOpShell(200, body)
    with serve_app(shell.application) as served:
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", served.http_url)
        answered = _focus_chat(_forwarded())

    assert answered.status_code == 502
    assert "not an object" in answered.get_json()["detail"]
