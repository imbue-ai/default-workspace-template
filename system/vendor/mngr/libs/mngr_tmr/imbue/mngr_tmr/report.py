"""HTML report generation for the test-mapreduce plugin.

The reporter takes a list of ``AgentMetadata`` from orchestration and
reads each agent's outcome JSON from ``output_dir/<agent_name>/`` on
demand. Outcome JSON shape is a contract between the agents and this
module; orchestration does not parse it. Parsed outcomes are cached
in-process (test-agent outcomes and the integrator outcome are
immutable once an agent has published them, so caching is safe).

The HTML template, the CSS, and the panel JS live under ``report_assets/``
and are rendered with Jinja2. This module's job is to assemble the
context dict the template renders against.

The test-mapreduce-specific data types (``TestResult``, ``Change``, etc.)
also live here -- they're only used by this module. Framework-side types
live in ``imbue.mngr_mapreduce.data_types``.
"""

import html
import json
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from importlib.resources import files
from pathlib import Path
from typing import Any
from typing import TypeVar

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from loguru import logger
from markdown_it import MarkdownIt
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_CSS
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_JS
from imbue.mngr.utils.detail_renderer import DETAIL_CSS
from imbue.mngr.utils.detail_renderer import render_test_detail
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


class ChangeKind(UpperCaseStrEnum):
    """What kind of change the agent attempted."""

    IMPROVE_TEST = auto()
    FIX_TEST = auto()
    FIX_IMPL = auto()
    FIX_TUTORIAL = auto()


class ChangeStatus(UpperCaseStrEnum):
    """Whether the change succeeded.

    There is deliberately no BLOCKED status. "I could not finish this change"
    and "this needs attention beyond my test" are different axes: an agent that
    cleanly fixes its own test may still have spotted a suite-wide problem, and
    a status field cannot carry both. The second axis lives in ``escalations``,
    which is orthogonal to the outcome and to every change's status.
    """

    SUCCEEDED = auto()
    FAILED = auto()


class Change(FrozenModel):
    """One change the agent attempted."""

    status: ChangeStatus = Field(description="Whether the change succeeded or failed")
    summary_markdown: str = Field(description="Markdown description of what was done or attempted")


class EscalationKind(UpperCaseStrEnum):
    """What kind of work an escalation is asking for.

    The vocabulary names the work rather than what happened to the reporting
    agent. An earlier BLOCKER/SHARED_PATTERN split described the reporter's own
    experience, which put 93% of a run's escalations in one bucket and told a
    reader nothing about what to do with them. "Did this stop me?" is not
    encoded here at all: it does not survive grouping, since one group collects
    reports from agents it stopped and agents it did not.

    UNCAUGHT_BUG: a real defect the agent noticed that no test fails on.
    FIX_DIRECTION_AMBIGUOUS: the test and the behavior disagree, and the
    docstring does not settle which of them is wrong.
    HARNESS_DEFECT: shared test infrastructure is broken, misreports, or lacks a
    capability or credential the tests need.
    SUITE_DUPLICATION: N local patches want one shared change.
    """

    UNCAUGHT_BUG = auto()
    FIX_DIRECTION_AMBIGUOUS = auto()
    HARNESS_DEFECT = auto()
    SUITE_DUPLICATION = auto()


class EscalationLocation(FrozenModel):
    """Where in the tree an escalation points.

    Reader-facing: it is what lets the report link an escalation to the code it
    concerns, which the pytest node id of the reporting agent cannot do (that
    identifies who noticed, not what needs to change).
    """

    path: str = Field(description="Repository-relative path the escalation concerns")
    line: int | None = Field(default=None, description="1-based line number, when the escalation names one")


class Escalation(FrozenModel):
    """Something needing attention beyond the reporting mapper's own scope.

    Orthogonal to the reporter's own success: a passing test can still raise one.
    """

    description_markdown: str = Field(
        description="Markdown description of the problem and the change it needs. Its first line is a "
        "one-sentence summary, which is what collapsed list views render."
    )
    kind: EscalationKind = Field(description="What kind of work this escalation is asking for")
    locations: tuple[EscalationLocation, ...] = Field(
        default=(), description="Places in the tree this escalation concerns"
    )


class IntegratorEscalation(FrozenModel):
    """One escalation as the integrator reports it: a group of mapper escalations.

    The integrator is the only step that sees every mapper's report at once, so
    its escalations are groupings of theirs -- many agents describe one problem
    in their own words, and it is the grouping that makes the problem legible.

    ``member_ids`` may be empty: the integrator also reads the whole integrated
    diff, so it finds problems no single mapper could see (two mappers reaching
    opposite conclusions, for instance).
    """

    kind: EscalationKind = Field(description="What kind of work this escalation is asking for")
    description_markdown: str = Field(
        description="Markdown description of the problem, its scale, and what it needs. Its first line is a "
        "one-sentence summary, which is what collapsed list views render."
    )
    member_ids: tuple[str, ...] = Field(
        default=(), description="Ids of the mapper escalations this group covers; empty for the integrator's own"
    )
    resolved_in_commit_hash: str | None = Field(
        default=None,
        description="Commit that resolved this escalation. Its presence is what marks the escalation resolved, "
        "so an escalation the integrator judged to need no change reads as unresolved.",
    )

    @property
    def is_resolved(self) -> bool:
        """Whether the integrator resolved this escalation within the run."""
        return self.resolved_in_commit_hash is not None


class ReportSection(UpperCaseStrEnum):
    """Derived section for HTML report grouping and coloring.

    FIX_FAILED covers results where the coding agent tried every change it
    attempted and landed none. FAILED is reserved for infrastructure failures:
    launch failures, agent timeouts, missing details -- cases where the agent
    never had a chance to produce a real verdict. INDETERMINATE is the explicit
    catch-all, so a result that fits no other section stops masquerading as a
    failed fix.

    Escalations are deliberately not a section: they are orthogonal to the
    outcome, so they get their own report sections instead (a clean pass may
    carry one).
    """

    IMPL_FIXES = auto()
    TEST_AND_DOC_FIXES = auto()
    FIX_FAILED = auto()
    INDETERMINATE = auto()
    FAILED = auto()
    CLEAN_PASS = auto()
    RUNNING = auto()


class TestRunInfo(FrozenModel):
    """Metadata for a single test run within an agent's work."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    run_name: str = Field(description="The --mngr-e2e-run-name value used for this run")
    description_markdown: str = Field(description="Brief description of what this run was for")


class TestResult(FrozenModel):
    """Result reported by a test agent, read from its outcome JSON."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(
        default=False, description="Whether an infrastructure error prevented the agent from working"
    )
    tests_passing_before: bool | None = Field(
        default=None, description="Were tests passing before any changes? None if unknown."
    )
    tests_passing_after: bool | None = Field(
        default=None, description="Are tests passing after all changes? None if unknown."
    )
    summary_markdown: str = Field(default="", description="Overall markdown summary of what happened")
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="List of test runs performed, in order")
    escalations: tuple[Escalation, ...] = Field(
        default=(), description="Issues needing attention beyond this test, independent of whether the test passed"
    )


class IntegratorResult(FrozenModel):
    """Result from the integrator agent that cherry-picks fix branches."""

    agent_name: AgentName | None = Field(default=None, description="Name of the integrator agent")
    squashed_branches: tuple[str, ...] = Field(default=(), description="Branches in the squashed non-impl commit")
    squashed_commit_hash: str | None = Field(default=None, description="Commit hash of the squashed non-impl commit")
    impl_priority: tuple[str, ...] = Field(default=(), description="Impl branches in priority order, highest first")
    impl_commit_hashes: dict[str, str] = Field(
        default_factory=dict, description="Mapping of impl branch name to its commit hash on the integrated branch"
    )
    failed: tuple[str, ...] = Field(default=(), description="Branch names that could not be integrated")
    branch_name: str | None = Field(default=None, description="Integrated branch name, if any merges succeeded")
    escalations: tuple[IntegratorEscalation, ...] = Field(
        default=(),
        description="Every escalation the integrator reports, resolved and unresolved alike. A suite-wide cleanup "
        "it applied is a resolved escalation it raised to itself, so there is no separate normalizations field.",
    )
    pull_request_url: str | None = Field(
        default=None, description="URL of the pull request the integrator opened for this run"
    )
    pull_request_error: str | None = Field(
        default=None, description="Why the integrator could not open a pull request, if it could not"
    )


class TestMapReduceResult(FrozenModel):
    """Result for one test in the map-reduce run."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    test_node_id: str = Field(description="The pytest node ID for the test")
    agent_name: AgentName = Field(description="Name of the agent that ran this test")
    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(default=False, description="Whether an error prevented the agent from working")
    tests_passing_before: bool | None = Field(default=None, description="Were tests passing before changes?")
    tests_passing_after: bool | None = Field(default=None, description="Are tests passing after changes?")
    summary_markdown: str = Field(default="", description="Markdown summary from the agent")
    branch_name: str | None = Field(
        default=None,
        description="Git branch name if code changes were pulled, or None",
    )
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="Test runs performed by the agent, in order")
    escalations: tuple[Escalation, ...] = Field(
        default=(), description="Issues this agent raised for attention beyond its own test"
    )


# Subdirectory of each agent's extracted output archive holding its .test_output contents.
EXTRACTED_TEST_OUTPUT_DIR = "test_output"

# Outcome JSON for a given agent is immutable once present, so parses are cached
# and generate_html_report can be called many times during polling without
# re-parsing.
# Keyed by (output_dir, agent_name), not agent name alone: the same agent name
# can appear under two different output directories (the orchestrator's own
# output dir and the reducer's inputs dir), and those are different outcomes.
_TESTING_OUTCOME_CACHE: dict[tuple[Path, AgentName], TestResult] = {}
_INTEGRATOR_OUTCOME_CACHE: dict[tuple[Path, AgentName], IntegratorResult] = {}

SECTION_ORDER: list[ReportSection] = [
    ReportSection.IMPL_FIXES,
    ReportSection.TEST_AND_DOC_FIXES,
    ReportSection.FIX_FAILED,
    ReportSection.INDETERMINATE,
    ReportSection.FAILED,
    ReportSection.CLEAN_PASS,
    ReportSection.RUNNING,
]

SECTION_LABELS: dict[ReportSection, str] = {
    ReportSection.IMPL_FIXES: "Implementation fixes",
    # "Test and doc" is the reducer's own name for exactly these change kinds:
    # it squashes them into one [TEST/DOC] commit.
    ReportSection.TEST_AND_DOC_FIXES: "Test and doc fixes",
    ReportSection.FIX_FAILED: "Fix failed",
    ReportSection.INDETERMINATE: "Indeterminate",
    ReportSection.FAILED: "Failed",
    ReportSection.CLEAN_PASS: "Clean pass",
    ReportSection.RUNNING: "Running",
}

_ESCALATION_KIND_LABELS: dict[EscalationKind, str] = {
    EscalationKind.UNCAUGHT_BUG: "Uncaught bug",
    EscalationKind.FIX_DIRECTION_AMBIGUOUS: "Fix direction ambiguous",
    EscalationKind.HARNESS_DEFECT: "Harness defect",
    EscalationKind.SUITE_DUPLICATION: "Suite duplication",
}

SECTION_COLORS: dict[ReportSection, str] = {
    ReportSection.IMPL_FIXES: "rgb(76, 175, 80)",
    ReportSection.TEST_AND_DOC_FIXES: "rgb(33, 150, 243)",
    ReportSection.FIX_FAILED: "rgb(244, 67, 54)",
    ReportSection.INDETERMINATE: "rgb(156, 39, 176)",
    ReportSection.FAILED: "rgb(255, 152, 0)",
    ReportSection.CLEAN_PASS: "rgb(158, 158, 158)",
    ReportSection.RUNNING: "rgb(3, 169, 244)",
}

# Colors and anchors for the escalation sections, which sit alongside the
# outcome sections in the sidebar and so need entries of the same shape.
UNRESOLVED_ESCALATIONS_COLOR = "rgb(244, 67, 54)"
RESOLVED_ESCALATIONS_COLOR = "rgb(76, 175, 80)"
UNRESOLVED_ESCALATIONS_ANCHOR = "sec-unresolved-escalations"
RESOLVED_ESCALATIONS_ANCHOR = "sec-resolved-escalations"


def escalation_kind_label(kind: EscalationKind) -> str:
    """Human-readable label for an escalation kind.

    Public so the PR-summary builder labels kinds the same way the HTML report
    does; a third kind must not be able to render differently in the two places.
    """
    return _ESCALATION_KIND_LABELS[kind]


def section_label(section: ReportSection) -> str:
    """Human-readable label for a report section.

    Public so the PR-summary builder labels statuses the same way the HTML
    report does.
    """
    return SECTION_LABELS[section]


_md = MarkdownIt()

# The "js-default" preset disables raw HTML, so agent-authored markdown cannot
# inject markup into a report that renders it through |safe. Escalation
# descriptions are agent prose, so they all go through this rather than _md.
_strict_markdown = MarkdownIt("js-default")

# Keys are each recipe's own change-kind enum (ChangeKind, BehaviorChangeKind).
_ChangeKindT = TypeVar("_ChangeKindT", bound=UpperCaseStrEnum)

_TEST_AND_DOC_CHANGE_KINDS = frozenset({ChangeKind.FIX_TEST, ChangeKind.IMPROVE_TEST, ChangeKind.FIX_TUTORIAL})

_CHANGE_STATUS_ICONS: dict[ChangeStatus, str] = {
    ChangeStatus.SUCCEEDED: "&#10003;",
    ChangeStatus.FAILED: "&#10007;",
}


# The Jinja env autoescapes the .j2 template; sections that already contain
# safe HTML (markdown-rendered cells, test ids with <wbr> hints) are passed
# through with the |safe filter in the template.
_jinja_env = Environment(
    loader=PackageLoader("imbue.mngr_tmr", "report_assets"),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


def read_static(filename: str) -> str:
    """Read a static (non-jinja) asset shipped under report_assets/."""
    return (files("imbue.mngr_tmr.report_assets") / filename).read_text()


def _parse_outcome_json(raw: str) -> TestResult:
    """Parse an outcome JSON string into a TestResult.

    Raises json.JSONDecodeError, KeyError, or ValueError on invalid data.
    """
    data = json.loads(raw)
    raw_changes = data.get("changes", {})
    changes: dict[ChangeKind, Change] = {
        ChangeKind(kind_str): Change(
            status=ChangeStatus(entry["status"]),
            summary_markdown=entry.get("summary_markdown", entry.get("summary", "")),
        )
        for kind_str, entry in raw_changes.items()
    }
    raw_runs = data.get("test_runs", [])
    test_runs = tuple(
        TestRunInfo(
            run_name=run_entry.get("run_name", ""),
            description_markdown=run_entry.get("description_markdown", ""),
        )
        for run_entry in raw_runs
    )
    return TestResult(
        changes=changes,
        errored=data.get("errored", False),
        tests_passing_before=data.get("tests_passing_before"),
        tests_passing_after=data.get("tests_passing_after"),
        summary_markdown=data.get("summary_markdown", ""),
        test_runs=test_runs,
        escalations=_parse_escalations(data.get("escalations", ())),
    )


def escalation_id(agent_name: AgentName | str, index: int) -> str:
    """The id of a mapper's Nth escalation.

    Derived rather than agent-supplied, so it is unique by construction and
    there is no uniqueness for the agents to get wrong. This is what the
    integrator's ``member_ids`` reference and what ``escalation_coverage``
    checks against.
    """
    return f"{agent_name}#{index}"


def _parse_locations(raw: Any) -> tuple[EscalationLocation, ...]:
    """Parse an escalation's ``locations`` list."""
    if not raw:
        return ()
    return tuple(
        EscalationLocation(
            path=str(entry["path"]),
            line=int(entry["line"]) if entry.get("line") is not None else None,
        )
        for entry in raw
    )


def _parse_escalations(raw: Any) -> tuple[Escalation, ...]:
    """Parse a mapper outcome's ``escalations`` list.

    ``kind`` and ``description_markdown`` are both required: a missing key
    raises, which the callers turn into a warning and a dropped outcome rather
    than silently filing the escalation under whichever kind happened to be the
    default. Takes the raw JSON value (like the sibling parsing in this module)
    rather than a narrowed type, since the outcome shape is a contract with the
    agents and malformed data is caught by the callers.
    """
    if not raw:
        return ()
    return tuple(
        Escalation(
            description_markdown=str(entry["description_markdown"]),
            kind=EscalationKind(str(entry["kind"])),
            locations=_parse_locations(entry.get("locations", ())),
        )
        for entry in raw
    )


def _parse_integrator_escalations(raw: Any) -> tuple[IntegratorEscalation, ...]:
    """Parse the integrator outcome's ``escalations`` list of grouped escalations."""
    if not raw:
        return ()
    return tuple(
        IntegratorEscalation(
            kind=EscalationKind(str(entry["kind"])),
            description_markdown=str(entry["description_markdown"]),
            member_ids=tuple(str(member) for member in entry.get("member_ids", ())),
            resolved_in_commit_hash=entry.get("resolved_in_commit_hash"),
        )
        for entry in raw
    )


def _outcome_path_for_testing_agent(output_dir: Path, agent_name: AgentName) -> Path:
    return output_dir / str(agent_name) / EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME


def _outcome_path_for_integrator(output_dir: Path, agent_name: AgentName) -> Path:
    return output_dir / str(agent_name) / EXTRACTED_TEST_OUTPUT_DIR / INTEGRATOR_OUTCOME_FILENAME


def synthesize_missing_mapper_outcomes(output_dir: Path, agents: Sequence[AgentMetadata]) -> list[AgentName]:
    """Write a synthetic errored outcome for every failed mapper that produced none.

    A mapper that failed to launch, timed out, or crashed never writes an outcome
    file. Anything that reads the output dir as *files* rather than as
    orchestrator metadata -- the reducer, whose PR summary and should-pull
    predicate both count outcomes on disk -- is therefore blind to it, so a run
    with failures reports as if those tests did not exist. Give each failed
    mapper a minimal ``errored`` outcome so it is seen and counted as failed.

    Only fills gaps: a mapper that already wrote an outcome is left untouched.
    Returns the agent names a synthetic outcome was written for.
    """
    written: list[AgentName] = []
    for meta in agents:
        if meta.kind is not AgentKind.MAPPER or meta.error_summary is None:
            continue
        path = _outcome_path_for_testing_agent(output_dir, meta.agent_name)
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "changes": {},
                    "errored": True,
                    "tests_passing_before": None,
                    "tests_passing_after": None,
                    "summary_markdown": meta.error_summary,
                    "test_runs": [],
                    "escalations": [],
                }
            )
        )
        written.append(meta.agent_name)
    return written


def load_testing_agent_outcome(agent_name: AgentName, output_dir: Path) -> TestResult | None:
    """Read and cache a testing agent's outcome from the extracted output dir.

    Public because the PR-summary builder reads the same per-agent layout from
    the reducer's inputs directory (see ``pr_summary``).
    """
    cache_key = (output_dir, agent_name)
    cached = _TESTING_OUTCOME_CACHE.get(cache_key)
    if cached is not None:
        return cached
    path = _outcome_path_for_testing_agent(output_dir, agent_name)
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        outcome = _parse_outcome_json(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse outcome for agent '{}': {}", agent_name, exc)
        return None
    _TESTING_OUTCOME_CACHE[cache_key] = outcome
    return outcome


def parse_integrator_outcome(data: Any, agent_name: AgentName | None, branch_name: str | None) -> IntegratorResult:
    """Build an ``IntegratorResult`` from already-decoded outcome JSON.

    Public so callers that hold the outcome file directly (the recipe, the PR
    summary builder) go through the same model rather than re-deriving fields
    from the raw dict.
    """
    return IntegratorResult(
        agent_name=agent_name,
        squashed_branches=tuple(data.get("squashed_branches", ())),
        squashed_commit_hash=data.get("squashed_commit_hash"),
        impl_priority=tuple(data.get("impl_priority", ())),
        impl_commit_hashes=data.get("impl_commit_hashes", {}),
        failed=tuple(data.get("failed", ())),
        branch_name=branch_name,
        escalations=_parse_integrator_escalations(data.get("escalations", ())),
        pull_request_url=data.get("pull_request_url"),
        pull_request_error=data.get("pull_request_error"),
    )


def load_integrator_outcome_file(
    outcome_path: Path, agent_name: AgentName | None = None, branch_name: str | None = None
) -> IntegratorResult | None:
    """Read and parse an integrator outcome file, or None if it is unreadable.

    The parse happens inside the try and ``KeyError``/``ValueError`` are caught
    alongside the read errors, matching ``load_testing_agent_outcome``. Both
    parsers now require ``kind`` and ``description_markdown`` rather than
    defaulting them, so malformed agent output raises where it used to yield a
    silently wrong result -- and every caller here (the report on each poll tick,
    the PR summary, the coverage check, the recipe's PR-url read) wants a
    warning and a None rather than a crash. ``TypeError`` is in the tuple because
    ``locations`` is nested agent-written JSON: a list of bare strings indexes as
    a string, not a mapping.
    """
    try:
        data = json.loads(outcome_path.read_text())
        return parse_integrator_outcome(data, agent_name, branch_name)
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Failed to read integrator outcome at {}: {}", outcome_path, exc)
        return None


def load_integrator_outcome(meta: AgentMetadata, output_dir: Path) -> IntegratorResult | None:
    """Read and cache the integrator's outcome, or None if it has not published one.

    None rather than an empty result: an integrator that exists but has not
    reported is a different thing from one that reported nothing, and the report
    keys real decisions on the difference. Returning an empty result made a run
    whose reducer crashed or is still working render an integration report and
    accuse the grouping of omitting every escalation, when in truth there was no
    grouping yet.

    Every consumer of the return value already accepts None -- it means "no
    integrator" throughout the rendering path -- so the empty stand-in bought
    nothing.
    """
    cache_key = (output_dir, meta.agent_name)
    cached = _INTEGRATOR_OUTCOME_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = load_integrator_outcome_file(
        _outcome_path_for_integrator(output_dir, meta.agent_name), meta.agent_name, meta.branch_name
    )
    if result is None:
        return None
    _INTEGRATOR_OUTCOME_CACHE[cache_key] = result
    return result


def _row_from_metadata(meta: AgentMetadata, outcome: TestResult | None) -> TestMapReduceResult:
    """Build a renderable row from per-agent metadata + optional parsed outcome."""
    if meta.error_summary is not None:
        return TestMapReduceResult(
            test_node_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            errored=True,
            summary_markdown=meta.error_summary,
            branch_name=meta.branch_name,
        )
    if outcome is None:
        return TestMapReduceResult(
            test_node_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            summary_markdown="Agent is still running...",
            branch_name=meta.branch_name,
        )
    return TestMapReduceResult(
        test_node_id=meta.task_id or str(meta.agent_name),
        agent_name=meta.agent_name,
        changes=outcome.changes,
        errored=outcome.errored,
        tests_passing_before=outcome.tests_passing_before,
        tests_passing_after=outcome.tests_passing_after,
        summary_markdown=outcome.summary_markdown,
        branch_name=meta.branch_name,
        test_runs=outcome.test_runs,
        escalations=outcome.escalations,
    )


def _build_rows(agents: Sequence[AgentMetadata], output_dir: Path) -> list[TestMapReduceResult]:
    """Build renderable rows for all testing agents (one per AgentMetadata).

    Skips the integrator -- it has its own panel in the report, not a row.
    """
    rows: list[TestMapReduceResult] = []
    for meta in agents:
        if meta.kind is not AgentKind.MAPPER:
            continue
        outcome = load_testing_agent_outcome(meta.agent_name, output_dir) if meta.error_summary is None else None
        rows.append(_row_from_metadata(meta, outcome))
    return rows


def report_section_of(result: TestMapReduceResult) -> ReportSection:
    """Derive a report section from a result for report grouping/coloring.

    ``errored=True`` indicates an infrastructure failure (launch failed, agent
    timed out, details missing) and is rendered in the FAILED section.
    FIX_FAILED covers results where every change the agent attempted failed;
    anything fitting no other section is INDETERMINATE.

    IMPL_FIXES is checked before TEST_AND_DOC_FIXES: an agent that fixed the
    implementation *and* touched its test has made an implementation fix, and
    that is the fact a reviewer needs. Testing the other order first hid such
    results under a test-only label and left IMPL_FIXES empty.

    Escalations do not influence the section: a result belongs to the section
    its own outcome earns, and its escalations are reported separately.
    """
    if result.errored:
        return ReportSection.FAILED
    if result.tests_passing_before is None and result.tests_passing_after is None and not result.changes:
        return ReportSection.RUNNING
    if result.changes and all(c.status is ChangeStatus.FAILED for c in result.changes.values()):
        return ReportSection.FIX_FAILED
    if ChangeKind.FIX_IMPL in result.changes:
        return ReportSection.IMPL_FIXES
    if any(kind in _TEST_AND_DOC_CHANGE_KINDS for kind in result.changes):
        return ReportSection.TEST_AND_DOC_FIXES
    if not result.changes and result.tests_passing_after is True:
        return ReportSection.CLEAN_PASS
    return ReportSection.INDETERMINATE


def _format_test_id(test_node_id: str) -> str:
    """HTML-escape the node ID, then add a soft wrap hint after each ``::``."""
    return html.escape(test_node_id).replace("::", "::<wbr>")


def format_changes(changes: Mapping[_ChangeKindT, Change]) -> str:
    """Format changes as concise kind + icon pairs (any recipe's change-kind enum)."""
    parts: list[str] = []
    for kind, change in changes.items():
        icon = _CHANGE_STATUS_ICONS.get(change.status, "?")
        parts.append(f"{kind.value} {icon}")
    return ", ".join(parts)


def merged_status_html(branch_name: str | None, integrator: IntegratorResult | None) -> str:
    """Return merged-status HTML: commit hash for impl, checkmark for squashed, X for failed."""
    if integrator is None or branch_name is None:
        return ""
    if branch_name in integrator.impl_commit_hashes:
        commit_hash = html.escape(integrator.impl_commit_hashes[branch_name][:10])
        return f"<code>{commit_hash}</code>"
    if branch_name in set(integrator.squashed_branches):
        return "&#10003;"
    if branch_name in set(integrator.impl_priority) and branch_name not in integrator.impl_commit_hashes:
        return "&#10003;"
    if branch_name in set(integrator.failed):
        return "&#10007;"
    return ""


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return _md.render(text)


def render_markdown_without_raw_html(text: str) -> str:
    """Render markdown to HTML with raw HTML disabled.

    Public because the behaviors report renders the same agent-authored fields
    and must apply the same policy to them.
    """
    return _strict_markdown.render(text)


def _find_test_artifact_runs(
    artifacts_root: Path,
    agent_name: AgentName,
    test_runs: tuple[TestRunInfo, ...],
) -> list[tuple[str, str, Path]]:
    """Find test artifact directories for all runs of an agent.

    Returns a list of (run_name, description, test_dir) tuples, one per run.
    Uses test_runs metadata when available to match run names to descriptions;
    otherwise discovers all run directories on disk.
    """
    agent_dir = artifacts_root / str(agent_name)
    if not agent_dir.is_dir():
        return []

    run_descriptions: dict[str, str] = {tr.run_name: tr.description_markdown for tr in test_runs}

    # Extracted layout from outputs.tar.gz: <agent_dir>/test_output/e2e/<run>/...
    test_output_dir = agent_dir / "test_output"
    found: list[tuple[str, str, Path]] = []
    for candidate_root in [test_output_dir / "e2e", test_output_dir, agent_dir / "e2e", agent_dir]:
        if not candidate_root.is_dir():
            continue
        for run_dir in sorted(candidate_root.iterdir()):
            if not run_dir.is_dir():
                continue
            for test_dir in sorted(run_dir.iterdir()):
                if test_dir.is_dir() and (test_dir / "transcript.txt").exists():
                    run_name = run_dir.name
                    description = run_descriptions.get(run_name, "")
                    found.append((run_name, description, test_dir))
    return found


def _build_row_view(
    row: TestMapReduceResult,
    integrator: IntegratorResult | None,
    has_artifacts_for_agent: bool,
) -> dict[str, object]:
    """Flatten a renderable row into the dict the jinja template consumes.

    A row carries its own escalations, so they read next to the test that raised
    them rather than in a separate list a reader has to cross-reference by test
    id. This is also what keeps them visible mid-run: rows exist from the moment
    a mapper publishes, long before any grouping does.
    """
    return {
        "test_id_html": _format_test_id(row.test_node_id),
        "agent_name": str(row.agent_name),
        "branch_name": row.branch_name,
        "changes_html": format_changes(row.changes) if row.changes else "-",
        "merged_html": merged_status_html(row.branch_name, integrator),
        "summary_html": _render_markdown(row.summary_markdown) if row.summary_markdown else "",
        "has_artifacts": has_artifacts_for_agent,
        "escalations": _build_row_escalation_views(row),
    }


def _build_row_escalation_views(row: TestMapReduceResult) -> list[dict[str, object]]:
    """One view per escalation this mapper raised, in the order it reported them."""
    return [
        {
            "id": escalation_id(row.agent_name, index),
            "description_html": render_markdown_without_raw_html(escalation.description_markdown),
            "kind_label": escalation_kind_label(escalation.kind),
            "locations": [_format_location(location) for location in escalation.locations],
        }
        for index, escalation in enumerate(row.escalations)
    ]


def _build_section_views(
    rows: list[TestMapReduceResult],
    integrator: IntegratorResult | None,
    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]],
    has_artifacts: bool,
) -> list[dict[str, object]]:
    """Group rows by section and prepare the section views the template consumes."""
    grouped: dict[ReportSection, list[TestMapReduceResult]] = {}
    for r in rows:
        grouped.setdefault(report_section_of(r), []).append(r)

    sections: list[dict[str, object]] = []
    for sec in SECTION_ORDER:
        group = grouped.get(sec)
        if not group:
            continue
        if sec == ReportSection.IMPL_FIXES and integrator is not None and integrator.impl_priority:
            priority_order = {branch: i for i, branch in enumerate(integrator.impl_priority)}
            group = sorted(group, key=lambda r: priority_order.get(r.branch_name or "", len(priority_order)))
        col_count_base = (
            5
            if sec not in (ReportSection.RUNNING, ReportSection.CLEAN_PASS)
            else (2 if sec == ReportSection.RUNNING else 3)
        )
        col_count = col_count_base + (1 if has_artifacts and sec != ReportSection.RUNNING else 0)
        section_rows = [_build_row_view(r, integrator, str(r.agent_name) in agent_artifact_runs) for r in group]
        sections.append(
            {
                "kind": sec.value,
                "label": SECTION_LABELS[sec],
                "color": SECTION_COLORS[sec],
                "anchor": f"sec-{sec.value}",
                "rows": section_rows,
                "count": len(section_rows),
                "col_count": col_count,
            }
        )
    return sections


def _build_toc_groups(
    sections: list[dict[str, object]],
    *,
    unresolved_count: int,
    resolved_count: int,
) -> list[dict[str, object]]:
    """Sidebar entries under two headings: what the integrator did, and what the tests did.

    The escalation blocks used to carry no ``id`` at all while the sidebar was
    built from ``sections`` alone, so they were reachable only by scrolling past
    them. They are ordinary sections here, under an "Integration" heading that
    separates the run-wide findings from the per-test results.
    """
    integration_links: list[dict[str, object]] = []
    if unresolved_count:
        integration_links.append(
            {
                "anchor": UNRESOLVED_ESCALATIONS_ANCHOR,
                "color": UNRESOLVED_ESCALATIONS_COLOR,
                "label": "Unresolved escalations",
                "count": unresolved_count,
            }
        )
    if resolved_count:
        integration_links.append(
            {
                "anchor": RESOLVED_ESCALATIONS_ANCHOR,
                "color": RESOLVED_ESCALATIONS_COLOR,
                "label": "Resolved escalations",
                "count": resolved_count,
            }
        )
    test_links = [
        {"anchor": s["anchor"], "color": s["color"], "label": s["label"], "count": s["count"]} for s in sections
    ]
    return [
        {"label": label, "links": links}
        for label, links in [("Integration", integration_links), ("Tests", test_links)]
        if links
    ]


def _format_location(location: EscalationLocation) -> str:
    """Render a location as ``path:line``, or just ``path`` when it names no line."""
    return location.path if location.line is None else f"{location.path}:{location.line}"


def split_summary(markdown_text: str) -> tuple[str, str]:
    """Split a description into its summary line and everything after it.

    Escalations carry a single ``description_markdown`` rather than a separate
    title, so every view labels an entry from its first line and the agents are
    told to make that line a one-sentence summary.

    One function returns both halves so a caller rendering them separately -- the
    PR body puts the summary in a heading and the rest beneath it -- cannot have
    the two disagree about where the split falls.
    """
    lines = markdown_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            return line.strip(), "\n".join(lines[index + 1 :]).strip()
    return "", ""


def first_line(markdown_text: str) -> str:
    """The summary line of a description. See :func:`split_summary`."""
    return split_summary(markdown_text)[0]


def _mapper_escalation_ids(rows: Sequence[TestMapReduceResult]) -> list[str]:
    """Every mapper escalation id in the run, in row order."""
    return [escalation_id(row.agent_name, index) for row in rows for index, _ in enumerate(row.escalations)]


def ungrouped_escalation_ids(
    rows: Sequence[TestMapReduceResult],
    integrator_escalations: Sequence[IntegratorEscalation],
) -> list[str]:
    """Mapper escalation ids that no integrator escalation claims as a member.

    The report shows the raw mapper escalations regardless, so an incomplete
    grouping never loses a report -- but it does mean the grouped view is not
    the whole story, which is worth saying out loud rather than leaving a reader
    to infer from counts.

    Takes the escalations rather than an optional result, because callers
    disagree about what "no integrator outcome" means: mid-run there is simply
    no grouping yet and nothing to report, while the reducer checking its own
    outcome wants every id back. Each decides at its own call site.
    """
    grouped = {member for escalation in integrator_escalations for member in escalation.member_ids}
    return [known_id for known_id in _mapper_escalation_ids(rows) if known_id not in grouped]


def sort_escalations(
    escalations: Sequence[IntegratorEscalation],
) -> list[IntegratorEscalation]:
    """Integrator-originated first, then by descending member count.

    Public so the PR body orders escalations exactly as the HTML report does.

    The integrator's own findings have no members, so a plain member-count sort
    would bury them last -- and they are precisely the ones no mapper could have
    produced, since they come from reading the whole integrated diff. Ties keep
    input order: the report is regenerated on every poll, so an unstable sort
    would make entries jump between renders.
    """
    return sorted(escalations, key=lambda e: (1 if e.member_ids else 0, -len(e.member_ids)))


def build_integrator_escalation_views(
    escalations: Sequence[IntegratorEscalation],
) -> list[dict[str, object]]:
    """Flatten grouped escalations into the dicts a report template consumes.

    Public and shared: the behaviors report renders the same model, and a
    divergent copy there had drifted to a different markdown policy for the same
    agent-authored field.
    """
    return [
        {
            "summary": first_line(escalation.description_markdown),
            "description_html": render_markdown_without_raw_html(escalation.description_markdown),
            "kind_label": escalation_kind_label(escalation.kind),
            "scale_label": escalation_scale_label(escalation),
            "resolved_in_commit_hash": escalation.resolved_in_commit_hash,
        }
        for escalation in sort_escalations(escalations)
    ]


def escalation_scale_label(escalation: IntegratorEscalation) -> str:
    """How many mapper reports a group covers, or that the integrator found it alone.

    Shared so the HTML report, the behaviors report, and the PR body cannot word
    this differently -- the same reason ``escalation_kind_label`` exists.
    """
    if not escalation.member_ids:
        return "found by the integrator"
    return f"{len(escalation.member_ids)} mapper report(s)"


def split_escalation_views(
    escalations: Sequence[IntegratorEscalation],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Build the (unresolved, resolved) view lists both report modules render."""
    return (
        build_integrator_escalation_views([e for e in escalations if not e.is_resolved]),
        build_integrator_escalation_views([e for e in escalations if e.is_resolved]),
    )


def _build_artifact_panels(
    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]],
) -> list[dict[str, object]]:
    """Build the panel views, one per agent that has artifact runs."""
    panels: list[dict[str, object]] = []
    for agent_name, runs in agent_artifact_runs.items():
        escaped_name = html.escape(agent_name)
        run_views: list[dict[str, object]] = []
        for i, (_run_name, description, test_dir) in enumerate(runs):
            prefix = f"art-{escaped_name}-r{i}-"
            run_views.append(
                {
                    "index": i,
                    "description_html": _render_markdown(description) if description else "",
                    "detail_html": render_test_detail(test_dir, detail_id_prefix=prefix),
                }
            )
        panels.append(
            {
                "agent_name": agent_name,
                "tab_count": len(runs),
                "runs": run_views,
            }
        )
    return panels


def generate_html_report(
    agents: Sequence[AgentMetadata],
    output_dir: Path,
    *,
    integrator_metadata: AgentMetadata | None = None,
    run_commands: list[tuple[str, str]] | None = None,
) -> Path:
    """Generate an HTML report summarizing the run.

    Walks ``agents`` and reads each testing agent's outcome from
    ``output_dir/<agent_name>/test_output/``; reads the integrator's
    outcome (if any) from ``output_dir/<integrator_name>/``. Writes the
    report to ``output_dir/index.html`` and returns that path.

    Side-effect free except for writing the local file. Mirroring the
    report to s3 is the recipe's responsibility (see ``recipe.render_report``).
    """
    rows = _build_rows(agents, output_dir)
    integrator = load_integrator_outcome(integrator_metadata, output_dir) if integrator_metadata is not None else None

    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]] = {}
    for r in rows:
        try:
            runs = _find_test_artifact_runs(output_dir, r.agent_name, r.test_runs)
        except OSError as exc:
            if "Too many open files" in str(exc):
                logger.warning("FD exhaustion while scanning artifacts for '{}': {}", r.agent_name, exc)
            raise
        if runs:
            agent_artifact_runs[str(r.agent_name)] = runs

    has_artifacts = bool(agent_artifact_runs)
    sections = _build_section_views(rows, integrator, agent_artifact_runs, has_artifacts)
    artifact_panels = _build_artifact_panels(agent_artifact_runs)

    # Summaries and descriptions are markdown rendered to HTML here and passed
    # through with |safe, like the per-row summary cells; everything else the
    # template writes is autoescaped.
    integrator_escalations = integrator.escalations if integrator is not None else ()
    unresolved_escalation_views, resolved_escalation_views = split_escalation_views(integrator_escalations)
    # Only an integrator that has actually published an outcome can have left an
    # escalation out of its grouping; mid-run there is simply no grouping yet.
    ungrouped_ids = ungrouped_escalation_ids(rows, integrator_escalations) if integrator is not None else []
    mapper_escalation_count = sum(len(row.escalations) for row in rows)
    if ungrouped_ids:
        logger.warning(
            "{} of {} mapper escalation(s) are in no integrator group",
            len(ungrouped_ids),
            mapper_escalation_count,
        )

    toc_groups = _build_toc_groups(
        sections,
        unresolved_count=len(unresolved_escalation_views),
        resolved_count=len(resolved_escalation_views),
    )

    reintegrate_cmd = ""
    if run_commands:
        for cmd_label, cmd_text in run_commands:
            if "reintegrate" in cmd_label.lower():
                reintegrate_cmd = html.escape(cmd_text)
                break

    template = _jinja_env.get_template("report.html.j2")
    report_html = template.render(
        rows=rows,
        sections=sections,
        toc_groups=toc_groups,
        has_artifacts=has_artifacts,
        artifact_panels=artifact_panels,
        integrator=integrator,
        mapper_escalation_count=mapper_escalation_count,
        unresolved_escalation_views=unresolved_escalation_views,
        resolved_escalation_views=resolved_escalation_views,
        ungrouped_escalation_ids=ungrouped_ids,
        has_integrator=integrator is not None,
        unresolved_anchor=UNRESOLVED_ESCALATIONS_ANCHOR,
        resolved_anchor=RESOLVED_ESCALATIONS_ANCHOR,
        unresolved_color=UNRESOLVED_ESCALATIONS_COLOR,
        resolved_color=RESOLVED_ESCALATIONS_COLOR,
        run_commands=run_commands or [],
        reintegrate_cmd=reintegrate_cmd,
        asciinema_css_url=ASCIINEMA_PLAYER_CSS,
        asciinema_js_url=ASCIINEMA_PLAYER_JS,
        css=read_static("report.css"),
        detail_css=DETAIL_CSS,
        js=read_static("artifacts.js"),
    )
    output_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to {}", output_path)
    return output_path
