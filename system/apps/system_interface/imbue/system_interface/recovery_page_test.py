import html
import re
from pathlib import Path

import pytest

from imbue.system_interface.recovery_page import AGENT_FIX_TASK_CLOSING
from imbue.system_interface.recovery_page import AGENT_FIX_TASK_DETAILS_LABEL
from imbue.system_interface.recovery_page import AGENT_FIX_TASK_LEAD
from imbue.system_interface.recovery_page import ASSIST_COMMAND
from imbue.system_interface.recovery_page import DESKTOPS_RECOVERY_PATH
from imbue.system_interface.recovery_page import DesktopsLoadFailure
from imbue.system_interface.recovery_page import agent_fix_task
from imbue.system_interface.recovery_page import desktops_not_loaded_page
from imbue.system_interface.recovery_page import desktops_repair_message
from imbue.system_interface.recovery_page import render_recovery_page
from imbue.system_interface.recovery_page import repair_mngr_command
from imbue.system_interface.testing import words_a_shell_splits

_WORKSPACE_UI_SOURCE = Path(__file__).parents[4] / "libs" / "workspace_ui" / "src"
_SHELL_FRONTEND_SOURCE = Path(__file__).parents[2] / "frontend" / "src"


def test_the_agents_task_fences_details_where_no_backtick_run_inside_them_can_close_the_fence() -> None:
    details = "HTTP 502\ndocs answered the launch with 500: boom\n  at render (``` in a template)"

    text = agent_fix_task("Docs couldn't open that", details)

    assert text.startswith("Imbue Studio showed the user this failure: Docs couldn't open that.\n\n")
    fenced = re.search(r"^(`{4,})\n([\s\S]*?)\n\1$", text, re.MULTILINE)
    assert fenced is not None and fenced.group(2) == details
    assert text.endswith(
        "\n\nFind out why it failed and fix it, then tell the user what was wrong and what you changed."
    )
    assert not re.search(r"\b(me|my|I)\b", text)


def test_the_agents_task_for_a_failure_without_details_is_its_sentence_and_the_task_alone() -> None:
    text = agent_fix_task("This window couldn't be closed.", "  ")

    assert text == (
        "Imbue Studio showed the user this failure: This window couldn't be closed.\n\n"
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
    )


@pytest.mark.frontend
def test_the_recovery_pages_agent_task_is_the_one_workspace_ui_words() -> None:
    """The page cannot import ``agentFixTask`` and ``assistRequest``, which it is served exactly when the bundle may be
    missing to run, so it words the same text in Python; this pins the words and the shape to that source, where a
    rewording would otherwise leave this page's agent told something no other ask says."""
    source = (_WORKSPACE_UI_SOURCE / "app_contract.ts").read_text()
    function = re.search(r"export function agentFixTask\(.*?\n}\n", source, re.DOTALL)
    assert function is not None, "agentFixTask is no longer declared in app_contract.ts"
    body = function.group(0)
    assert f"`{AGENT_FIX_TASK_LEAD}${{" in body
    assert f"`{AGENT_FIX_TASK_DETAILS_LABEL}\\n${{fence}}\\n${{details}}\\n${{fence}}`" in body
    assert f'parts.push("{AGENT_FIX_TASK_CLOSING}");' in body
    assert 'return parts.join("\\n\\n");' in body
    assert "Math.max(3, longestRun + 1)" in source
    assist = re.search(r'export const ASSIST_COMMAND = "([^"]+)";', source)
    assert assist is not None and assist.group(1) == ASSIST_COMMAND
    assert "return `${ASSIST_COMMAND} ${request}`;" in source


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
    # The agent is told what the page says, so the full disk's ask names the full disk.
    shown_line = re.search(r'<pre id="repair-command">(.*?)</pre>', full_page, re.DOTALL)
    assert shown_line is not None
    assert words_a_shell_splits(html.unescape(shown_line.group(1)), tmp_path)[-1].startswith(
        "/assist Imbue Studio showed the user this failure: This workspace is out of space, so its desktops couldn't "
        "be loaded.\n\nDetails:\n```\n[Errno 28] No space left on device\n```"
    )
