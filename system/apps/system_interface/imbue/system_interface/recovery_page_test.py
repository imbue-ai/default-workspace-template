import html
import re
from pathlib import Path

import pytest

from imbue.system_interface.recovery_page import ASSIST_COMMAND
from imbue.system_interface.recovery_page import DESKTOPS_RECOVERY_PATH
from imbue.system_interface.recovery_page import DesktopsLoadFailure
from imbue.system_interface.recovery_page import FREE_SPACE_INSTRUCTION
from imbue.system_interface.recovery_page import desktops_not_loaded_page
from imbue.system_interface.recovery_page import desktops_repair_message
from imbue.system_interface.recovery_page import render_recovery_page
from imbue.system_interface.recovery_page import repair_mngr_command
from imbue.system_interface.testing import words_a_shell_splits

_WORKSPACE_UI_SOURCE = Path(__file__).parents[4] / "libs" / "workspace_ui" / "src"
_SHELL_FRONTEND_SOURCE = Path(__file__).parents[2] / "frontend" / "src"


@pytest.mark.frontend
def test_the_recovery_pages_asks_start_with_the_assist_command_as_workspace_ui_writes_it() -> None:
    """The page's asks reach a chat with no shell to write ``assistRequest``'s lead, and the page cannot import it (it is
    served exactly when the bundle may be missing), so it puts the command first itself; this pins the command and
    its place to that source. A full disk's instruction is pinned to the desktop's the same way."""
    source = (_WORKSPACE_UI_SOURCE / "app_contract.ts").read_text()
    assist = re.search(r'export const ASSIST_COMMAND = "([^"]+)";', source)
    assert assist is not None and assist.group(1) == ASSIST_COMMAND
    assert "if (origin === null) return `${ASSIST_COMMAND} ${request}`;" in source
    failures = (_SHELL_FRONTEND_SOURCE / "model" / "failures.ts").read_text()
    free_space = re.search(r'export const FREE_SPACE_INSTRUCTION =\s*"([^"]+)";', failures)
    assert free_space is not None and free_space.group(1) == FREE_SPACE_INSTRUCTION


@pytest.mark.frontend
def test_the_frontend_sends_its_page_to_the_route_the_shell_serves_the_desktops_recovery_page_at() -> None:
    source = (_SHELL_FRONTEND_SOURCE / "reload.ts").read_text()
    declared = re.search(r'export const DESKTOPS_RECOVERY_PATH = "([^"]+)";', source)
    assert declared is not None and declared.group(1) == DESKTOPS_RECOVERY_PATH


def test_the_desktops_repair_line_hands_a_shell_its_message_whole_whatever_the_details_hold(tmp_path: Path) -> None:
    """The details are the shell's own text (a path, an exception, a traceback), so they can hold anything a shell
    would expand: the line quotes the message so a real ``sh`` builds the very argv the button's message is."""
    failure = DesktopsLoadFailure(
        details='it\'s `$(echo injected)` and "quoted" $HOME !\n```\nfenced\n```\nback\\slash',
        is_out_of_space=False,
    )
    message = desktops_repair_message(failure)
    line = repair_mngr_command(message)

    words = words_a_shell_splits(line, tmp_path)

    assert words[words.index("--message") + 1] == message
    assert message.startswith(
        "/assist Imbue Studio showed the user this failure: This workspace's desktops couldn't be loaded.\n\n"
    )


def test_a_full_disk_is_named_on_the_desktops_page_and_its_agent_is_asked_to_free_up_space(tmp_path: Path) -> None:
    full = desktops_not_loaded_page(
        DesktopsLoadFailure(details="[Errno 28] No space left on device", is_out_of_space=True)
    )
    other = desktops_not_loaded_page(DesktopsLoadFailure(details="[Errno 21] Is a directory", is_out_of_space=False))

    full_page = render_recovery_page(full, None, True, "")
    other_page = render_recovery_page(other, None, True, "")

    assert "<h1>This workspace is out of space, so its desktops couldn't be loaded.</h1>" in full_page
    assert ">Ask an agent to free up space</button>" in full_page
    assert "<h1>This workspace's desktops couldn't be loaded.</h1>" in other_page
    assert ">Ask an agent to fix this</button>" in other_page
    # The agent is told what the page says and what its button asks for: the full disk, and to free up space.
    shown_line = re.search(r'<pre id="repair-command">(.*?)</pre>', full_page, re.DOTALL)
    assert shown_line is not None
    assert words_a_shell_splits(html.unescape(shown_line.group(1)), tmp_path)[-1] == (
        "/assist Imbue Studio showed the user this failure: This workspace is out of space, so its desktops couldn't "
        "be loaded.\n\nDetails:\n```\n[Errno 28] No space left on device\n```\n\n"
        "Free up disk space in this workspace, then tell the user what was taking up the space and what you removed."
    )
    assert desktops_repair_message(DesktopsLoadFailure(details="x", is_out_of_space=False)).endswith(
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
    )
