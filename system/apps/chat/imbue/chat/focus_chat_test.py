"""``POST /api/focus-chat``: the chat app turns the Imbue Studio app's ask to show a chat into one ``show`` of its injected
shell, and answers with what the shell did."""

from typing import Any

import pytest
from workspace_layout.errors import ShellAnswerMalformedError
from workspace_layout.errors import ShellOpError
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.testing import FakeShell

from imbue.chat.agent_manager import AgentManager
from imbue.chat.primitives import ChatId
from imbue.chat.server import create_application
from imbue.chat.shell_client import chat_show_args
from imbue.chat.testing import build_test_state
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import seed_failed_chat
from imbue.chat.ws_broadcaster import WebSocketBroadcaster

_CHAT_ID = "agent-5f0c2e"


def _focus_chat(
    shell: FakeShell, body: dict[str, Any], is_secondary: bool = False, agent_manager: AgentManager | None = None
) -> Any:
    state = build_test_state(is_secondary=is_secondary, shell=shell, agent_manager=agent_manager)
    return create_application(state).test_client().post("/api/focus-chat", json=body)


def _forwarded(chat_id: str = _CHAT_ID) -> dict[str, str]:
    """The body the shell posts for ``minds:focus-chat``: the message's own fields and the client."""
    return {"type": "minds:focus-chat", "client_id": "client-1", "chatId": chat_id}


def test_a_focus_chat_shows_the_chat_root_on_the_chat_counting_its_own_page_and_moving_a_chat_root_window() -> None:
    shell = FakeShell(shown=ShowOutcome.NAVIGATED)

    answered = _focus_chat(shell, _forwarded())

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": "navigated", "window_id": "win-0123456789abcdef"}
    assert shell.shows == [
        chat_show_args(
            WindowPath(f"/?chat={_CHAT_ID}"),
            showing=(WindowPath(f"/{_CHAT_ID}"),),
            repoint=(WindowPage("/"),),
            client_id=ClientId("client-1"),
        )
    ]


@pytest.mark.parametrize("body_chat_id", [{}, {"chatId": None}], ids=["absent", "null"])
def test_a_focus_chat_with_no_chat_shows_the_chat_root_as_it_stands_moving_no_window(
    body_chat_id: dict[str, None],
) -> None:
    """With no chat named, the chat root comes up: a chat root window on any chat the app lists or holds provisionally
    already shows it (the welcome chat may have been put there first), and no window is pointed elsewhere."""
    shell = FakeShell(shown=ShowOutcome.PINNED)
    manager = AgentManager.build(WebSocketBroadcaster())
    seed_agent_state(manager, "agent-listed-7a1", name="Chat-1")
    seed_failed_chat(manager, ChatId("agent-welcome-7a2"), "Welcome")

    answered = _focus_chat(
        shell, {"type": "minds:focus-chat", "client_id": "client-1", **body_chat_id}, agent_manager=manager
    )

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": "pinned", "window_id": "win-0123456789abcdef"}
    assert shell.shows == [
        chat_show_args(
            WindowPath("/"),
            showing=(WindowPath("/?chat=agent-listed-7a1"), WindowPath("/?chat=agent-welcome-7a2")),
            repoint=(),
            client_id=ClientId("client-1"),
        )
    ]


@pytest.mark.parametrize("chat_id", ["", "not-a-chat", "agent-1/../x", "agent-1?x=1", "agent-1.agent-2.sess-3"])
def test_a_focus_chat_for_something_that_is_not_a_chat_id_is_a_400_and_asks_the_shell_nothing(chat_id: str) -> None:
    shell = FakeShell()

    answered = _focus_chat(shell, _forwarded(chat_id))

    assert answered.status_code == 400
    assert shell.shows == []


def test_a_secondary_chat_refuses_to_open_a_window() -> None:
    shell = FakeShell()

    answered = _focus_chat(shell, _forwarded(), is_secondary=True)

    assert answered.status_code == 403
    assert shell.shows == []


@pytest.mark.parametrize(
    "error",
    [
        ShellUnreachableError("Could not reach the shell at http://127.0.0.1:1/api/layout/broadcast"),
        ShellRefusedOpError(
            "The shell refused the show (404): No client 'client-1'", status_code=404, detail="No client 'client-1'"
        ),
        ShellAnswerMalformedError("The shell answered the show with something else: []"),
    ],
    ids=["unreachable", "refused", "malformed"],
)
def test_a_show_the_shell_did_not_carry_out_is_a_502_saying_why(error: ShellOpError) -> None:
    shell = FakeShell(error=error)

    answered = _focus_chat(shell, _forwarded())

    assert answered.status_code == 502
    assert answered.get_json() == {"detail": str(error)}
    assert len(shell.shows) == 1


def test_a_focus_chat_for_a_client_id_the_shell_would_refuse_is_a_400_and_asks_the_shell_nothing() -> None:
    shell = FakeShell()

    answered = _focus_chat(shell, {**_forwarded(), "client_id": "../escape"})

    assert answered.status_code == 400
    assert shell.shows == []
