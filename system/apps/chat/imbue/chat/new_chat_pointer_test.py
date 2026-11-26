"""``POST /api/show-new-chat-pointer``: the chat app turns another page's ask for the New chat pointer into one
``show`` of its injected shell, and answers with what the shell did."""

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

from imbue.chat.server import create_application
from imbue.chat.shell_client import chat_show_args
from imbue.chat.testing import build_test_state


def _show_new_chat_pointer(shell: FakeShell, body: dict[str, Any], is_secondary: bool = False) -> Any:
    state = build_test_state(is_secondary=is_secondary, shell=shell)
    return create_application(state).test_client().post("/api/show-new-chat-pointer", json=body)


def _forwarded() -> dict[str, str]:
    """The body the shell posts for ``onboarding:show-new-chat-pointer``: the message's type and the client."""
    return {"type": "onboarding:show-new-chat-pointer", "client_id": "client-1"}


@pytest.mark.parametrize("shown", [ShowOutcome.NAVIGATED, ShowOutcome.PINNED], ids=["on-screen", "pinned"])
def test_the_ask_points_a_chat_root_window_at_the_path_that_asks_the_root_for_its_pointer(
    shown: ShowOutcome,
) -> None:
    shell = FakeShell(shown=shown)

    answered = _show_new_chat_pointer(shell, _forwarded())

    assert answered.status_code == 200
    assert answered.get_json() == {"shown": shown.value, "window_id": "win-0123456789abcdef"}
    # No page counts as already showing it, so the root is told every time; a chat root window on screen is the one
    # pointed at the path, whatever chat it shows.
    assert shell.shows == [
        chat_show_args(
            WindowPath("/?open=new-chat-pointer"),
            showing=(),
            repoint=(WindowPage("/"),),
            client_id=ClientId("client-1"),
        )
    ]


def test_a_field_a_newer_sender_adds_is_ignored() -> None:
    shell = FakeShell(shown=ShowOutcome.NAVIGATED)

    answered = _show_new_chat_pointer(shell, {**_forwarded(), "from": "checklist"})

    assert answered.status_code == 200
    assert len(shell.shows) == 1


def test_a_secondary_chat_refuses_to_open_a_window() -> None:
    shell = FakeShell()

    answered = _show_new_chat_pointer(shell, _forwarded(), is_secondary=True)

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

    answered = _show_new_chat_pointer(shell, _forwarded())

    assert answered.status_code == 502
    assert answered.get_json() == {"detail": str(error)}
    assert len(shell.shows) == 1


def test_an_ask_for_a_client_id_the_shell_would_refuse_is_a_400_and_asks_the_shell_nothing() -> None:
    shell = FakeShell()

    answered = _show_new_chat_pointer(shell, {**_forwarded(), "client_id": "../escape"})

    assert answered.status_code == 400
    assert shell.shows == []


def test_an_ask_naming_no_client_is_refused_and_asks_the_shell_nothing() -> None:
    shell = FakeShell()

    answered = _show_new_chat_pointer(shell, {"type": "onboarding:show-new-chat-pointer"})

    assert answered.status_code == 400
    assert shell.shows == []
