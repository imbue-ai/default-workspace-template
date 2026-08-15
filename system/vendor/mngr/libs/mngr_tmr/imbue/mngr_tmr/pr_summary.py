"""Build the human-readable part of the TMR pull request description.

The reducer agent opens the run's PR itself, and its body should carry the
headline findings rather than just a link to the report. Tabulating 80 mapper
outcomes is exactly the kind of work an agent does slowly and inaccurately, so
the reducer shells out to this module instead: it reads the same outcome JSON
files the reducer already has under its inputs directory and emits finished
markdown on stdout.

Usage (from the reducer prompt)::

    python -m imbue.mngr_tmr.pr_summary <inputs_dir>

Layout of ``inputs_dir`` matches what the orchestrator rsyncs to the reducer:
``<inputs_dir>/<agent_name>/test_output/testing_agent_outcome.json``.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import EXTRACTED_TEST_OUTPUT_DIR
from imbue.mngr_tmr.report import IntegratorEscalation
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.report import TestMapReduceResult
from imbue.mngr_tmr.report import escalation_kind_label
from imbue.mngr_tmr.report import escalation_scale_label
from imbue.mngr_tmr.report import first_line
from imbue.mngr_tmr.report import load_integrator_outcome_file
from imbue.mngr_tmr.report import load_testing_agent_outcome
from imbue.mngr_tmr.report import report_section_of
from imbue.mngr_tmr.report import section_label
from imbue.mngr_tmr.report import sort_escalations
from imbue.mngr_tmr.report import split_summary

# Order the status breakdown reads in: the sections a reviewer most needs to act
# on come first, and the uneventful ones last.
_BREAKDOWN_ORDER: list[ReportSection] = [
    ReportSection.IMPL_FIXES,
    ReportSection.TEST_AND_DOC_FIXES,
    ReportSection.FIX_FAILED,
    ReportSection.INDETERMINATE,
    ReportSection.FAILED,
    ReportSection.CLEAN_PASS,
    ReportSection.RUNNING,
]


def _escape_cell(text: str) -> str:
    """Make a string safe to place in a markdown table cell.

    Pipes would split the cell and newlines would end the row, so both are
    neutralized. Escalation detail is markdown written by an agent, so it can
    contain either.
    """
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def collect_results(inputs_dir: Path) -> list[TestMapReduceResult]:
    """Read every mapper outcome under ``inputs_dir`` into a result row.

    Agent directories with no readable outcome file are skipped: those mappers
    never published, which the orchestrator already reports separately.
    """
    results: list[TestMapReduceResult] = []
    if not inputs_dir.is_dir():
        return results
    for agent_dir in sorted(inputs_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        # Only directories that actually hold an outcome are agent directories.
        # The inputs directory sits inside the reducer's work_dir, so it can
        # also contain unrelated entries (dotfiles, tool state) whose names are
        # not valid agent names at all.
        if not (agent_dir / EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME).is_file():
            continue
        agent_name = AgentName(agent_dir.name)
        outcome = load_testing_agent_outcome(agent_name, inputs_dir)
        if outcome is None:
            continue
        results.append(
            TestMapReduceResult(
                # The agent name is the only per-test identity available here;
                # the pytest node id lives in orchestrator-side metadata, which
                # the reducer does not receive.
                test_node_id=agent_dir.name,
                agent_name=agent_name,
                changes=outcome.changes,
                errored=outcome.errored,
                tests_passing_before=outcome.tests_passing_before,
                tests_passing_after=outcome.tests_passing_after,
                summary_markdown=outcome.summary_markdown,
                test_runs=outcome.test_runs,
                escalations=outcome.escalations,
            )
        )
    return results


def build_status_breakdown(results: list[TestMapReduceResult]) -> str:
    """Render the per-status mapper counts as a markdown table."""
    counts = Counter(report_section_of(result) for result in results)
    lines = [
        f"### Mapper outcomes ({len(results)} total)",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for section in _BREAKDOWN_ORDER:
        count = counts.get(section, 0)
        if count:
            lines.append(f"| {section_label(section)} | {count} |")
    return "\n".join(lines)


def build_escalations_section(reducer_escalations: Sequence[IntegratorEscalation]) -> str:
    """Render the integrator's escalations: unresolved in full, then resolved in one line each.

    Unresolved escalations are what a reviewer has to act on, so they carry their
    whole description. Resolved ones are already fixed and verified, so a line
    and their scale is enough, and the commit carries the rest.

    Both halves report the same three things about an escalation -- its text, its
    kind, and how many mapper reports it covers -- in the same words the HTML
    report uses, with resolved ones adding the commit. Unresolved ones are
    headings rather than table rows only because a markdown table cell cannot
    hold the multi-paragraph description they carry.

    Only the integrator's escalations appear. They are groupings of the mappers',
    and a run of this size produces hundreds of mapper reports -- reproducing
    them here overran GitHub's 65 KB body limit, which is what the grouping
    exists to fix. The full raw list lives in the HTML report.
    """
    unresolved = sort_escalations([e for e in reducer_escalations if not e.is_resolved])
    resolved = sort_escalations([e for e in reducer_escalations if e.is_resolved])
    if not unresolved and not resolved:
        return "### Escalations\n\nNone reported."

    lines: list[str] = []
    if unresolved:
        lines += [f"### Unresolved escalations ({len(unresolved)})", ""]
        for escalation in unresolved:
            # One split, so the heading and the body below it cannot disagree
            # about where the summary sentence ends.
            summary, detail = split_summary(escalation.description_markdown)
            lines += [
                f"#### {summary}",
                "",
                f"*{escalation_kind_label(escalation.kind)} -- {escalation_scale_label(escalation)}*",
                "",
                detail,
                "",
            ]
    if resolved:
        if lines:
            lines.append("")
        lines += [
            f"### Resolved escalations ({len(resolved)})",
            "",
            "| Escalation | Kind | Reports | Commit |",
            "| --- | --- | --- | --- |",
        ]
        for escalation in resolved:
            summary = _escape_cell(first_line(escalation.description_markdown))
            commit = escalation.resolved_in_commit_hash or ""
            lines.append(
                f"| {summary} | {escalation_kind_label(escalation.kind)} "
                f"| {escalation_scale_label(escalation)} | `{commit[:10]}` |"
            )
    return "\n".join(lines).rstrip()


def _reducer_escalations(reducer_outcome_path: Path | None) -> tuple[IntegratorEscalation, ...]:
    """Read the integrator's escalations, or none when it has not written them yet.

    The title is built before the PR exists and the outcome file may legitimately
    be absent (a run whose integrator failed still opens a PR), so a missing file
    means "no escalations to report" rather than an error.
    """
    if reducer_outcome_path is None:
        return ()
    outcome = load_integrator_outcome_file(reducer_outcome_path)
    return outcome.escalations if outcome is not None else ()


def build_pr_summary(inputs_dir: Path, reducer_outcome_path: Path | None = None) -> str:
    """Build the full markdown summary block for the PR description.

    ``reducer_outcome_path`` is the integrator's own outcome file, when it has
    already been written; its escalations are the run's escalation sections.
    """
    results = collect_results(inputs_dir)
    reducer_escalations = _reducer_escalations(reducer_outcome_path)
    return "\n\n".join([build_status_breakdown(results), build_escalations_section(reducer_escalations)])


def _format_run_date(run_name: str) -> str:
    """Render the leading YYYYMMDD of a run name as YYYY-MM-DD.

    Run names are UTC YYYYMMDDHHMMSS timestamps generated by the framework. A
    name that does not follow that shape is passed through unchanged rather
    than guessed at.
    """
    if len(run_name) >= 8 and run_name[:8].isdigit():
        return f"{run_name[:4]}-{run_name[4:6]}-{run_name[6:8]}"
    return run_name


def build_pr_title(
    branch_name: str,
    results: list[TestMapReduceResult],
    reducer_escalations: Sequence[IntegratorEscalation] = (),
) -> str:
    """Build the PR title: mechanical, but readable at a glance in a PR list.

    ``branch_name`` is the reducer's own branch, ``<variant>/<run>/reducer``,
    which is where the variant and run name come from.

    The escalation count is the number of *unresolved* integrator escalations --
    the problems still needing a person. Counting raw mapper escalations instead
    put "429 escalated" in a title whose run held 21 distinct problems, and
    counting resolved ones too would advertise work already done.
    """
    parts = branch_name.split("/")
    variant = parts[0] if parts else branch_name
    run_name = parts[1] if len(parts) > 1 else ""

    counts = Counter(report_section_of(result) for result in results)
    fixes = counts.get(ReportSection.IMPL_FIXES, 0) + counts.get(ReportSection.TEST_AND_DOC_FIXES, 0)
    unresolved = (
        counts.get(ReportSection.FIX_FAILED, 0)
        + counts.get(ReportSection.INDETERMINATE, 0)
        + counts.get(ReportSection.FAILED, 0)
    )
    escalations = sum(1 for escalation in reducer_escalations if not escalation.is_resolved)

    # Only non-zero facts earn a place, so the common "everything was clean"
    # run gets a short title instead of a row of zeros.
    summary_bits = []
    if fixes:
        summary_bits.append(f"{fixes} fixed")
    if unresolved:
        # Not "unresolved": that word now names an escalation state, and these
        # are agents that did not finish, which is a different thing.
        summary_bits.append(f"{unresolved} unfinished")
    if escalations:
        summary_bits.append(f"{escalations} escalations unresolved")
    if not summary_bits:
        summary_bits.append(f"{len(results)} tests clean")

    date_part = f" {_format_run_date(run_name)}" if run_name else ""
    return f"TMR {variant}{date_part}: {', '.join(summary_bits)}"


def main(argv: list[str]) -> int:
    """Print either the PR body summary or the PR title."""
    parser = argparse.ArgumentParser(description="Build TMR pull request title/body from mapper outcomes.")
    parser.add_argument("inputs_dir", type=Path, help="Directory of per-mapper output directories")
    parser.add_argument(
        "--reducer-outcome",
        type=Path,
        help="Path to the integrator's own outcome file, whose escalations are the run's escalation sections "
        "and whose unresolved count the title reports",
    )
    parser.add_argument(
        "--title",
        metavar="BRANCH",
        help="Print the PR title for this reducer branch instead of the body summary",
    )
    args = parser.parse_args(argv[1:])

    if args.title:
        print(build_pr_title(args.title, collect_results(args.inputs_dir), _reducer_escalations(args.reducer_outcome)))
    else:
        print(build_pr_summary(args.inputs_dir, args.reducer_outcome))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
