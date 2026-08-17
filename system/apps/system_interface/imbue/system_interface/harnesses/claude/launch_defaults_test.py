"""Unit tests for the workspace fast-mode launch default."""

from pathlib import Path

from imbue.system_interface.harnesses.claude.launch_defaults import get_workspace_fast_mode_decision_path
from imbue.system_interface.harnesses.claude.launch_defaults import read_workspace_fast_mode_decision
from imbue.system_interface.harnesses.claude.launch_defaults import write_workspace_fast_mode_decision


def test_absent_decision_reads_as_undecided(tmp_path: Path) -> None:
    assert read_workspace_fast_mode_decision(get_workspace_fast_mode_decision_path(tmp_path)) is None


def test_written_decision_round_trips(tmp_path: Path) -> None:
    path = get_workspace_fast_mode_decision_path(tmp_path)
    write_workspace_fast_mode_decision(path, False)
    assert read_workspace_fast_mode_decision(path) is False
    write_workspace_fast_mode_decision(path, True)
    assert read_workspace_fast_mode_decision(path) is True


def test_corrupt_decision_reads_as_undecided(tmp_path: Path) -> None:
    path = get_workspace_fast_mode_decision_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_workspace_fast_mode_decision(path) is None
