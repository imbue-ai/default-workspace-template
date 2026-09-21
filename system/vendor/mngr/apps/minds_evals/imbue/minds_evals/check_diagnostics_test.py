"""How the checker grades a computed fact block: the table it accepts, and the verdict it reaches.

The trials are synthesized step directories, so every case here is reachable by a real job: a table
asserts facts the checker actually computes, and a miss is produced by editing the record the fact
reads rather than by handing the checker a value.
"""

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from imbue.minds_evals import evidence_collection
from imbue.minds_evals.check_diagnostics import check_job_facts
from imbue.minds_evals.check_diagnostics import compute_job_facts
from imbue.minds_evals.check_diagnostics import load_expected_facts_table
from imbue.minds_evals.check_diagnostics import render_diagnostics_summary_markdown
from imbue.minds_evals.data_types import DiagnosticRunCheck
from imbue.minds_evals.data_types import DiagnosticVerdict
from imbue.minds_evals.data_types import ExpectedFactsTable
from imbue.minds_evals.data_types import PreparationStage
from imbue.minds_evals.data_types import split_fact_reference
from imbue.minds_evals.errors import ExpectedFactsTableError
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.testing import DIAGNOSTIC_CASE_ID
from imbue.minds_evals.testing import DIAGNOSTIC_STEP_NAMES
from imbue.minds_evals.testing import DIAGNOSTIC_WORKER_NAME
from imbue.minds_evals.testing import DiagnosticStepFiles
from imbue.minds_evals.testing import GATES_CRITERION_NAMES
from imbue.minds_evals.testing import diagnostic_reward_details
from imbue.minds_evals.testing import diagnostic_state
from imbue.minds_evals.testing import diagnostic_step
from imbue.minds_evals.testing import edited_diagnostic_step
from imbue.minds_evals.testing import write_diagnostic_trial_dir

# The repository's own table for every live cell, which the checker has to accept as written.
LIVE_INVARIANTS_PATH: Path = Path(__file__).resolve().parents[2] / "configs" / "diagnostics" / "live_invariants.json"

# The fixture family's table, and a live fixture job trimmed to the records its facts read. The two
# are one measurement: the table says what a fixture trial records, and this is the trial that did.
# Trimmed means the snapshot is a tarball holding the one path `snapshot.readable` looks for, the
# flow frames are emptied because only their count is read, and everything no fact opens is gone.
FIXTURE_TABLE_PATH: Path = (
    Path(__file__).resolve().parents[2] / "configs" / "diagnostics" / "fixture_expected_facts.json"
)
FIXTURE_JOB_DIR: Path = Path(__file__).parent / "test_fixtures" / "diagnostics_fixture_job"


def _table(facts: Mapping[str, Any], **top_level: Any) -> ExpectedFactsTable:
    return ExpectedFactsTable.model_validate({"facts": dict(facts), **top_level})


def _load_table(tmp_path: Path, raw_table: Mapping[str, Any]) -> ExpectedFactsTable:
    """The table as `check-diagnostics` reads it: off a file, with every refusal an
    ExpectedFactsTableError rather than the pydantic error a validator raises inside."""
    table_path = tmp_path / "expected.json"
    table_path.write_text(json.dumps(raw_table))
    return load_expected_facts_table(table_path)


def _stepped_trial(job_dir: Path, trial_name: str, **overrides: DiagnosticStepFiles) -> Path:
    """A two-step behaviour trial, with either step replaced by name."""
    steps = [
        overrides.get(name, diagnostic_step(name, step_index=index, step_total=2))
        for index, name in enumerate(DIAGNOSTIC_STEP_NAMES)
    ]
    return write_diagnostic_trial_dir(job_dir, trial_name, steps)


def _checked(tmp_path: Path, job_dir: Path, table: ExpectedFactsTable) -> DiagnosticRunCheck:
    return check_job_facts(job_dir.name, compute_job_facts(job_dir, tmp_path / "work"), table)


# --- the table ---


@pytest.mark.parametrize(
    ("reference", "expected"),
    [("gates.held", ("gates.held", "")), ("gates.held@work", ("gates.held", "work"))],
)
def test_a_fact_reference_names_its_step_or_the_last_one(reference: str, expected: tuple[str, str]) -> None:
    assert split_fact_reference(reference) == expected


@pytest.mark.parametrize(
    ("table", "message"),
    [
        pytest.param({"facts": {"gates.held": {}}}, "give exactly one of", id="no matcher"),
        pytest.param({"facts": {"gates.held": {"expected": True, "at_least": 1}}}, "give one matcher", id="two"),
        pytest.param({"facts": {"gates.held": {"at_least": None}}}, "needs a number", id="null bound"),
        pytest.param({"facts": {"gates.held": {"expected": True, "gate": True}}}, "gate", id="unknown key"),
        pytest.param(
            {"facts": {"gates.held": {"expected": None}}}, "needs a known_failure", id="unobservable with no mark"
        ),
        pytest.param(
            {"facts": {"listing.complete": {"expected": True, "compliance": True, "known_failure": {"issue": 1}}}},
            "cannot declare a known failure",
            id="compliance with a known failure",
        ),
        pytest.param(
            {"facts": {"listing.complete": {"expected": True, "compliance": True, "health": True}}},
            "not both",
            id="compliance and health",
        ),
        pytest.param(
            {
                "facts": {
                    "gates.held": {"expected": True, "requires": ["trial.completed"]},
                    "trial.completed": {"expected": True},
                }
            },
            "not a compliance or health fact",
            id="requires an instrument fact",
        ),
        pytest.param(
            {
                "facts": {
                    "listing.complete": {"expected": True, "health": True, "requires": ["probe.read"]},
                    "probe.read": {"expected": True, "health": True, "requires": ["listing.complete"]},
                }
            },
            "cycle",
            id="requirement cycle",
        ),
        pytest.param(
            {"requires": ["prep.stage_reached"], "facts": {"gates.held": {"expected": True}}},
            "which it does not assert",
            id="table requires an unasserted fact",
        ),
        pytest.param({"facts": {"gates.held@work@check": {"expected": True}}}, "more than one step", id="two steps"),
        pytest.param({"facts": {"gates.held@": {"expected": True}}}, "is not a", id="empty step"),
        pytest.param({"facts": {"@work": {"expected": True}}}, "is not a", id="no fact"),
        pytest.param(
            {
                "facts": {
                    "listing.complete": {"expected": True, "health": True},
                    "gates.held": {
                        "expected": True,
                        "by_harness": {"codex": {"expected": None}},
                        "requires": ["listing.complete"],
                    },
                }
            },
            "needs a known_failure",
            id="unobservable override with no mark",
        ),
        pytest.param(
            {
                "requires": ["prep.stage_reached"],
                "facts": {
                    "prep.stage_reached": {"expected": "conversation", "health": True, "requires": ["gates.held"]},
                    "gates.held": {"expected": True, "health": True},
                },
            },
            "cycle",
            id="cycle through the table's own requirement",
        ),
    ],
)
def test_a_table_that_cannot_be_checked_is_refused(tmp_path: Path, table: Mapping[str, Any], message: str) -> None:
    with pytest.raises(ExpectedFactsTableError) as raised:
        _load_table(tmp_path, table)

    assert message in str(raised.value)


def test_a_known_failure_names_an_issue_or_a_reason_and_not_both(tmp_path: Path) -> None:
    """A defect with neither is a permanent exemption with nothing to look it up by."""
    named = {"facts": {"gates.held": {"expected": False, "known_failure": {"reason": "the gate is being rewritten"}}}}
    assert _load_table(tmp_path, named).facts["gates.held"].known_failure is not None
    for raw_known_failure in ({}, {"issue": 898, "reason": "also this"}):
        with pytest.raises(ExpectedFactsTableError):
            _load_table(tmp_path, {"facts": {"gates.held": {"expected": False, "known_failure": raw_known_failure}}})


def test_the_live_invariants_table_is_one_the_checker_accepts() -> None:
    """It names no case, so it grades any trial; that is what lets the evaluate job read every live
    cell against it."""
    table = load_expected_facts_table(LIVE_INVARIANTS_PATH)

    assert table.case_id == ""
    assert table.facts["workers.no_phantoms"].requires == ("listing.complete",)
    assert table.facts["listing.complete"].is_health


def test_a_healthy_live_trial_holds_every_invariant(tmp_path: Path) -> None:
    """Both shapes a live cell takes: the nightly's flat cases and its stepped ones."""
    job_dir = tmp_path / "evaluate"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    write_diagnostic_trial_dir(job_dir, "todo-app__bbbbbbb", [diagnostic_step()])

    run_check = _checked(tmp_path, job_dir, load_expected_facts_table(LIVE_INVARIANTS_PATH))

    assert [trial.verdict for trial in run_check.trials] == [DiagnosticVerdict.PASSED, DiagnosticVerdict.PASSED]


# --- the case the table grades ---


def test_a_job_whose_trials_ran_another_case_is_refused(tmp_path: Path) -> None:
    """A family's expected answers say nothing about another family's trial, so grading one would be
    a verdict about a trial the table never described."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table({"gates.held": {"expected": True}}, case_id="fixture")

    with pytest.raises(ExpectedFactsTableError) as raised:
        _checked(tmp_path, job_dir, table)

    assert "fixture" in str(raised.value) and DIAGNOSTIC_CASE_ID in str(raised.value)


def test_a_table_that_names_the_case_grades_it(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")

    run_check = _checked(tmp_path, job_dir, _table({"gates.held": {"expected": True}}, case_id=DIAGNOSTIC_CASE_ID))

    assert [trial.verdict for trial in run_check.trials] == [DiagnosticVerdict.PASSED]


def test_an_empty_job_is_a_job_that_cannot_be_read(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    job_dir.mkdir()

    with pytest.raises(JobReadError):
        compute_job_facts(job_dir, tmp_path / "work")


# --- the verdict lattice ---


def test_a_trial_that_never_produced_a_workspace_is_not_measured(tmp_path: Path) -> None:
    """Harbor's exception stopped the trial before the driver wrote any state, so there is no
    instrument reading to grade and nothing is asserted."""
    job_dir = tmp_path / "diagnose-behaviour"
    write_diagnostic_trial_dir(job_dir, "behaviour__aaaaaaa", [], exception_type="EnvironmentStartTimeoutError")

    (trial,) = _checked(tmp_path, job_dir, _table({"gates.held": {"expected": True}})).trials

    assert trial.verdict is DiagnosticVerdict.NOT_MEASURED
    assert "EnvironmentStartTimeoutError" in trial.not_measured_reason
    assert trial.failed_facts == ()


def test_a_failure_after_the_workspace_exists_is_measured(tmp_path: Path) -> None:
    """A tunnel, a sign-in or a switch that failed is the instrument's, so it is a failed
    `prep.stage_reached` rather than a night with no measurement."""
    job_dir = tmp_path / "diagnose-behaviour"
    step = edited_diagnostic_step(
        diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2),
        {"agent/state.json": json.dumps(diagnostic_state(DIAGNOSTIC_STEP_NAMES[0], preparation_stage="created"))},
    )
    write_diagnostic_trial_dir(job_dir, "behaviour__aaaaaaa", [step])

    (trial,) = _checked(tmp_path, job_dir, _table({"prep.stage_reached": {"expected": "conversation"}})).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.failed_facts] == ["prep.stage_reached"]


def test_a_known_failure_does_not_excuse_a_record_that_was_never_written(tmp_path: Path) -> None:
    """A mark says what the defect records, not that the fact may go unread: an instrument that wrote
    no record is broken whatever the table expected."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {"verifier/reward-details.json": None},
        ),
    )
    table = _table(
        {"harness.detected": {"expected": None, "known_failure": {"issue": 898}}},
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.not_recorded_facts] == ["harness.detected"]


def test_a_known_failure_whose_matcher_the_trial_records_is_known(tmp_path: Path) -> None:
    """The codex lane records no model name on its steps, so the table states what the defect records
    and a trial that records it is known rather than red."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table(
        {
            "transcript.agent_steps_with_model_name": {
                "expected": "none",
                "known_failure": {"issue": 898},
                "by_harness": {"claude": {"expected": "all"}},
            }
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    # The trial is a claude one, so the override's matcher applies and the mark rides along with it.
    assert trial.verdict is DiagnosticVerdict.KNOWN
    assert [fact.fact_name for fact in trial.known_facts] == ["transcript.agent_steps_with_model_name"]


def test_a_known_failure_the_trial_no_longer_records_is_unexpectedly_passing(tmp_path: Path) -> None:
    """Every known failure is strict: a defect that stopped happening means the mark must come off."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table({"transcript.agent_steps_with_model_name": {"expected": "none", "known_failure": {"issue": 898}}})

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.unexpectedly_passing_facts] == ["transcript.agent_steps_with_model_name"]
    assert trial.unexpectedly_passing_facts[0].known_failure is not None


def test_a_compliance_fact_that_reads_false_is_not_followed_and_holds_back_what_needs_it(tmp_path: Path) -> None:
    """The agent launched no worker, so nothing the worker readers say about this trial means
    anything -- and none of it is the instrument's failure."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table(
        {
            "workers.listed_agent_created": {"expected": [DIAGNOSTIC_WORKER_NAME], "compliance": True},
            "workers.discovered_equals_listed": {
                "expected": True,
                "requires": ["workers.listed_agent_created"],
            },
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.NOT_FOLLOWED
    assert [fact.fact_name for fact in trial.not_followed_facts] == ["workers.listed_agent_created"]
    assert [fact.fact_name for fact in trial.unmet_preconditions] == ["workers.discovered_equals_listed"]
    assert trial.unmet_preconditions[0].unmet_requirements == ("workers.listed_agent_created",)


def test_a_compliance_fact_the_instrument_could_not_read_fails(tmp_path: Path) -> None:
    """A compliance source that could not be read is the instrument's failure, so it must not be
    reported as the agent not doing what it was asked."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {"agent/verification/tickets.jsonl": evidence_collection.ticket_failure_jsonl("bridge_failed")},
        ),
    )
    table = _table({"tickets.step_titles": {"expected": [], "compliance": True}})

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.failed_facts] == ["tickets.step_titles"]


def test_a_health_fact_that_misses_fails_rather_than_reading_as_the_agents_doing(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {
                "agent/verification/workers/listing.json": json.dumps(
                    {"exit_code": 1, "errors": ["modal unreachable"], "is_complete": False}
                )
            },
        ),
    )
    table = _table({"listing.complete": {"expected": True, "health": True}})

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.failed_facts] == ["listing.complete"]


def test_a_fact_the_table_asserts_and_the_step_never_recorded_fails(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {"agent/verification/manifest.json": None},
        ),
    )
    table = _table({"evidence.errored_entries": {"expected": []}})

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.not_recorded_facts] == ["evidence.errored_entries"]
    assert trial.failed_facts == ()


def test_a_fact_is_read_from_the_step_its_reference_names(tmp_path: Path) -> None:
    """Without a step a fact is read from the last one that ran, which is the reading a table means
    unless it says otherwise."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table(
        {
            "entries.count@work": {"expected": 1},
            "entries.count@check": {"expected": 2},
            "entries.count": {"expected": 2},
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.PASSED


def test_a_fact_pinned_to_a_step_that_never_ran_is_not_recorded(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    write_diagnostic_trial_dir(
        job_dir, "behaviour__aaaaaaa", [diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2)]
    )

    (trial,) = _checked(tmp_path, job_dir, _table({"gates.held@check": {"expected": True}})).trials

    assert [fact.fact_name for fact in trial.not_recorded_facts] == ["gates.held"]


def test_the_table_level_requirement_holds_every_fact_back_at_once(tmp_path: Path) -> None:
    """A cell that never got through preparation reads as that one miss rather than as every fact of
    the table failing."""
    job_dir = tmp_path / "diagnose-behaviour"
    step = edited_diagnostic_step(
        diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2),
        {"agent/state.json": json.dumps(diagnostic_state(DIAGNOSTIC_STEP_NAMES[0], preparation_stage="signed_in"))},
    )
    write_diagnostic_trial_dir(job_dir, "behaviour__aaaaaaa", [step])
    table = _table(
        {
            "prep.stage_reached@work": {"expected": "conversation", "health": True},
            "gates.held": {"expected": True},
            "manifest.readable": {"expected": True},
        },
        requires=["prep.stage_reached@work"],
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.failed_facts] == ["prep.stage_reached"]
    assert sorted(fact.fact_name for fact in trial.unmet_preconditions) == ["gates.held", "manifest.readable"]


@pytest.mark.parametrize(
    ("matcher", "is_matched"),
    [
        pytest.param({"expected": 2}, True, id="expected"),
        pytest.param({"expected": True}, False, id="a boolean never equals a number"),
        pytest.param({"at_least": 2}, True, id="at_least"),
        pytest.param({"at_least": 3}, False, id="at_least misses"),
        pytest.param({"at_most": 2}, True, id="at_most"),
        pytest.param({"at_most": 1}, False, id="at_most misses"),
    ],
)
def test_the_matchers_compare_what_the_step_recorded(
    tmp_path: Path, matcher: Mapping[str, Any], is_matched: bool
) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")

    (trial,) = _checked(tmp_path, job_dir, _table({"entries.count": dict(matcher)})).trials

    assert (trial.verdict is DiagnosticVerdict.PASSED) is is_matched


def test_contains_finds_a_member_of_a_recorded_list(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")

    (trial,) = _checked(
        tmp_path, job_dir, _table({"steps.boundary_markers": {"contains": DIAGNOSTIC_STEP_NAMES[1]}})
    ).trials

    assert trial.verdict is DiagnosticVerdict.PASSED


def test_a_harness_override_applies_only_to_its_own_harness(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "claude__aaaaaaa")
    write_diagnostic_trial_dir(
        job_dir,
        "codex__bbbbbbb",
        [diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2, harness="codex")],
    )
    table = _table(
        {
            "arm.harness_config.harness": {
                "expected": "claude",
                "by_harness": {"codex": {"expected": "codex"}},
            }
        }
    )

    run_check = _checked(tmp_path, job_dir, table)

    assert [trial.verdict for trial in run_check.trials] == [DiagnosticVerdict.PASSED, DiagnosticVerdict.PASSED]
    assert [trial.harness for trial in run_check.trials] == ["claude", "codex"]


# --- reports ---


def test_the_markdown_summary_names_every_fact_that_decided_a_trial(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table({"entries.count@work": {"expected": 9}})

    markdown = render_diagnostics_summary_markdown(_checked(tmp_path, job_dir, table))

    assert "FAIL" in markdown
    assert "entries.count [work]: 1 vs expected 9" in markdown


def test_a_requirement_is_met_only_when_its_own_fact_passed(tmp_path: Path) -> None:
    """The listing was due and never written, so it fails on its own account and the fact that reads
    it says nothing rather than being charged with the gap twice."""
    job_dir = tmp_path / "evaluate"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {"agent/verification/workers/listing.json": None},
        ),
    )

    (trial,) = _checked(tmp_path, job_dir, load_expected_facts_table(LIVE_INVARIANTS_PATH)).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.not_recorded_facts] == ["listing.complete"]
    assert [fact.fact_name for fact in trial.unmet_preconditions] == ["workers.no_phantoms"]


def test_a_genuine_failure_beats_a_known_one(tmp_path: Path) -> None:
    """A night with a declared known failure is still red when something else broke."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table(
        {
            "transcript.agent_steps_with_model_name": {
                "expected": "none",
                "known_failure": {"issue": 898},
                "by_harness": {"claude": {"expected": "all"}},
            },
            "entries.count": {"expected": 9},
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [fact.fact_name for fact in trial.known_facts] == ["transcript.agent_steps_with_model_name"]
    assert [fact.fact_name for fact in trial.failed_facts] == ["entries.count"]


def test_check_runs_own_criteria_are_facts_a_table_asserts(tmp_path: Path) -> None:
    """`check-run` stays absolute; here the same four readings are facts, so a table can declare one
    known or override it per harness."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(
        job_dir,
        "behaviour__aaaaaaa",
        check=edited_diagnostic_step(
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
            {
                "agent/state.json": json.dumps(
                    diagnostic_state(DIAGNOSTIC_STEP_NAMES[1], test_state="timed_out", entry_count=2)
                ),
                "verifier/reward-details.json": json.dumps(
                    diagnostic_reward_details(failed_gate_names=("not_timed_out",))
                ),
            },
        ),
    )
    table = _table(
        {
            "trial.completed": {"expected": True},
            "gates.held": {"expected": True},
            "gates.results": {"expected": {name: True for name in GATES_CRITERION_NAMES}},
            "evidence.errored_entries": {"expected": []},
            "arm.harness_config.is_model_confirmed": {"expected": True},
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert sorted(fact.fact_name for fact in trial.failed_facts) == [
        "gates.held",
        "gates.results",
        "trial.completed",
    ]


def test_a_harness_override_may_declare_only_a_known_failure(tmp_path: Path) -> None:
    """The matcher stays the fact's own, so a defect that records the same value on one harness is
    marked without restating what to expect."""
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "codex__aaaaaaa")
    table = _table(
        {
            "arm.harness_config.harness": {
                "expected": "claude",
                "by_harness": {"claude": {"known_failure": {"reason": "the lane is being renamed"}}},
            }
        }
    )

    (trial,) = _checked(tmp_path, job_dir, table).trials

    assert trial.verdict is DiagnosticVerdict.KNOWN
    assert trial.known_facts[0].known_failure is not None
    assert trial.known_facts[0].known_failure.label == "the lane is being renamed"


def test_a_trial_that_recorded_no_case_at_all_is_left_to_its_verdict(tmp_path: Path) -> None:
    """A trial harbor stopped before the driver wrote state names no case; refusing the job over it
    would turn a cell that was never measured into one the checker could not read."""
    job_dir = tmp_path / "diagnose-behaviour"
    write_diagnostic_trial_dir(job_dir, "behaviour__aaaaaaa", [], exception_type="EnvironmentStartTimeoutError")

    (trial,) = _checked(
        tmp_path, job_dir, _table({"gates.held": {"expected": True}}, case_id=DIAGNOSTIC_CASE_ID)
    ).trials

    assert trial.verdict is DiagnosticVerdict.NOT_MEASURED


def test_a_trial_that_gave_up_before_the_workspace_says_which_wait_ran_out(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    step = edited_diagnostic_step(
        diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2),
        {
            "agent/state.json": json.dumps(
                diagnostic_state(
                    DIAGNOSTIC_STEP_NAMES[0],
                    preparation_stage="",
                    test_state="timed_out",
                    timed_out_reason="the workspace never became usable",
                )
            )
        },
    )
    write_diagnostic_trial_dir(job_dir, "behaviour__aaaaaaa", [step])

    (trial,) = _checked(tmp_path, job_dir, _table({"gates.held": {"expected": True}})).trials

    assert trial.verdict is DiagnosticVerdict.NOT_MEASURED
    assert "the workspace never became usable" in trial.not_measured_reason


@pytest.mark.parametrize(
    ("fact_reference", "matcher", "is_matched"),
    [
        pytest.param("transcript.source", {"contains": "space"}, True, id="contains a substring"),
        pytest.param("transcript.source", {"contains": "codex"}, False, id="contains misses"),
        pytest.param("trial.completed", {"at_least": 1}, False, id="a bound never matches a boolean"),
        pytest.param("transcript.source", {"at_most": 1}, False, id="a bound never matches a string"),
        pytest.param(
            "gates.results", {"expected": {name: True for name in GATES_CRITERION_NAMES}}, True, id="a dict value"
        ),
        pytest.param("gates.results", {"expected": {"not_timed_out": True}}, False, id="a dict missing keys"),
    ],
)
def test_the_matchers_compare_the_shapes_a_table_actually_asserts(
    tmp_path: Path, fact_reference: str, matcher: Mapping[str, Any], is_matched: bool
) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")

    (trial,) = _checked(tmp_path, job_dir, _table({fact_reference: dict(matcher)})).trials

    assert (trial.verdict is DiagnosticVerdict.PASSED) is is_matched


def test_the_markdown_summary_names_a_known_failure_and_the_requirement_that_missed(tmp_path: Path) -> None:
    job_dir = tmp_path / "diagnose-behaviour"
    _stepped_trial(job_dir, "behaviour__aaaaaaa")
    table = _table(
        {
            "tickets.step_titles": {"expected": ["DIAG alpha 7f3a"], "compliance": True},
            "tickets.closed_step_titles": {"expected": [], "requires": ["tickets.step_titles"]},
            "transcript.agent_steps_with_model_name": {"expected": "all", "known_failure": {"issue": 898}},
        }
    )

    markdown = render_diagnostics_summary_markdown(_checked(tmp_path, job_dir, table))

    assert "transcript.agent_steps_with_model_name" in markdown and "#898" in markdown
    assert "requires tickets.step_titles" in markdown
    assert "not followed" in markdown


# --- the fixture family's own table, against the trial that measured it ---


def _fixture_job_check(tmp_path: Path, table: ExpectedFactsTable) -> DiagnosticRunCheck:
    return check_job_facts(FIXTURE_JOB_DIR.name, compute_job_facts(FIXTURE_JOB_DIR, tmp_path / "work"), table)


def test_the_fixture_table_holds_against_the_trial_that_measured_it(tmp_path: Path) -> None:
    """The table states what a fixture trial records, so a value nobody measured has to be caught
    here rather than on the night the cell first runs."""
    (trial,) = _fixture_job_check(tmp_path, load_expected_facts_table(FIXTURE_TABLE_PATH)).trials

    assert [outcome.fact_name for outcome in trial.failed_facts] == []
    assert [outcome.fact_name for outcome in trial.not_recorded_facts] == []
    assert [outcome.fact_name for outcome in trial.not_followed_facts] == []
    assert [outcome.fact_name for outcome in trial.unmet_preconditions] == []
    assert trial.verdict is DiagnosticVerdict.PASSED


@pytest.mark.parametrize(
    ("fact_reference", "matcher"),
    [
        pytest.param("evidence.statuses", {"expected": {}}, id="entry statuses"),
        pytest.param("flow.unnamed.after.3.checked", {"expected": ["walk dog"]}, id="a flow reading"),
        pytest.param("judge.screenshot_count", {"expected": 0}, id="what the judge was handed"),
        pytest.param("timing.agrees_with_feed", {"expected": False}, id="the timing agreement"),
    ],
)
def test_a_fixture_table_value_the_trial_did_not_record_fails_it(
    tmp_path: Path, fact_reference: str, matcher: Mapping[str, Any]
) -> None:
    """The table passing has to mean the trial agreed with it, not that the facts under it are
    unreadable or the entries unasserted: each edit here is the same table with one wrong answer."""
    raw_table = json.loads(FIXTURE_TABLE_PATH.read_text())
    raw_table["facts"][fact_reference] = dict(matcher)

    (trial,) = _fixture_job_check(tmp_path, _load_table(tmp_path, raw_table)).trials

    assert [outcome.fact_name for outcome in trial.failed_facts] == [fact_reference]
    assert trial.verdict is DiagnosticVerdict.FAILED


def test_a_fixture_trial_that_never_reached_its_conversation_reads_as_that_one_miss(tmp_path: Path) -> None:
    """Preparation stopping short leaves every record the table asserts on absent, so without the
    table's own requirement one such trial would report a failure per fact the table holds."""
    job_dir = tmp_path / "diagnose-fixture"
    shutil.copytree(FIXTURE_JOB_DIR, job_dir)
    state_path = next(job_dir.glob("*/steps/*/agent/state.json"))
    state = json.loads(state_path.read_text())
    state_path.write_text(json.dumps({**state, "preparation_stage": PreparationStage.SIGNED_IN.value}))
    table = load_expected_facts_table(FIXTURE_TABLE_PATH)

    (trial,) = check_job_facts(job_dir.name, compute_job_facts(job_dir, tmp_path / "work"), table).trials

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert [outcome.fact_name for outcome in trial.failed_facts] == ["prep.stage_reached"]
    assert [outcome.fact_name for outcome in trial.not_recorded_facts] == []
    assert len(trial.unmet_preconditions) == len(table.facts) - 1
