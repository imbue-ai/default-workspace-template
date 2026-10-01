"""The chat app's client of the shell's layout asks as the chat app: a show names this app's page and carries this
app as the requester, over a loopback stand-in for the shell."""

from workspace_layout.primitives import ClientId
from workspace_layout.testing import fake_desktop
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import ShowOutcome
from workspace_layout.shell_url import LAYOUT_OP_ROUTE
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import desktop_answer

from imbue.chat.shell_client import build_chat_shell_client
from imbue.chat.shell_client import chat_show_request


def test_a_show_is_asked_for_as_the_chat_app_of_one_of_its_pages() -> None:
    shell = LoopbackShell(
        op_answer={**desktop_answer(fake_desktop("home"), "client-1", "win-0123456789abcdef"), "shown": "raised"}
    )
    shell.start()
    try:
        answer = build_chat_shell_client(shell.url).show(
            chat_show_request(
                WindowPath("/?chat=agent-1"),
                showing=(WindowPath("/agent-1"),),
                repoint=(WindowPage("/"),),
                client_id=ClientId("client-1"),
            )
        )
    finally:
        shell.close()

    assert answer.shown is ShowOutcome.RAISED
    assert shell.posted == [
        (
            LAYOUT_OP_ROUTE,
            {
                "op": "show",
                "args": {
                    "app": "chat",
                    "path": "/?chat=agent-1",
                    "showing": ["/agent-1"],
                    "repoint": ["/"],
                    "client": "client-1",
                },
                "requester": {"app": "chat", "marker": ""},
            },
        )
    ]
