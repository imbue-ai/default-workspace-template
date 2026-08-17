"""Unit tests for the TMR pull-request summary builder."""

import json
from pathlib import Path

import pytest

from imbue.mngr_tmr.pr_summary import build_escalations_section
from imbue.mngr_tmr.pr_summary import build_pr_summary
from imbue.mngr_tmr.pr_summary import build_pr_title
from imbue.mngr_tmr.pr_summary import build_status_breakdown
from imbue.mngr_tmr.pr_summary import collect_results
from imbue.mngr_tmr.pr_summary import main
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import EscalationKind
from imbue.mngr_tmr.report import IntegratorEscalation


def write_outcome(inputs_dir: Path, agent_name: str, payload: dict[str, object]) -> None:
    """Write a mapper outcome where the summary builder expects to find it."""
    target = inputs_dir / agent_name / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def _fix_outcome(kind: str = "FIX_TEST", **extra: object) -> dict[str, object]:
    return {
        "changes": {kind: {"status": "SUCCEEDED", "summary_markdown": "fixed"}},
        "errored": False,
        "tests_passing_before": False,
        "tests_passing_after": True,
        "summary_markdown": "ok",
        **extra,
    }


def _escalation(
    description: str = "Something needs attention.\n\nDetail.",
    kind: EscalationKind = EscalationKind.SUITE_DUPLICATION,
    member_ids: tuple[str, ...] = (),
    resolved_in_commit_hash: str | None = None,
) -> IntegratorEscalation:
    return IntegratorEscalation(
        kind=kind,
        description_markdown=description,
        member_ids=member_ids,
        resolved_in_commit_hash=resolved_in_commit_hash,
    )


def test_collect_results_skips_agents_without_an_outcome(tmp_path: Path) -> None:
    write_outcome(tmp_path, "has-outcome", _fix_outcome())
    (tmp_path / "no-outcome").mkdir()
    results = collect_results(tmp_path)
    assert [str(r.agent_name) for r in results] == ["has-outcome"]


def test_collect_results_on_missing_directory(tmp_path: Path) -> None:
    assert collect_results(tmp_path / "nope") == []


def test_status_breakdown_counts_each_section(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    write_outcome(tmp_path, "b", _fix_outcome(kind="FIX_IMPL"))
    write_outcome(
        tmp_path,
        "c",
        {
            "changes": {"FIX_TEST": {"status": "FAILED", "summary_markdown": "no"}},
            "tests_passing_before": False,
            "tests_passing_after": False,
        },
    )
    breakdown = build_status_breakdown(collect_results(tmp_path))
    assert "### Mapper outcomes (3 total)" in breakdown
    assert "| Implementation fixes | 1 |" in breakdown
    assert "| Test and doc fixes | 1 |" in breakdown
    assert "| Fix failed | 1 |" in breakdown


def test_status_breakdown_omits_empty_sections(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert "Fix failed" not in build_status_breakdown(collect_results(tmp_path))


def test_escalations_section_reports_none() -> None:
    assert build_escalations_section(()) == "### Escalations\n\nNone reported."


def test_unresolved_escalations_carry_their_whole_description() -> None:
    """A reviewer has to act on these, so the body must not truncate them."""
    section = build_escalations_section(
        (_escalation(description="Coverage gate fails single-test runs.\n\nThe repo-wide gate applies."),)
    )
    assert "### Unresolved escalations (1)" in section
    assert "Coverage gate fails single-test runs." in section
    assert "The repo-wide gate applies." in section


def test_resolved_escalations_are_one_line_each() -> None:
    """They are already fixed; the commit carries the detail."""
    section = build_escalations_section(
        (
            _escalation(
                description="ttyd install short-circuits on root hosts.\n\nLong detail nobody needs here.",
                member_ids=("a#0", "b#0"),
                resolved_in_commit_hash="abc1234def567",
            ),
        )
    )
    assert "### Resolved escalations (1)" in section
    assert "ttyd install short-circuits on root hosts." in section
    assert "Long detail nobody needs here." not in section
    assert "`abc1234def`" in section


def test_unresolved_escalations_come_before_resolved_ones() -> None:
    section = build_escalations_section(
        (
            _escalation(description="Already fixed.", resolved_in_commit_hash="abc1234"),
            _escalation(description="Still open."),
        )
    )
    assert section.index("Unresolved escalations") < section.index("Resolved escalations")


def test_integrator_originated_escalations_lead_their_section() -> None:
    """A plain member-count sort would bury the findings no mapper could produce."""
    section = build_escalations_section(
        (
            _escalation(description="Reported by many.", member_ids=("a#0", "b#0", "c#0")),
            _escalation(description="Found while reading the diff."),
            _escalation(description="Reported by one.", member_ids=("d#0",)),
        )
    )
    assert section.index("Found while reading the diff.") < section.index("Reported by many.")
    assert section.index("Reported by many.") < section.index("Reported by one.")


def test_escalation_scale_is_stated() -> None:
    section = build_escalations_section(
        (
            _escalation(description="Widely reported.", member_ids=("a#0", "b#0")),
            _escalation(description="Mine alone."),
        )
    )
    assert "2 mapper report(s)" in section
    assert "found by the integrator" in section


def test_resolved_escalation_cells_are_escaped() -> None:
    """A pipe in a summary would split the markdown table cell."""
    section = build_escalations_section((_escalation(description="a | b", resolved_in_commit_hash="abc1234"),))
    assert r"a \| b" in section


def test_both_halves_report_scale_the_same_way() -> None:
    """The Reports column must not mean a phrase in one half and a bare number in the other."""
    section = build_escalations_section(
        (
            _escalation(description="Still open.", member_ids=("a#0", "b#0")),
            _escalation(description="Already fixed.", member_ids=("c#0",), resolved_in_commit_hash="abc1234"),
        )
    )
    assert "2 mapper report(s)" in section
    assert "1 mapper report(s)" in section


def test_pr_title_counts_only_unresolved_escalations(tmp_path: Path) -> None:
    """Counting raw mapper escalations put "429 escalated" on a run of 21 problems."""
    write_outcome(tmp_path, "a", _fix_outcome())
    write_outcome(tmp_path, "b", _fix_outcome())
    title = build_pr_title(
        "tmr-mngr/20260721085455/reducer",
        collect_results(tmp_path),
        (
            _escalation(description="Still open.", member_ids=("a#0", "b#0")),
            _escalation(description="Already fixed.", resolved_in_commit_hash="abc1234"),
        ),
    )
    assert title == "TMR tmr-mngr 2026-07-21: 2 fixed, 1 escalations unresolved"


def test_pr_title_for_an_all_clean_run(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", {"changes": {}, "tests_passing_before": True, "tests_passing_after": True})
    title = build_pr_title("tmr-mngr/20260721085455/reducer", collect_results(tmp_path))
    assert title == "TMR tmr-mngr 2026-07-21: 1 tests clean"


def test_pr_title_tolerates_an_unexpected_branch_shape() -> None:
    """A non-timestamp run name is passed through rather than mangled."""
    assert build_pr_title("weird-branch", []) == "TMR weird-branch: 0 tests clean"


def test_reducer_escalations_are_the_summary(tmp_path: Path) -> None:
    """The integrator's groups are what the PR reports; the raw list lives in the report."""
    inputs = tmp_path / "inputs"
    write_outcome(inputs, "a", _fix_outcome())
    outcome_path = tmp_path / "integrator_outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "escalations": [
                    {
                        "kind": "SUITE_DUPLICATION",
                        "description_markdown": "39 tests added a timeout marker.",
                        "member_ids": ["a#0"],
                    }
                ]
            }
        )
    )
    summary = build_pr_summary(inputs, outcome_path)
    assert "39 tests added a timeout marker." in summary
    assert "1 mapper report(s)" in summary


def test_pr_summary_without_a_reducer_outcome(tmp_path: Path) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert "None reported." in build_pr_summary(tmp_path)


def test_pr_summary_tolerates_an_unreadable_reducer_outcome(tmp_path: Path) -> None:
    """A reducer that died before writing its outcome must not break the PR body."""
    write_outcome(tmp_path, "a", _fix_outcome())
    assert "Mapper outcomes" in build_pr_summary(tmp_path, tmp_path / "nonexistent.json")


def test_main_prints_the_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert main(["pr_summary", str(tmp_path)]) == 0
    assert "### Mapper outcomes (1 total)" in capsys.readouterr().out


def test_main_title_counts_escalations_from_the_reducer_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reducer passes --reducer-outcome to the title command; without it the count is always 0."""
    inputs = tmp_path / "inputs"
    write_outcome(inputs, "a", _fix_outcome())
    outcome_path = tmp_path / "integrator_outcome.json"
    outcome_path.write_text(
        json.dumps(
            {
                "escalations": [
                    {"kind": "HARNESS_DEFECT", "description_markdown": "Still open.", "member_ids": ["a#0"]},
                    {
                        "kind": "SUITE_DUPLICATION",
                        "description_markdown": "Already fixed.",
                        "resolved_in_commit_hash": "abc1234",
                    },
                ]
            }
        )
    )
    argv = [
        "pr_summary",
        str(inputs),
        "--title",
        "tmr-mngr/20260721085455/reducer",
        "--reducer-outcome",
        str(outcome_path),
    ]
    assert main(argv) == 0
    assert "1 escalations unresolved" in capsys.readouterr().out


def test_main_prints_the_title(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_outcome(tmp_path, "a", _fix_outcome())
    assert main(["pr_summary", str(tmp_path), "--title", "tmr-mngr/20260721085455/reducer"]) == 0
    assert capsys.readouterr().out.strip().startswith("TMR tmr-mngr 2026-07-21:")
