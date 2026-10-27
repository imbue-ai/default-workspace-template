"""The `tk start` / `tk close` commands skills and shared references give an agent, run past
the hook that gates them and checked for ids that cannot reach the call."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_DIR = _REPO_ROOT / ".agents"
_CHECKER = _REPO_ROOT / "system" / "scripts" / "agent_tk_standalone_check.py"
_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_LIFECYCLE = re.compile(r"\btk (start|close)\b")
# An agent fills `<ticket-id>`-style placeholders in before running a block.
_PLACEHOLDER = re.compile(r"<[a-z][a-z0-9-]*>")
# Each tool call gets a fresh shell, so an id held in a variable is empty by the time a later
# call starts or closes the ticket, and `tk` resolves an empty id to whatever single regular
# ticket it finds.
_CAPTURED_CREATE = re.compile(r"\$\(tk create\b")
_VARIABLE_ID = re.compile(r"\btk (start|close) +\"?\$[A-Za-z_{]")


def _skill_docs() -> list[Path]:
    return sorted(
        path for path in _AGENTS_DIR.rglob("*.md") if "changelog" not in path.parts
    )


def _lifecycle_blocks() -> list[tuple[str, str]]:
    return [
        (f"{path.relative_to(_REPO_ROOT)}#{index}", block)
        for path in _skill_docs()
        for index, block in enumerate(_BASH_BLOCK.findall(path.read_text()))
        if _LIFECYCLE.search(block)
    ]


@pytest.mark.parametrize(
    "block", [block for _, block in _lifecycle_blocks()], ids=[name for name, _ in _lifecycle_blocks()]
)
def test_a_documented_tk_start_or_close_block_passes_the_standalone_hook(block: str) -> None:
    """Every bash block holding a `tk start` or `tk close`, with its placeholders filled in,
    is one the hook lets through."""
    command = _PLACEHOLDER.sub("dwt-1234", block).strip()
    result = subprocess.run(
        [sys.executable, str(_CHECKER), command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_no_skill_captures_a_created_ticket_id_in_a_shell_variable() -> None:
    """No skill or reference stores a `tk create` result in a shell variable."""
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}"
        for path in _skill_docs()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _CAPTURED_CREATE.search(line)
    ]
    assert offenders == []


def test_no_skill_starts_or_closes_a_ticket_by_a_shell_variable() -> None:
    """No skill or reference passes `tk start` or `tk close` its id through a shell variable."""
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}"
        for path in _skill_docs()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _VARIABLE_ID.search(line)
    ]
    assert offenders == []
