"""Unit tests for the test-mapreduce report module (data types + HTML generation)."""

import json
from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import Change
from imbue.mngr_tmr.report import ChangeKind
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.report import Escalation
from imbue.mngr_tmr.report import EscalationKind
from imbue.mngr_tmr.report import EscalationLocation
from imbue.mngr_tmr.report import IntegratorResult
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.report import SECTION_LABELS
from imbue.mngr_tmr.report import TestMapReduceResult
from imbue.mngr_tmr.report import TestResult
from imbue.mngr_tmr.report import _render_markdown
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report import load_integrator_outcome_file
from imbue.mngr_tmr.report import load_testing_agent_outcome
from imbue.mngr_tmr.report import merged_status_html
from imbue.mngr_tmr.report import report_section_of
from imbue.mngr_tmr.report import synthesize_missing_mapper_outcomes

SUCCEEDED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
FAILED_FIX = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="failed")}


def make_test_result(
    changes: dict[ChangeKind, Change] | None = None,
    errored: bool = False,
    before: bool | None = None,
    after: bool | None = None,
    escalations: tuple[Escalation, ...] = (),
) -> TestMapReduceResult:
    """Build a minimal TestMapReduceResult for testing render-internal helpers."""
    return TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        changes=changes if changes is not None else {},
        errored=errored,
        tests_passing_before=before,
        tests_passing_after=after,
        escalations=escalations,
    )


def _serialize_outcome(outcome: TestResult) -> dict[str, object]:
    return {
        "changes": {
            k.value: {"status": v.status.value, "summary_markdown": v.summary_markdown}
            for k, v in outcome.changes.items()
        },
        "errored": outcome.errored,
        "tests_passing_before": outcome.tests_passing_before,
        "tests_passing_after": outcome.tests_passing_after,
        "summary_markdown": outcome.summary_markdown,
        "test_runs": [
            {"run_name": r.run_name, "description_markdown": r.description_markdown} for r in outcome.test_runs
        ],
        "escalations": [
            {
                "kind": e.kind.value,
                "description_markdown": e.description_markdown,
                "locations": [{"path": loc.path, "line": loc.line} for loc in e.locations],
            }
            for e in outcome.escalations
        ],
    }


def _write_test_outcome(output_dir: Path, agent_name: AgentName, outcome: TestResult) -> None:
    target = output_dir / str(agent_name) / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_serialize_outcome(outcome)))


def write_integrator_outcome(output_dir: Path, agent_name: AgentName, payload: dict[str, object]) -> None:
    """Write an integrator outcome JSON where the reporter expects it."""
    target = output_dir / str(agent_name) / "test_output" / INTEGRATOR_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def make_metadata_and_outcome(
    output_dir: Path,
    agent_name: str,
    *,
    test_node_id: str = "t::t",
    branch_name: str | None = None,
    error_summary: str | None = None,
    changes: dict[ChangeKind, Change] | None = None,
    errored: bool = False,
    tests_passing_before: bool | None = None,
    tests_passing_after: bool | None = None,
    summary_markdown: str = "",
    escalations: tuple[Escalation, ...] = (),
    write_outcome: bool = True,
) -> AgentMetadata:
    """Build an ``AgentMetadata`` and (unless ``write_outcome`` is False) write
    its outcome JSON under ``output_dir/<agent_name>/test_output/``.

    Mirrors what orchestration would emit at runtime: errored agents have
    ``error_summary`` set and no outcome on disk; "running" agents have neither.
    """
    name = AgentName(agent_name)
    metadata = AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=name,
        task_id=test_node_id,
        branch_name=branch_name,
        error_summary=error_summary,
    )
    if error_summary is None and write_outcome:
        outcome = TestResult(
            changes=changes if changes is not None else {},
            errored=errored,
            tests_passing_before=tests_passing_before,
            tests_passing_after=tests_passing_after,
            summary_markdown=summary_markdown,
            escalations=escalations,
        )
        _write_test_outcome(output_dir, name, outcome)
    return metadata


# --- enum + dataclass smoke tests ---


def test_change_kind_values() -> None:
    assert ChangeKind.IMPROVE_TEST == "IMPROVE_TEST"
    assert ChangeKind.FIX_TEST == "FIX_TEST"
    assert ChangeKind.FIX_IMPL == "FIX_IMPL"
    assert ChangeKind.FIX_TUTORIAL == "FIX_TUTORIAL"


def test_change_status_values() -> None:
    assert ChangeStatus.SUCCEEDED == "SUCCEEDED"
    assert ChangeStatus.FAILED == "FAILED"


def test_report_section_values() -> None:
    assert ReportSection.TEST_AND_DOC_FIXES == "TEST_AND_DOC_FIXES"
    assert ReportSection.IMPL_FIXES == "IMPL_FIXES"
    assert ReportSection.FIX_FAILED == "FIX_FAILED"
    assert ReportSection.INDETERMINATE == "INDETERMINATE"
    assert ReportSection.CLEAN_PASS == "CLEAN_PASS"
    assert ReportSection.RUNNING == "RUNNING"


def test_test_result_empty() -> None:
    result = TestResult(tests_passing_before=True, tests_passing_after=True, summary_markdown="All good")
    assert result.changes == {}
    assert result.errored is False


def test_test_result_with_changes() -> None:
    changes = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed"),
        ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="Needs work"),
    }
    result = TestResult(changes=changes, tests_passing_before=False, tests_passing_after=True)
    assert len(result.changes) == 2


def test_test_map_reduce_result_with_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_baz",
        agent_name=AgentName("tmr-test-baz"),
        changes={ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="Fixed null check")},
        tests_passing_before=False,
        tests_passing_after=True,
        summary_markdown="Fixed missing null check",
        branch_name="tmr/20260101000000/test-baz",
    )
    assert result.branch_name == "tmr/20260101000000/test-baz"


def test_test_map_reduce_result_without_branch() -> None:
    result = TestMapReduceResult(
        test_node_id="tests/test_foo.py::test_ok",
        agent_name=AgentName("tmr-test-ok"),
        tests_passing_before=True,
        tests_passing_after=True,
    )
    assert result.branch_name is None


# --- report_section_of tests ---


def test_report_section_errored() -> None:
    assert report_section_of(make_test_result(errored=True)) == ReportSection.FAILED


def test_report_section_running() -> None:
    assert report_section_of(make_test_result()) == ReportSection.RUNNING


def test_report_section_clean_pass() -> None:
    assert report_section_of(make_test_result(before=True, after=True)) == ReportSection.CLEAN_PASS


def test_report_section_test_and_doc_fixes() -> None:
    assert (
        report_section_of(make_test_result(changes=SUCCEEDED_FIX, before=False, after=True))
        == ReportSection.TEST_AND_DOC_FIXES
    )


def test_report_section_impl_fix_wins_over_a_test_fix() -> None:
    """An agent that fixed both made an implementation fix, and that is what a reviewer needs."""
    both = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed the test"),
        ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed the impl"),
    }
    assert report_section_of(make_test_result(changes=both, before=False, after=True)) == ReportSection.IMPL_FIXES


def test_report_section_impl_fixes() -> None:
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")}
    assert report_section_of(make_test_result(changes=impl_fix, before=False, after=True)) == ReportSection.IMPL_FIXES


def test_report_section_all_changes_failed_is_fix_failed() -> None:
    """Every attempted change having failed means the agent landed nothing."""
    assert (
        report_section_of(make_test_result(changes=FAILED_FIX, before=False, after=False)) == ReportSection.FIX_FAILED
    )


def test_report_section_partial_failure_keeps_fix_section() -> None:
    """A failed change alongside a succeeded one still counts as a fix."""
    mixed = {
        ChangeKind.FIX_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed"),
        ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="could not"),
    }
    assert (
        report_section_of(make_test_result(changes=mixed, before=False, after=True))
        == ReportSection.TEST_AND_DOC_FIXES
    )


def test_report_section_no_changes_tests_failing_is_indeterminate() -> None:
    """The catch-all is its own section, so it stops masquerading as a failed fix."""
    assert report_section_of(make_test_result(before=False, after=False)) == ReportSection.INDETERMINATE


def test_report_section_ignores_escalations() -> None:
    """Escalations are orthogonal: a clean pass carrying one is still a clean pass."""
    escalated = make_test_result(
        before=True,
        after=True,
        escalations=(Escalation(description_markdown="Siblings carry it.", kind=EscalationKind.SUITE_DUPLICATION),),
    )
    assert report_section_of(escalated) == ReportSection.CLEAN_PASS


# --- render_markdown tests ---


def test_render_markdown_bold() -> None:
    result = _render_markdown("**bold**")
    assert "<strong>bold</strong>" in result


def test_render_markdown_plain_text() -> None:
    result = _render_markdown("plain text")
    assert "plain text" in result


# --- _merged_status tests ---


def test_merged_status_no_integrator() -> None:
    r = make_test_result(before=True, after=True)
    assert merged_status_html(r.branch_name, None) == ""


def test_merged_status_no_branch() -> None:
    r = make_test_result(before=True, after=True)
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/a",))
    assert merged_status_html(r.branch_name, integrator) == ""


def test_merged_status_squashed() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/a",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/a",))
    assert "10003" in merged_status_html(r.branch_name, integrator)


def test_merged_status_impl_priority() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/b",
        tests_passing_before=False,
        tests_passing_after=True,
        changes={ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed")},
    )
    integrator = IntegratorResult(impl_priority=("mngr-tmr/b",), impl_commit_hashes={"mngr-tmr/b": "abc123def"})
    status = merged_status_html(r.branch_name, integrator)
    assert "abc123def" in status
    assert "<code>" in status


def test_merged_status_failed() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/c",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(failed=("mngr-tmr/c",))
    assert "10007" in merged_status_html(r.branch_name, integrator)


def test_merged_status_not_in_integrator() -> None:
    r = TestMapReduceResult(
        test_node_id="t::t",
        agent_name=AgentName("a"),
        branch_name="mngr-tmr/d",
        tests_passing_before=False,
        tests_passing_after=True,
        changes=SUCCEEDED_FIX,
    )
    integrator = IntegratorResult(squashed_branches=("mngr-tmr/other",))
    assert merged_status_html(r.branch_name, integrator) == ""


# --- HTML report tests ---


def test_generate_html_report(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "tmr-test-pass",
            test_node_id="tests/test_a.py::test_pass",
            tests_passing_before=True,
            tests_passing_after=True,
            summary_markdown="Passed immediately",
        ),
        make_metadata_and_outcome(
            output_dir,
            "tmr-test-fixed",
            test_node_id="tests/test_b.py::test_fixed",
            branch_name="mngr-tmr/test-fixed",
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="Fixed missing import",
        ),
    ]
    result_path = generate_html_report(agents, output_dir)
    assert result_path == output_dir / "index.html"
    assert result_path.exists()
    content = result_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Clean pass" in content
    assert "Test and doc fixes" in content
    assert 'class="toc-sidebar"' in content


def test_generate_html_report_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "subdir" / "nested"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    generate_html_report(agents, output_dir)
    assert (output_dir / "index.html").exists()


def test_generate_html_report_all_report_sections(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    impl_fix = {ChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="fixed impl")}
    unresolved_changes = {ChangeKind.FIX_TEST: Change(status=ChangeStatus.FAILED, summary_markdown="could not fix")}
    agents = [
        make_metadata_and_outcome(output_dir, "running-agent", write_outcome=False),
        make_metadata_and_outcome(
            output_dir, "test-and-doc", changes=SUCCEEDED_FIX, tests_passing_before=False, tests_passing_after=True
        ),
        make_metadata_and_outcome(
            output_dir, "impl-fix", changes=impl_fix, tests_passing_before=False, tests_passing_after=True
        ),
        make_metadata_and_outcome(
            output_dir, "fix-failed", changes=unresolved_changes, tests_passing_before=False, tests_passing_after=False
        ),
        # No changes attempted and the test is not passing: fits no other section.
        make_metadata_and_outcome(output_dir, "indeterminate", tests_passing_before=False, tests_passing_after=False),
        make_metadata_and_outcome(output_dir, "failed", error_summary="boom"),
        make_metadata_and_outcome(output_dir, "clean", tests_passing_before=True, tests_passing_after=True),
    ]
    result_path = generate_html_report(agents, output_dir)
    content = result_path.read_text()
    for sec in ReportSection:
        assert SECTION_LABELS[sec] in content


def test_generate_html_report_empty_agents(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result_path = generate_html_report([], output_dir)
    assert "0 test(s)" in result_path.read_text()


def test_generate_html_report_with_integrator(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "agent-a",
            branch_name="mngr-tmr/a",
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
        ),
    ]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-abc123"),
        branch_name="mngr-tmr/integrated-abc123",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {"squashed_branches": ["mngr-tmr/a"], "squashed_commit_hash": "abc", "impl_priority": [], "failed": []},
    )
    result_path = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta)
    content = result_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "Merged?" in content


def test_generate_html_report_integrator_with_failures(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-abc123"),
        branch_name="mngr-tmr/integrated-abc123",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {"squashed_branches": ["mngr-tmr/a"], "failed": ["mngr-tmr/b"]},
    )
    result_path = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta)
    assert result_path.exists()


def test_generate_html_report_without_integrator(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    result_path = generate_html_report(agents, output_dir)
    assert "Test Map-Reduce Report" in result_path.read_text()


def test_generate_html_report_splits_resolved_from_unresolved_escalations(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-render"),
        branch_name="mngr-tmr/integrated-esc-render",
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {
            "squashed_branches": ["mngr-tmr/a"],
            "escalations": [
                {
                    "kind": "HARNESS_DEFECT",
                    "description_markdown": "codex needs OpenAI creds\n\nProvide a **fake-codex** fixture",
                },
                {
                    "kind": "SUITE_DUPLICATION",
                    "description_markdown": "Extracted assert_agent_running helper",
                    "resolved_in_commit_hash": "abc1234def5",
                },
            ],
        },
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta).read_text()
    assert "Unresolved escalations (1)" in content
    assert "Resolved escalations (1)" in content
    assert "codex needs OpenAI creds" in content
    # The unresolved one carries its whole description, rendered as markdown.
    assert "<strong>fake-codex</strong>" in content
    # The resolved one is a row naming its commit.
    assert "abc1234def" in content


def test_generate_html_report_no_escalation_sections_when_empty(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-empty"),
        branch_name="mngr-tmr/integrated-esc-empty",
    )
    write_integrator_outcome(output_dir, integrator_meta.agent_name, {"squashed_branches": ["mngr-tmr/a"]})
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta).read_text()
    assert "Unresolved escalations" not in content
    assert "Resolved escalations" not in content


def test_generate_html_report_escalation_summary_escaped(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator_meta = AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=AgentName("tmr-integrator-esc-xss"),
        branch_name=None,
    )
    write_integrator_outcome(
        output_dir,
        integrator_meta.agent_name,
        {
            "escalations": [
                {
                    "kind": "HARNESS_DEFECT",
                    "description_markdown": "<script>alert('xss')</script>",
                    "resolved_in_commit_hash": "abc1234",
                }
            ]
        },
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator_meta).read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content


def test_generate_html_report_html_escaped(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    xss_branch = "<script>alert('xss')</script>"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "xss-agent",
            test_node_id="t::xss",
            branch_name=xss_branch,
            changes=SUCCEEDED_FIX,
            tests_passing_before=False,
            tests_passing_after=True,
            summary_markdown="<img onerror=alert(1)>",
        )
    ]
    result_path = generate_html_report(agents, output_dir)
    content = result_path.read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content


# --- escalation aggregation into the report ---


def test_report_includes_mapper_escalations(tmp_path: Path) -> None:
    """A mapper's escalation must reach the report even though its test passed."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "clean-but-escalating",
            test_node_id="tests/test_a.py::test_a",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(
                Escalation(
                    description_markdown="rsync mark superfluous suite-wide\n\nSix siblings carry it.",
                    kind=EscalationKind.SUITE_DUPLICATION,
                    locations=(EscalationLocation(path="libs/mngr/conftest.py", line=12),),
                ),
            ),
        )
    ]
    content = generate_html_report(agents, output_dir).read_text()
    assert "rsync mark superfluous suite-wide" in content
    assert "Suite duplication" in content
    # It renders inside its own test's row rather than in a separate list.
    # The node id carries a <wbr> soft-wrap hint after each "::".
    assert content.index("tests/test_a.py::<wbr>test_a") < content.index("rsync mark superfluous suite-wide")
    # The location is what lets a reader jump to the code rather than the reporter.
    assert "libs/mngr/conftest.py:12" in content


def test_escalations_render_on_their_row_without_an_integrator(tmp_path: Path) -> None:
    """The report is regenerated on every poll, long before the integrator exists."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "escalating",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(Escalation(description_markdown="Seen everywhere.", kind=EscalationKind.HARNESS_DEFECT),),
        )
    ]
    content = generate_html_report(agents, output_dir).read_text()
    assert "Escalations (1)" in content
    assert "Seen everywhere." in content
    # No integrator yet, so there is no grouped view and no Integration heading.
    assert ">Integration<" not in content
    # The per-test sections still group under Tests.
    assert ">Tests<" in content


def test_report_carries_every_mapper_escalation_id(tmp_path: Path) -> None:
    """Ids are derived, so an agent's Nth escalation is addressable without the agent naming it."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "two-escalations",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(
                Escalation(description_markdown="First.", kind=EscalationKind.UNCAUGHT_BUG),
                Escalation(description_markdown="Second.", kind=EscalationKind.HARNESS_DEFECT),
            ),
        )
    ]
    content = generate_html_report(agents, output_dir).read_text()
    assert "two-escalations#0" in content
    assert "two-escalations#1" in content


def test_report_warns_when_a_mapper_escalation_is_in_no_group(tmp_path: Path) -> None:
    """An ungrouped escalation is missing from the grouped view, which must not be silent."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "ungrouped",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(Escalation(description_markdown="Nobody grouped me.", kind=EscalationKind.UNCAUGHT_BUG),),
        )
    ]
    integrator = AgentMetadata(
        kind=AgentKind.REDUCER, agent_name=AgentName("red"), task_id=None, branch_name="b", error_summary=None
    )
    write_integrator_outcome(
        output_dir,
        AgentName("red"),
        {"escalations": [{"kind": "HARNESS_DEFECT", "description_markdown": "Unrelated group.", "member_ids": []}]},
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator).read_text()
    assert "belong to no group" in content
    # And the escalation itself is still shown in full.
    assert "Nobody grouped me." in content


def test_report_orders_integrator_originated_escalations_first(tmp_path: Path) -> None:
    """A plain member-count sort would bury the findings no mapper could produce."""
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "m", tests_passing_before=True, tests_passing_after=True)]
    integrator = AgentMetadata(
        kind=AgentKind.REDUCER, agent_name=AgentName("red"), task_id=None, branch_name="b", error_summary=None
    )
    write_integrator_outcome(
        output_dir,
        AgentName("red"),
        {
            "escalations": [
                {
                    "kind": "SUITE_DUPLICATION",
                    "description_markdown": "MANY-REPORTED",
                    "member_ids": ["m#0", "m#1", "m#2"],
                },
                {"kind": "UNCAUGHT_BUG", "description_markdown": "INTEGRATOR-FOUND", "member_ids": []},
                {"kind": "HARNESS_DEFECT", "description_markdown": "ONE-REPORTED", "member_ids": ["m#3"]},
            ]
        },
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator).read_text()
    assert content.index("INTEGRATOR-FOUND") < content.index("MANY-REPORTED")
    assert content.index("MANY-REPORTED") < content.index("ONE-REPORTED")


def test_outcome_with_a_missing_escalation_kind_is_dropped(tmp_path: Path) -> None:
    """kind is required now; a missing one must not silently become some default."""
    output_dir = tmp_path / "out"
    target = output_dir / "no-kind" / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"changes": {}, "escalations": [{"description_markdown": "No kind here."}]}))
    assert load_testing_agent_outcome(AgentName("no-kind"), output_dir) is None


def test_outcome_with_an_unknown_escalation_kind_is_dropped(tmp_path: Path) -> None:
    """An outcome written against the old vocabulary parses to nothing rather than crashing."""
    output_dir = tmp_path / "out"
    target = output_dir / "old-kind" / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"changes": {}, "escalations": [{"kind": "BLOCKER", "description_markdown": "Old vocabulary."}]})
    )
    assert load_testing_agent_outcome(AgentName("old-kind"), output_dir) is None


def test_malformed_integrator_escalations_yield_no_outcome(tmp_path: Path) -> None:
    """The integrator loader must warn and return None rather than raise on every poll tick."""
    outcome_path = tmp_path / "integrator_outcome.json"
    outcome_path.write_text(json.dumps({"escalations": [{"description_markdown": "No kind."}]}))
    assert load_integrator_outcome_file(outcome_path) is None

    outcome_path.write_text(
        json.dumps({"escalations": [{"kind": "NOT_A_KIND", "description_markdown": "Unknown kind."}]})
    )
    assert load_integrator_outcome_file(outcome_path) is None


def test_a_report_renders_when_the_integrator_outcome_is_malformed(tmp_path: Path) -> None:
    """A bad integrator outcome must not take the whole report down mid-run."""
    output_dir = tmp_path / "out"
    agents = [make_metadata_and_outcome(output_dir, "a", tests_passing_before=True, tests_passing_after=True)]
    integrator = AgentMetadata(
        kind=AgentKind.REDUCER, agent_name=AgentName("red"), task_id=None, branch_name="b", error_summary=None
    )
    write_integrator_outcome(output_dir, AgentName("red"), {"escalations": [{"description_markdown": "No kind."}]})
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator).read_text()
    assert "Test Map-Reduce Report" in content


def test_escalation_locations_render_without_a_line(tmp_path: Path) -> None:
    """Agents are told line may be omitted, so the path alone has to render."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "no-line",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(
                Escalation(
                    description_markdown="Whole file is the problem.",
                    kind=EscalationKind.HARNESS_DEFECT,
                    locations=(EscalationLocation(path="apps/minds/conftest.py"),),
                ),
            ),
        )
    ]
    content = generate_html_report(agents, output_dir).read_text()
    assert "apps/minds/conftest.py" in content
    assert "apps/minds/conftest.py:" not in content


def test_escalation_markdown_cannot_inject_raw_html(tmp_path: Path) -> None:
    """Descriptions are agent-authored and render through |safe, so raw HTML must not pass."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "injecting",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(
                Escalation(
                    description_markdown="<script>alert('xss')</script>",
                    kind=EscalationKind.UNCAUGHT_BUG,
                ),
            ),
        )
    ]
    content = generate_html_report(agents, output_dir).read_text()
    assert "<script>alert" not in content
    assert "&lt;script&gt;" in content


def test_malformed_escalation_locations_drop_the_outcome(tmp_path: Path) -> None:
    """locations is nested agent-written JSON; its parse errors must be caught like the rest."""
    output_dir = tmp_path / "out"
    for agent, locations in [
        ("string-locations", ["libs/mngr/conftest.py"]),
        ("no-path", [{"line": 3}]),
        ("bad-line", [{"path": "libs/mngr/conftest.py", "line": "twelve"}]),
    ]:
        target = output_dir / agent / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "changes": {},
                    "escalations": [
                        {
                            "kind": "HARNESS_DEFECT",
                            "description_markdown": "Somewhere.",
                            "locations": locations,
                        }
                    ],
                }
            )
        )
        assert load_testing_agent_outcome(AgentName(agent), output_dir) is None, agent


def test_no_integration_report_when_the_integrator_has_not_published(tmp_path: Path) -> None:
    """A reducer that exists but has not reported must not be blamed for an incomplete grouping."""
    output_dir = tmp_path / "out"
    agents = [
        make_metadata_and_outcome(
            output_dir,
            "escalating",
            tests_passing_before=True,
            tests_passing_after=True,
            escalations=(Escalation(description_markdown="Seen everywhere.", kind=EscalationKind.HARNESS_DEFECT),),
        )
    ]
    # Metadata for a reducer that has written no outcome file at all.
    integrator = AgentMetadata(
        kind=AgentKind.REDUCER, agent_name=AgentName("red"), task_id=None, branch_name="b", error_summary=None
    )
    content = generate_html_report(agents, output_dir, integrator_metadata=integrator).read_text()
    assert "Integration report" not in content
    assert "belong to no group" not in content
    # The raw escalation is still shown; only the grouped view is absent.
    assert "Seen everywhere." in content


def test_outcome_without_escalations_still_parses(tmp_path: Path) -> None:
    """An outcome written before the field existed must not be discarded."""
    output_dir = tmp_path / "out"
    target = output_dir / "legacy" / "test_output" / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"changes": {}, "tests_passing_before": True, "tests_passing_after": True}))
    outcome = load_testing_agent_outcome(AgentName("legacy"), output_dir)
    assert outcome is not None
    assert outcome.escalations == ()


def test_outcome_cache_is_keyed_by_output_dir(tmp_path: Path) -> None:
    """The same agent name under two output dirs must not return the first one's outcome.

    The reducer's inputs directory and the orchestrator's output directory both
    hold per-agent outcomes under the same agent names.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    name = AgentName("shared-name")
    _write_test_outcome(first, name, TestResult(summary_markdown="FROM-FIRST"))
    _write_test_outcome(second, name, TestResult(summary_markdown="FROM-SECOND"))
    from_first = load_testing_agent_outcome(name, first)
    from_second = load_testing_agent_outcome(name, second)
    assert from_first is not None and from_second is not None
    assert from_first.summary_markdown == "FROM-FIRST"
    assert from_second.summary_markdown == "FROM-SECOND"


# --- synthesizing outcomes for failed mappers ---


def _errored_mapper(name: str) -> AgentMetadata:
    return AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=AgentName(name),
        task_id="tests/test_x.py::test_x",
        branch_name=None,
        error_summary="Agent timed out",
    )


def test_synthesize_writes_an_errored_outcome_for_a_failed_mapper(tmp_path: Path) -> None:
    written = synthesize_missing_mapper_outcomes(tmp_path, [_errored_mapper("dead-agent")])
    assert written == [AgentName("dead-agent")]
    outcome = load_testing_agent_outcome(AgentName("dead-agent"), tmp_path)
    assert outcome is not None
    assert outcome.errored is True
    assert outcome.summary_markdown == "Agent timed out"


def test_synthesize_makes_a_failed_mapper_count_as_failed(tmp_path: Path) -> None:
    """The whole point: the synthetic file lands the failed mapper in the FAILED section."""
    synthesize_missing_mapper_outcomes(tmp_path, [_errored_mapper("dead-agent")])
    outcome = load_testing_agent_outcome(AgentName("dead-agent"), tmp_path)
    assert outcome is not None
    row = TestMapReduceResult(test_node_id="t", agent_name=AgentName("dead-agent"), errored=outcome.errored)
    assert report_section_of(row) == ReportSection.FAILED


def test_synthesize_does_not_overwrite_a_real_outcome(tmp_path: Path) -> None:
    """A mapper that both errored-in-metadata and left a real file keeps its file."""
    name = AgentName("has-real-outcome")
    _write_test_outcome(tmp_path, name, TestResult(summary_markdown="REAL", errored=False))
    written = synthesize_missing_mapper_outcomes(tmp_path, [_errored_mapper(str(name))])
    assert written == []
    outcome = load_testing_agent_outcome(name, tmp_path)
    assert outcome is not None
    assert outcome.summary_markdown == "REAL"


def test_synthesize_ignores_successful_mappers_and_the_reducer(tmp_path: Path) -> None:
    ok_mapper = AgentMetadata(
        kind=AgentKind.MAPPER, agent_name=AgentName("ok"), task_id="t", branch_name=None, error_summary=None
    )
    reducer = AgentMetadata(
        kind=AgentKind.REDUCER, agent_name=AgentName("red"), task_id=None, branch_name="b", error_summary="boom"
    )
    assert synthesize_missing_mapper_outcomes(tmp_path, [ok_mapper, reducer]) == []
