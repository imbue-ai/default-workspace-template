"""Which of the workspace tree's memory tools ``update_self.py memory`` runs, and how.

The staged skill runs in a workspace as old as the oldest release the app updates from. The tool
first shipped in ``minds-v0.8.2`` as ``memory_candidates.py``, which prints its listing when run
with no arguments. Each tool here is a fake script at the real path, so the tests exercise the
real shell-out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from update_self_skill import update_memory, update_self

_MUST_NOT_RUN = "import sys\nsys.exit('this tool must not run')\n"


def _write_tool(repo_root: Path, tool: Path, body: str) -> None:
    script = repo_root / tool
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body)


def _reporting_tool(exit_code: int) -> str:
    """A tool that prints how it was run and exits ``exit_code``."""
    return (
        "import os, sys\n"
        "print('ran with', sys.argv[1:], 'in', os.getcwd())\n"
        f"sys.exit({exit_code})\n"
    )


@pytest.mark.parametrize("exit_code", [0, 3])
def test_runs_the_current_tool_alone_and_passes_on_what_it_prints_and_its_exit_code(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], exit_code: int
) -> None:
    repo_root = tmp_path.resolve()
    _write_tool(
        repo_root, update_memory.WORKSPACE_MEMORY_REL, _reporting_tool(exit_code)
    )
    _write_tool(repo_root, update_memory.MEMORY_CANDIDATES_REL, _MUST_NOT_RUN)

    assert update_self.main(["memory", "--repo-root", str(repo_root)]) == exit_code

    assert capfd.readouterr().out == (
        f"ran with ['list', '--stoppable'] in {repo_root}\n"
    )


def test_an_earlier_workspace_gets_its_own_tools_listing_then_how_to_read_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path.resolve()
    _write_tool(repo_root, update_memory.MEMORY_CANDIDATES_REL, _reporting_tool(0))

    assert update_self.main(["memory", "--repo-root", str(repo_root)]) == 0

    listing, _, note = capfd.readouterr().out.partition("\n")
    assert listing == f"ran with [] in {repo_root}"
    assert note.strip()


def test_a_workspace_with_no_memory_tool_is_told_so_without_a_failure(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert update_self.main(["memory", "--repo-root", str(tmp_path)]) == 0

    assert capfd.readouterr().out.strip()
