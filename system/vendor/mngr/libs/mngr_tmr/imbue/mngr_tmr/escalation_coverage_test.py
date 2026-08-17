"""Unit tests for the escalation-grouping coverage check."""

import json
from pathlib import Path

import pytest

from imbue.mngr_tmr.conftest import write_mapper_outcome
from imbue.mngr_tmr.escalation_coverage import find_ungrouped_ids
from imbue.mngr_tmr.escalation_coverage import main


def _write_mapper_outcome(inputs_dir: Path, agent_name: str, escalation_count: int) -> None:
    """Write a mapper outcome carrying ``escalation_count`` escalations."""
    write_mapper_outcome(
        inputs_dir,
        agent_name,
        {
            "changes": {},
            "tests_passing_before": True,
            "tests_passing_after": True,
            "escalations": [
                {"kind": "HARNESS_DEFECT", "description_markdown": f"Problem {i}."} for i in range(escalation_count)
            ],
        },
    )


def _write_reducer_outcome(path: Path, member_id_groups: list[list[str]]) -> None:
    path.write_text(
        json.dumps(
            {
                "escalations": [
                    {
                        "kind": "HARNESS_DEFECT",
                        "description_markdown": "A group.",
                        "member_ids": member_ids,
                    }
                    for member_ids in member_id_groups
                ]
            }
        )
    )


def test_no_ungrouped_ids_when_every_escalation_is_claimed(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 2)
    _write_mapper_outcome(inputs, "agent-b", 1)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [["agent-a#0", "agent-b#0"], ["agent-a#1"]])
    assert find_ungrouped_ids(inputs, outcome) == []


def test_ungrouped_ids_are_reported(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 3)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [["agent-a#1"]])
    assert find_ungrouped_ids(inputs, outcome) == ["agent-a#0", "agent-a#2"]


def test_a_group_with_no_members_claims_nothing(tmp_path: Path) -> None:
    """The integrator's own findings are legitimate, but they cover no mapper report."""
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 1)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [[]])
    assert find_ungrouped_ids(inputs, outcome) == ["agent-a#0"]


def test_a_missing_reducer_outcome_means_nothing_is_grouped(tmp_path: Path) -> None:
    """A reducer that died before writing leaves every escalation ungrouped, which is the truth."""
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 2)
    assert find_ungrouped_ids(inputs, tmp_path / "nonexistent.json") == ["agent-a#0", "agent-a#1"]


def test_a_run_with_no_escalations_is_covered(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 0)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [])
    assert find_ungrouped_ids(inputs, outcome) == []


def test_main_exits_zero_and_says_so_when_covered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 1)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [["agent-a#0"]])
    assert main(["escalation_coverage", str(inputs), "--reducer-outcome", str(outcome)]) == 0
    assert "are covered by an escalation group" in capsys.readouterr().out


def test_main_exits_non_zero_and_lists_the_gaps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The reducer reads these ids back to fix its own grouping, so they must be printed."""
    inputs = tmp_path / "inputs"
    _write_mapper_outcome(inputs, "agent-a", 2)
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [["agent-a#0"]])
    assert main(["escalation_coverage", str(inputs), "--reducer-outcome", str(outcome)]) == 1
    output = capsys.readouterr().out
    assert "1 mapper escalation(s) belong to no group" in output
    assert "agent-a#1" in output


def test_main_refuses_a_missing_inputs_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A mistyped path must not read zero outcomes and report a confident pass."""
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [])
    assert main(["escalation_coverage", str(tmp_path / "nope"), "--reducer-outcome", str(outcome)]) == 2
    assert "No such inputs directory" in capsys.readouterr().out


def test_main_refuses_an_inputs_directory_holding_no_outcomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    outcome = tmp_path / "integrator_outcome.json"
    _write_reducer_outcome(outcome, [])
    assert main(["escalation_coverage", str(inputs), "--reducer-outcome", str(outcome)]) == 2
    assert "No mapper outcomes found" in capsys.readouterr().out
