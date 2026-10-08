"""How the chat app reaches the shell's layout: the shared ``workspace_layout`` client, asking as this app.

Every op carries this app as its requester with no marker (the app itself asks, not one of its chats' agents), and a
``show`` of one of this app's paths names this app as the one whose page to show.
"""

from collections.abc import Sequence
from typing import Final

from workspace_layout.client import ShellLayoutClient
from workspace_layout.ops import OpRequester
from workspace_layout.ops import ShowArgs
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import WindowPage
from workspace_layout.primitives import WindowPath
from workspace_layout.shell_url import shell_base_url

from imbue.chat.primitives import CHAT_APP_NAME

# One loopback request the shell answers without work; past this it is not answering.
SHELL_TIMEOUT_SECONDS: Final[float] = 2.0

CHAT_APP_REQUESTER: Final[OpRequester] = OpRequester(app=CHAT_APP_NAME, marker="")


def build_chat_shell_client(shell_url: str) -> ShellLayoutClient:
    return ShellLayoutClient(shell_url=shell_url, requester=CHAT_APP_REQUESTER, timeout_seconds=SHELL_TIMEOUT_SECONDS)


def build_live_chat_shell_client() -> ShellLayoutClient:
    """The client of the shell the workspace runs (``MINDS_WORKSPACE_SERVER_URL``, else its loopback port)."""
    return build_chat_shell_client(shell_base_url())


def chat_show_args(
    path: WindowPath, showing: Sequence[WindowPath], repoint: Sequence[WindowPage], client_id: ClientId
) -> ShowArgs:
    """A ``show`` of one of this app's paths on one client's screen."""
    return ShowArgs(app=CHAT_APP_NAME, path=path, showing=tuple(showing), repoint=tuple(repoint), client=client_id)
