"""The Mind app's ask to show a chat, through the real shell.

The shell and this app are served together (``running_workspace``); the test posts ``minds:focus-chat`` to this
app's handler as the shell's relay does, for a client the shell knows, and reads the shell's own desktop and
placements afterwards, so the seam between this app's ``show`` and the shell's is crossed for real.
"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import RunningWorkspace
from imbue.chat.testing import running_workspace
from imbue.system_interface.testing import find_free_port

_HOME_DESKTOP_ID = "home"
_CLIENT_ID = "client-4f1d"
_OTHER_CHAT_ID = "agent-7c3e9b"


def _focus_chat(workspace: RunningWorkspace, chat_id: str) -> httpx.Response:
    """What the shell's relay posts this app for ``minds:focus-chat``: the message and the client it reached."""
    return httpx.post(
        f"{workspace.chat_url}/api/focus-chat",
        json={"type": "minds:focus-chat", "client_id": _CLIENT_ID, "chatId": chat_id},
        timeout=10.0,
    )


def _chat_windows(workspace: RunningWorkspace) -> list[dict[str, Any]]:
    desktops = httpx.get(f"{workspace.shell_url}/api/desktops", timeout=5.0).json()["desktops"]
    (home,) = [desktop for desktop in desktops if desktop["id"] == _HOME_DESKTOP_ID]
    return [window for window in home["windows"] if window["app"] == "chat"]


def _placements(workspace: RunningWorkspace) -> list[dict[str, Any]]:
    answer = httpx.get(f"{workspace.shell_url}/api/placements/{_HOME_DESKTOP_ID}?client={_CLIENT_ID}", timeout=5.0)
    return answer.json()["placements"]


@pytest.mark.timeout(60, func_only=False)
def test_a_focus_chat_opens_the_chat_root_on_the_chat_and_then_moves_that_window_to_another_chat(
    tmp_path: Path,
) -> None:
    with running_workspace(tmp_path, find_free_port(), find_free_port()) as workspace:
        broadcaster = workspace.shell_state.shell.broadcaster
        client_queue = broadcaster.register()
        broadcaster.set_client_info(client_queue, _CLIENT_ID, _HOME_DESKTOP_ID)
        try:
            # Nothing of the chat app is on screen, and this workspace pins no chat window: a chat root opens.
            first = _focus_chat(workspace, FIXTURE_AGENT_ID)
            assert first.status_code == 200, first.text
            assert first.json()["shown"] == "opened"
            (window,) = _chat_windows(workspace)
            assert (window["id"], window["path"]) == (first.json()["window_id"], f"/?chat={FIXTURE_AGENT_ID}")
            on_top = _placements(workspace)[-1]
            assert (on_top["window_id"], on_top["is_minimized"]) == (window["id"], False)

            # That chat root is on screen on another chat now, so it is the window moved, and no other opens.
            second = _focus_chat(workspace, _OTHER_CHAT_ID)
            assert second.status_code == 200, second.text
            assert (second.json()["shown"], second.json()["window_id"]) == ("navigated", window["id"])
            assert [(moved["id"], moved["path"]) for moved in _chat_windows(workspace)] == [
                (window["id"], f"/?chat={_OTHER_CHAT_ID}")
            ]
            assert _placements(workspace)[-1]["window_id"] == window["id"]
        finally:
            broadcaster.unregister(client_queue)
