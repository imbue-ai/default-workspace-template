import re
from pathlib import Path

import pytest

from imbue.system_interface.shell.agent_asks import AGENT_FIX_INSTRUCTION
from imbue.system_interface.shell.agent_asks import AGENT_FIX_TASK_DETAILS_LABEL
from imbue.system_interface.shell.agent_asks import AGENT_FIX_TASK_LEAD
from imbue.system_interface.shell.agent_asks import agent_fix_task

_WORKSPACE_UI_SOURCE = Path(__file__).parents[5] / "libs" / "workspace_ui" / "src"


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


def test_the_agents_task_ends_with_the_instruction_an_ask_names_in_place_of_finding_a_fault() -> None:
    assert agent_fix_task("This window couldn't be closed.", "  ") == (
        "Imbue Studio showed the user this failure: This window couldn't be closed.\n\n"
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
    )
    assert agent_fix_task("Terminal isn't running.", None, "Start it again, then tell the user what was wrong.") == (
        "Imbue Studio showed the user this failure: Terminal isn't running.\n\n"
        "Start it again, then tell the user what was wrong."
    )


@pytest.mark.frontend
def test_the_agents_task_is_the_one_workspace_ui_words() -> None:
    """The server's own pages cannot import ``agentFixTask``, so they word the same text in Python; this pins the words
    and the shape to that source, where a rewording would otherwise leave these pages' agent told something no other
    ask says."""
    source = (_WORKSPACE_UI_SOURCE / "app_contract.ts").read_text()
    function = re.search(r"export function agentFixTask\(.*?\n}\n", source, re.DOTALL)
    assert function is not None, "agentFixTask is no longer declared in app_contract.ts"
    body = function.group(0)
    assert f"`{AGENT_FIX_TASK_LEAD}${{" in body
    assert f"`{AGENT_FIX_TASK_DETAILS_LABEL}\\n${{fence}}\\n${{details}}\\n${{fence}}`" in body
    assert "parts.push(instruction ?? AGENT_FIX_INSTRUCTION);" in body
    assert 'return parts.join("\\n\\n");' in body
    assert "Math.max(3, longestRun + 1)" in source
    instruction = re.search(r'export const AGENT_FIX_INSTRUCTION =\s*"([^"]+)";', source)
    assert instruction is not None and instruction.group(1) == AGENT_FIX_INSTRUCTION
