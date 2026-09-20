"""Assert a finished self-diagnostic job's facts against its family's expected table.

Every step of every trial yields one fact block, computed at check time by `evidence_facts.py` from
the records the job directory holds. A table entry names a fact -- `<fact>` for the last step that
ran, or `<fact>@<step>` -- and a matcher, and optionally per-harness overrides, a known failure,
whether the fact reads the agent's compliance or the health of a source that reads it, and the facts
it requires.

Each trial gets one verdict, in this precedence: not measured (no workspace was ever created, so
nothing is asserted), failed, not followed, known, passed. Only failed is the instrument's fault.
`check-run`'s four criteria are facts here (`trial.completed`, `gates.held`,
`evidence.errored_entries` and `arm.harness_config.is_model_confirmed`), so a table can declare any
of them known or override it per harness; `check-run`'s own verdict is never consulted and
`check-run` itself stays absolute and unchanged.

`--record-only` computes the same blocks with no table and writes them out. That is the reading a
live job gets, and the mode a past job directory is read with.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set
from pathlib import Path
from typing import Final
from typing import assert_never

from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_facts
from imbue.minds_evals import fact_sources
from imbue.minds_evals.check_run import list_trial_dirs
from imbue.minds_evals.data_types import DiagnosticRunCheck
from imbue.minds_evals.data_types import DiagnosticTrialCheck
from imbue.minds_evals.data_types import DiagnosticVerdict
from imbue.minds_evals.data_types import ExpectedFactsTable
from imbue.minds_evals.data_types import FactExpectation
from imbue.minds_evals.data_types import FactMatcher
from imbue.minds_evals.data_types import FactMatcherKind
from imbue.minds_evals.data_types import FactOutcome
from imbue.minds_evals.data_types import FactStatus
from imbue.minds_evals.data_types import FactValue
from imbue.minds_evals.data_types import KnownFailure
from imbue.minds_evals.data_types import NOT_RECORDED
from imbue.minds_evals.data_types import NotRecorded
from imbue.minds_evals.data_types import fact_requirements
from imbue.minds_evals.data_types import split_fact_reference
from imbue.minds_evals.errors import ExpectedFactsTableError
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.fact_sources import TrialFactSources
from imbue.minds_evals.reporting import as_table_cell
from imbue.minds_evals.reporting import write_reports

# What the computed blocks are named, beside whichever summary was asked for and after it. The name is
# derived rather than fixed because the scheduled run merges every job's summary directory into one
# flat directory, where one fixed name would be several files overwriting each other.
RECORDED_FACTS_SUFFIX: Final[str] = "-facts.json"

# A seed build that went through records this status; any other non-empty one is a seed that could
# not apply, which is the suite's own failure and so never reads as not measured.
_CLEAN_SEED_BUILD_STATUS: Final[str] = "clean"
_SEED_BUILD_STATUS_FACT: Final[str] = "seed.build_status"

_FAILING_STATUSES: Final[frozenset[FactStatus]] = frozenset(
    {FactStatus.FAILED, FactStatus.NOT_RECORDED, FactStatus.UNEXPECTEDLY_PASSING}
)
_NOT_FOLLOWED_STATUSES: Final[frozenset[FactStatus]] = frozenset(
    {FactStatus.NOT_FOLLOWED, FactStatus.PRECONDITION_NOT_MET}
)

# How much of a recorded or expected value one markdown cell carries. The JSON summary has it whole.
_MAX_CELL_VALUE_CHARS: Final[int] = 120


class StepFacts(FrozenModel):
    """One step's computed block, as the checker holds it."""

    step_name: str = Field(description="The step's name; empty for a flat trial's one step")
    values: dict[str, FactValue] = Field(
        description="Every fact the step recorded; a fact whose record was never due is absent"
    )

    @property
    def recorded_values(self) -> dict[str, JsonValue]:
        """The block's readings, with the not-recorded facts left out so it is plain JSON."""
        return {name: value for name, value in self.values.items() if not isinstance(value, NotRecorded)}

    @property
    def not_recorded_names(self) -> list[str]:
        return sorted(name for name, value in self.values.items() if isinstance(value, NotRecorded))


class TrialFacts(FrozenModel):
    """One trial's computed blocks, step by step, with what decides whether they are asserted at all."""

    trial_name: str = Field(description="The trial directory's name")
    case_id: str = Field(description="The case the trial ran; empty when no state names one")
    harness: str = Field(description="The harness the trial's arm records; empty when it records none")
    not_measured_reason: str = Field(description="Why no workspace was ever created; empty when one was")
    steps: tuple[StepFacts, ...] = Field(description="One block per step harbor ran, in order")

    @property
    def last_step_name(self) -> str:
        return self.steps[-1].step_name if self.steps else ""

    def block(self, step_name: str) -> Mapping[str, FactValue]:
        """The named step's block, or an empty one for a step that never ran."""
        return next((step.values for step in self.steps if step.step_name == step_name), {})


def load_expected_facts_table(table_path: Path) -> ExpectedFactsTable:
    """Raises ExpectedFactsTableError if the file cannot be read or is not a valid table."""
    try:
        raw_text = table_path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExpectedFactsTableError("cannot read {}: {}".format(table_path, exc)) from exc
    try:
        raw_table = json.loads(raw_text)
    except ValueError as exc:
        raise ExpectedFactsTableError("{} is not valid JSON: {}".format(table_path, exc)) from exc
    try:
        return ExpectedFactsTable.model_validate(raw_table)
    except ValidationError as exc:
        raise ExpectedFactsTableError("{} is not a valid expected-facts table: {}".format(table_path, exc)) from exc


# --- computing one trial's blocks ---


@pure
def _recorded_seed_build_status(block: Mapping[str, FactValue]) -> str:
    status = block.get(_SEED_BUILD_STATUS_FACT)
    return status if isinstance(status, str) else ""


@pure
def describe_not_measured(sources: TrialFactSources, blocks: Sequence[Mapping[str, FactValue]]) -> str:
    """Why the trial never produced a workspace to measure, or empty when it did.

    A recorded seed-build failure is measured: a seed that cannot apply is the suite's own failure,
    asserted through its fact rather than excused as infrastructure.
    """
    written_states = [step.state for step in sources.steps if step.state is not None]
    is_seed_build_failed = any(
        _recorded_seed_build_status(block) not in ("", _CLEAN_SEED_BUILD_STATUS) for block in blocks
    )
    if is_seed_build_failed or any(
        str(state.get("preparation_stage") or "") in evidence_facts.WORKSPACE_CREATED_STAGES
        for state in written_states
    ):
        return ""
    elif not written_states:
        # Harbor's exception, when it recorded one, is what stopped the trial before the driver
        # wrote any state.
        return sources.incompletion_reason
    else:
        last_state = written_states[-1]
        timed_out_reason = str(last_state.get("timed_out_reason") or "")
        return "no workspace was created (preparation stage {!r}){}".format(
            str(last_state.get("preparation_stage") or ""), ": {}".format(timed_out_reason) if timed_out_reason else ""
        )


def compute_trial_facts(trial_dir: Path, work_dir: Path) -> TrialFacts:
    """Every step's fact block for one trial.

    `work_dir` is scratch space for the readings that need a program run against a captured archive,
    so it must lie outside the job directory.

    Raises JobReadError for an artifact harbor wrote that is there but cannot be read, as
    `check-run` does.
    """
    sources = fact_sources.load_trial_fact_sources(trial_dir)
    blocks = [
        evidence_facts.compute_step_facts(
            step, fact_sources.read_check_time_readings(step, work_dir / trial_dir.name / str(step.step_index))
        )
        for step in sources.steps
    ]
    return TrialFacts(
        trial_name=sources.trial_name,
        case_id=sources.case_id,
        harness=sources.harness,
        not_measured_reason=describe_not_measured(sources, blocks),
        steps=tuple(
            StepFacts(step_name=step.step_name, values=block)
            for step, block in zip(sources.steps, blocks, strict=True)
        ),
    )


def compute_job_facts(job_dir: Path, work_dir: Path) -> tuple[TrialFacts, ...]:
    """Every trial of a finished job, in name order.

    Raises JobReadError if the directory is missing or holds no trials: a job that never ran is not
    a job whose diagnostics passed.
    """
    if not job_dir.is_dir():
        raise JobReadError("{} is not a job directory".format(job_dir))
    trial_dirs = list_trial_dirs(job_dir)
    if not trial_dirs:
        raise JobReadError("{} holds no trial directories".format(job_dir))
    return tuple(compute_trial_facts(trial_dir, work_dir) for trial_dir in trial_dirs)


# --- matching ---


@pure
def _as_number(value: JsonValue) -> int | float | None:
    """The value as a number; None for anything else, a boolean included."""
    if isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        return value
    else:
        return None


@pure
def is_json_equal(left: JsonValue, right: JsonValue) -> bool:
    """JSON equality: Python's own, except that a boolean never equals a number."""
    left_number = _as_number(left)
    right_number = _as_number(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    elif left_number is not None and right_number is not None:
        return left_number == right_number
    elif isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            is_json_equal(left_item, right_item) for left_item, right_item in zip(left, right, strict=True)
        )
    elif isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(is_json_equal(left[key], right[key]) for key in left)
    else:
        return type(left) is type(right) and left == right


@pure
def _is_contained(recorded: JsonValue, wanted: JsonValue) -> bool:
    if isinstance(recorded, list):
        return any(is_json_equal(member, wanted) for member in recorded)
    elif isinstance(recorded, str):
        return isinstance(wanted, str) and wanted in recorded
    else:
        return False


@pure
def is_fact_matched(matcher: FactMatcher, recorded: FactValue) -> bool:
    """Whether a recorded value satisfies a matcher.

    A recorded null matches only `expected: null`, and a fact that was not recorded matches nothing:
    a record the instrument did not write is never the value a table was looking for.
    """
    if isinstance(recorded, NotRecorded):
        return False
    recorded_number = _as_number(recorded)
    bound = _as_number(matcher.value)
    match matcher.kind:
        case FactMatcherKind.EXPECTED:
            return is_json_equal(recorded, matcher.value)
        case FactMatcherKind.AT_LEAST:
            return recorded_number is not None and bound is not None and recorded_number >= bound
        case FactMatcherKind.AT_MOST:
            return recorded_number is not None and bound is not None and recorded_number <= bound
        case FactMatcherKind.CONTAINS:
            return _is_contained(recorded, matcher.value)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def resolve_fact_expectation(expectation: FactExpectation, harness: str) -> tuple[FactMatcher, KnownFailure | None]:
    """The matcher and known failure that apply to one trial. The harness's override wins over the
    fact's own, for only what it states."""
    override = expectation.by_harness.get(harness)
    matcher = next(
        (
            declared
            for declared in (
                override.declared_matcher() if override is not None else None,
                expectation.declared_matcher(),
            )
            if declared is not None
        ),
        None,
    )
    if matcher is None:
        raise ExpectedFactsTableError("a validated fact expectation states no matcher")
    known_failure = next(
        (
            declared
            for declared in (
                override.known_failure if override is not None else None,
                expectation.known_failure,
            )
            if declared is not None
        ),
        None,
    )
    return matcher, known_failure


# --- verdicts ---


@pure
def _fact_status(
    expectation: FactExpectation,
    known_failure: KnownFailure | None,
    recorded: FactValue,
    is_matched: bool,
    unmet_requirements: Sequence[str],
) -> FactStatus:
    """How one fact came out.

    A requirement decides first: a fact whose source the agent never produced is not asserted at all.
    A compliance or health fact that is null or not recorded fails rather than reading as the agent's
    doing -- the instrument could not tell, which is never what the prompt did. A known failure's
    matcher states the value its defect records, so matching it is `known` and anything else means
    the defect changed or was fixed and the mark must come off.
    """
    if unmet_requirements:
        return FactStatus.PRECONDITION_NOT_MET
    elif isinstance(recorded, NotRecorded):
        return FactStatus.NOT_RECORDED
    elif expectation.is_agent_reading:
        if recorded is None:
            return FactStatus.FAILED
        elif is_matched:
            return FactStatus.PASSED
        else:
            return FactStatus.NOT_FOLLOWED if expectation.is_compliance else FactStatus.FAILED
    elif known_failure is not None:
        return FactStatus.KNOWN if is_matched else FactStatus.UNEXPECTEDLY_PASSING
    else:
        return FactStatus.PASSED if is_matched else FactStatus.FAILED


@pure
def requirement_order(table: ExpectedFactsTable) -> list[str]:
    """Every fact reference of the table, with each one's requirements ahead of it.

    The table's validation walks these same edges and refuses a cycle, so this always terminates.
    """
    placed: list[str] = []
    remaining = list(table.facts)
    while remaining:
        ready = [
            reference
            for reference in remaining
            if set(fact_requirements(table.facts, table.requires, reference)) <= set(placed)
        ]
        assert ready, "a validated table's requirements are acyclic, so some fact is always ready"
        placed.extend(ready)
        remaining = [reference for reference in remaining if reference not in ready]
    return placed


@pure
def evaluate_trial_facts(table: ExpectedFactsTable, facts: TrialFacts) -> tuple[FactOutcome, ...]:
    """Every table fact's outcome on one trial, in table order.

    A fact is read from the step its reference names, or from the last step that ran; a step that
    never ran has no block, so every fact pinned to it is absent from one, which is not recorded.
    """
    resolved_by_reference = {
        reference: resolve_fact_expectation(expectation, facts.harness)
        for reference, expectation in table.facts.items()
    }
    read_by_reference: dict[str, tuple[str, str, FactValue]] = {}
    for reference in table.facts:
        fact_name, step_name = split_fact_reference(reference)
        resolved_step_name = step_name or facts.last_step_name
        read_by_reference[reference] = (
            fact_name,
            resolved_step_name,
            facts.block(resolved_step_name).get(fact_name, NOT_RECORDED),
        )
    outcome_by_reference: dict[str, FactOutcome] = {}
    # A requirement is met only when its fact passed, so each fact's own outcome is settled first.
    for reference in requirement_order(table):
        expectation = table.facts[reference]
        fact_name, step_name, recorded = read_by_reference[reference]
        matcher, known_failure = resolved_by_reference[reference]
        unmet_requirements = tuple(
            required
            for required in fact_requirements(table.facts, table.requires, reference)
            if outcome_by_reference[required].status is not FactStatus.PASSED
        )
        outcome_by_reference[reference] = FactOutcome(
            fact_name=fact_name,
            step_name=step_name,
            status=_fact_status(
                expectation, known_failure, recorded, is_fact_matched(matcher, recorded), unmet_requirements
            ),
            is_compliance=expectation.is_compliance,
            is_health=expectation.is_health,
            recorded=None if isinstance(recorded, NotRecorded) else recorded,
            expected=matcher,
            known_failure=known_failure,
            unmet_requirements=unmet_requirements,
        )
    return tuple(outcome_by_reference[reference] for reference in table.facts)


@pure
def decide_diagnostic_verdict(outcomes: Sequence[FactOutcome]) -> DiagnosticVerdict:
    """The verdict of a measured trial: failed, then not followed, then known, then passed."""
    statuses = {outcome.status for outcome in outcomes}
    if statuses & _FAILING_STATUSES:
        return DiagnosticVerdict.FAILED
    elif statuses & _NOT_FOLLOWED_STATUSES:
        return DiagnosticVerdict.NOT_FOLLOWED
    elif FactStatus.KNOWN in statuses:
        return DiagnosticVerdict.KNOWN
    else:
        return DiagnosticVerdict.PASSED


@pure
def _outcomes_with_status(outcomes: Sequence[FactOutcome], statuses: Set[FactStatus]) -> tuple[FactOutcome, ...]:
    return tuple(outcome for outcome in outcomes if outcome.status in statuses)


@pure
def check_trial_facts(facts: TrialFacts, table: ExpectedFactsTable) -> DiagnosticTrialCheck:
    """One trial's verdict against the table, with every fact that decided it."""
    outcomes = () if facts.not_measured_reason else evaluate_trial_facts(table, facts)
    return DiagnosticTrialCheck(
        trial_name=facts.trial_name,
        case_id=facts.case_id,
        harness=facts.harness,
        verdict=DiagnosticVerdict.NOT_MEASURED if facts.not_measured_reason else decide_diagnostic_verdict(outcomes),
        not_measured_reason=facts.not_measured_reason,
        failed_facts=_outcomes_with_status(outcomes, {FactStatus.FAILED}),
        not_recorded_facts=_outcomes_with_status(outcomes, {FactStatus.NOT_RECORDED}),
        known_facts=_outcomes_with_status(outcomes, {FactStatus.KNOWN}),
        not_followed_facts=_outcomes_with_status(outcomes, {FactStatus.NOT_FOLLOWED}),
        unmet_preconditions=_outcomes_with_status(outcomes, {FactStatus.PRECONDITION_NOT_MET}),
        unexpectedly_passing_facts=_outcomes_with_status(outcomes, {FactStatus.UNEXPECTEDLY_PASSING}),
    )


@pure
def check_job_facts(job_name: str, job_facts: Sequence[TrialFacts], table: ExpectedFactsTable) -> DiagnosticRunCheck:
    """Every trial of a job against the table.

    Raises ExpectedFactsTableError for a trial whose case is not the table's: a family's expected
    answers say nothing about another family's trial, so grading it would be a fabricated verdict. A
    trial that recorded no case at all never reached one, and is left to the not-measured verdict.
    """
    for facts in job_facts:
        if table.case_id and facts.case_id and facts.case_id != table.case_id:
            raise ExpectedFactsTableError(
                "the table grades the case {!r} but the trial {} ran the case {!r}".format(
                    table.case_id, facts.trial_name, facts.case_id
                )
            )
    return DiagnosticRunCheck(job_name=job_name, trials=tuple(check_trial_facts(facts, table) for facts in job_facts))


# --- reports ---


@pure
def render_recorded_facts(job_facts: Sequence[TrialFacts]) -> str:
    """Every trial's per-step block as JSON: the readings, and the facts whose records were due and
    never written beside them."""
    return (
        json.dumps(
            {
                facts.trial_name: {
                    "case_id": facts.case_id,
                    "harness": facts.harness,
                    "not_measured_reason": facts.not_measured_reason,
                    "steps": {
                        step.step_name: {
                            "facts": step.recorded_values,
                            "not_recorded_facts": step.not_recorded_names,
                        }
                        for step in facts.steps
                    },
                }
                for facts in job_facts
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


@pure
def _format_json_value(value: JsonValue) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= _MAX_CELL_VALUE_CHARS else text[: _MAX_CELL_VALUE_CHARS - 3] + "..."


@pure
def format_fact_matcher(matcher: FactMatcher) -> str:
    match matcher.kind:
        case FactMatcherKind.EXPECTED:
            return _format_json_value(matcher.value)
        case FactMatcherKind.AT_LEAST:
            return ">= {}".format(_format_json_value(matcher.value))
        case FactMatcherKind.AT_MOST:
            return "<= {}".format(_format_json_value(matcher.value))
        case FactMatcherKind.CONTAINS:
            return "contains {}".format(_format_json_value(matcher.value))
        case _ as unreachable:
            assert_never(unreachable)


@pure
def format_fact_outcome(outcome: FactOutcome) -> str:
    """One fact as a report reads it: `<fact> [<step>]: <recorded> vs expected <matcher>`, with what a
    known failure names and the requirements that missed."""
    notes = [
        *([outcome.known_failure.label] if outcome.known_failure is not None else []),
        *(["not recorded"] if outcome.status is FactStatus.NOT_RECORDED else []),
        *(["requires {}".format(", ".join(outcome.unmet_requirements))] if outcome.unmet_requirements else []),
    ]
    return "{}{}: {} vs expected {}{}".format(
        outcome.fact_name,
        " [{}]".format(outcome.step_name) if outcome.step_name else "",
        _format_json_value(outcome.recorded),
        format_fact_matcher(outcome.expected),
        " ({})".format("; ".join(notes)) if notes else "",
    )


@pure
def _format_facts_cell(outcomes: Sequence[FactOutcome]) -> str:
    return as_table_cell("; ".join(format_fact_outcome(outcome) for outcome in outcomes)) or "-"


@pure
def _format_verdict_cell(trial: DiagnosticTrialCheck) -> str:
    label = trial.verdict.value.replace("_", " ")
    return as_table_cell("{}: {}".format(label, trial.not_measured_reason) if trial.not_measured_reason else label)


@pure
def render_diagnostics_summary_markdown(run_check: DiagnosticRunCheck) -> str:
    """The job as a GitHub step-summary table: one row per trial, one verdict line above it."""
    header_lines = [
        "## minds-evals diagnostics: {} -- {}".format(run_check.job_name, "FAIL" if run_check.is_failed else "pass"),
        "",
        "| trial | case | harness | verdict | failed facts | not recorded | unexpectedly passing | known facts"
        " | not followed | unmet preconditions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    trial_lines = [
        "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            as_table_cell(trial.trial_name),
            as_table_cell(trial.case_id) or "-",
            as_table_cell(trial.harness) or "-",
            _format_verdict_cell(trial),
            _format_facts_cell(trial.failed_facts),
            _format_facts_cell(trial.not_recorded_facts),
            _format_facts_cell(trial.unexpectedly_passing_facts),
            _format_facts_cell(trial.known_facts),
            _format_facts_cell(trial.not_followed_facts),
            _format_facts_cell(trial.unmet_preconditions),
        )
        for trial in run_check.trials
    ]
    return "\n".join([*header_lines, *trial_lines, ""])


def write_diagnostics_reports(
    run_check: DiagnosticRunCheck | None,
    job_facts: Sequence[TrialFacts],
    summary_md_path: Path | None,
    summary_json_path: Path | None,
) -> None:
    """The verdicts where they were asked for, and the computed blocks beside them.

    The blocks are written on every run, not only under `--record-only`: a table asserts a handful of
    facts and the rest are the reading the report aggregates later.
    """
    recorded_facts_path = next(
        (
            path.with_name(path.stem + RECORDED_FACTS_SUFFIX)
            for path in (summary_json_path, summary_md_path)
            if path is not None
        ),
        None,
    )
    write_reports(
        [
            *(
                []
                if run_check is None
                else [
                    (summary_md_path, render_diagnostics_summary_markdown(run_check)),
                    (summary_json_path, run_check.model_dump_json(indent=2) + "\n"),
                ]
            ),
            (recorded_facts_path, render_recorded_facts(job_facts)),
        ]
    )
