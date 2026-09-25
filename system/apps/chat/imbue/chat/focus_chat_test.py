"""``POST /api/focus-chat``: the chat app turns the Mind app's ask to show a chat into one ``show`` of its injected
shell, and answers with what the shell did."""

from typing import Any

import pytest

from imbue.chat.server import create_application
from imbue.chat.shell_client import ShellAnswerMalformedError
from imbue.chat.shell_client import ShellOpError
from imbue.chat.shell_client import ShellRefusedOpError
from imbue.chat.shell_client import ShellUnreachableError
from imbue.chat.shell_client import ShowAnswer
from imbue.chat.shell_client import ShowRequest
from imbue.chat.testing import RecordingShell
from imbue.chat.testing import build_test_state

_CHAT_ID = "agent-5f0c2e"


def _focus_chat(shell: RecordingShell, body: dict[str, Any], is_secondary: bool = False) -> Any:
    client = create_application(build_test_state(is_secondary=is_secondary, shell=shell)).test_client()
    return client.post("/api/focus-chat", json=body)


def _forwarded(chat_id: str = _CHAT_ID) -> dict[str, str]:
    """The body the shell posts for ``minds:focus-chat``: the message's own fields and the client."""
    return {"type": "minds:focus-chat", "client_id": "client-1", "chatId": chat_id}


def test_a_focus_chat_shows_the_chat_root_on_the_chat_counting_its_own_page_and_moving_a_chat_root_window() -> None:
    shell = RecordingShell(answer=ShowAnswer(shown="navigated", window_id="win-0123456789abcdef"))

    answered = _focus_chat(shell, _forwarded())

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": "navigated", "window_id": "win-0123456789abcdef"}
    assert shell.shows == [
        ShowRequest(path=f"/?chat={_CHAT_ID}", showing=(f"/{_CHAT_ID}",), repoint=("/",), client_id="client-1")
    ]


@pytest.mark.parametrize("chat_id", ["", "not-a-chat", "agent-1/../x", "agent-1?x=1", "agent-1.agent-2.sess-3"])
def test_a_focus_chat_for_something_that_is_not_a_chat_id_is_a_400_and_asks_the_shell_nothing(chat_id: str) -> None:
    shell = RecordingShell()

    answered = _focus_chat(shell, _forwarded(chat_id))

    assert answered.status_code == 400
    assert shell.shows == []


def test_a_secondary_chat_refuses_to_open_a_window() -> None:
    shell = RecordingShell()

    answered = _focus_chat(shell, _forwarded(), is_secondary=True)

    assert answered.status_code == 403
    assert shell.shows == []


@pytest.mark.parametrize(
    "error",
    [
        ShellUnreachableError("Could not reach the shell at http://127.0.0.1:1/api/layout/broadcast"),
        ShellRefusedOpError("The shell refused the show (404): No client 'client-1'"),
        ShellAnswerMalformedError("The shell answered the show with something else: []"),
    ],
    ids=["unreachable", "refused", "malformed"],
)
def test_a_show_the_shell_did_not_carry_out_is_a_502_saying_why(error: ShellOpError) -> None:
    shell = RecordingShell(error=error)

    answered = _focus_chat(shell, _forwarded())

    assert answered.status_code == 502
    assert answered.get_json() == {"detail": str(error)}
    assert len(shell.shows) == 1
