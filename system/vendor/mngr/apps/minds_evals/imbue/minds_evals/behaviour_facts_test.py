"""The behaviour family's table, graded against the live cell that measured it, one per harness.

Each cell's job directory is checked in under `test_fixtures/diagnostics_behaviour_jobs/`, trimmed to
the records the facts read: claude on haiku, pi-coding on gpt-5-mini, codex on gpt-5.6-luna. The
table and those cells are one measurement, so a wrong table entry, a fact that reads the wrong
record, and a reader that disagrees with what a real harness produced each fail here rather than on
the night.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.minds_evals import behaviour_facts
from imbue.minds_evals import diagnostic_probe
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.check_diagnostics import check_job_facts
from imbue.minds_evals.check_diagnostics import compute_job_facts
from imbue.minds_evals.check_diagnostics import load_expected_facts_table
from imbue.minds_evals.data_types import DiagnosticRunCheck
from imbue.minds_evals.data_types import DiagnosticTrialCheck
from imbue.minds_evals.data_types import DiagnosticVerdict
from imbue.minds_evals.data_types import ExpectedFactsTable
from imbue.minds_evals.data_types import FactValue
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import BEHAVIOUR_EXPECTED_FACTS_PATH
from imbue.minds_evals.testing import BEHAVIOUR_HARNESSES
from imbue.minds_evals.testing import DIAGNOSTIC_STEP_NAMES
from imbue.minds_evals.testing import behaviour_job_dir
from imbue.minds_evals.testing import behaviour_step_dir
from imbue.minds_evals.testing import behaviour_trajectory_fixture
from imbue.minds_evals.testing import copied_behaviour_job_dir

_WORK_STEP, _CHECK_STEP = DIAGNOSTIC_STEP_NAMES
_PROBE_PATH: Final[str] = "steps/{}/agent/verification/{}".format(
    _WORK_STEP, evidence_collection.DIAGNOSTIC_PROBE_FILENAME
)
_DRIVER_EVENTS_PATH: Final[str] = "steps/{}/agent/driver_events.jsonl".format(_CHECK_STEP)
_TRAJECTORY_PATH: Final[str] = "steps/{}/agent/trajectory.json".format(_CHECK_STEP)
_LISTING_PATH: Final[str] = "steps/{}/agent/verification/workers/{}".format(
    _CHECK_STEP, evidence_collection.WORKER_LISTING_OUTCOME_FILENAME
)

# What each harness's cell records that the table marks as a known defect rather than a regression.
_KNOWN_FACTS_BY_HARNESS: Final[Mapping[str, frozenset[str]]] = {
    "claude": frozenset({"workers.model_is_lead_model"}),
    "pi-coding": frozenset({"workers.model_is_lead_model"}),
    "codex": frozenset(
        {
            "workers.model_is_lead_model",
            "failures.missing_command_is_error",
            "transcript.agent_steps_with_model_name",
            "arm.harness_config.is_model_confirmed",
            "usage.tokens_present",
            "steps.spend_deltas_sum",
        }
    ),
}


def _table() -> ExpectedFactsTable:
    return load_expected_facts_table(BEHAVIOUR_EXPECTED_FACTS_PATH)


def _checked(
    tmp_path: Path,
    harness: str,
    edited_files: Mapping[str, str | None] | None = None,
    **table_overrides: Any,
) -> DiagnosticTrialCheck:
    """One harness's cell against the checked-in table, with the named records edited.

    An unedited cell is read where it is checked in; an edited one is read from a copy, so the
    checked-in record stays what the trial wrote.
    """
    job_dir = (
        behaviour_job_dir(harness)
        if edited_files is None
        else copied_behaviour_job_dir(tmp_path / "edited", harness, edited_files)
    )
    table = _table() if not table_overrides else _overridden_table(**table_overrides)
    run: DiagnosticRunCheck = check_job_facts(job_dir.name, compute_job_facts(job_dir, tmp_path / "work"), table)
    (trial,) = run.trials
    return trial


def _overridden_table(**facts: Any) -> ExpectedFactsTable:
    raw_table = json.loads(BEHAVIOUR_EXPECTED_FACTS_PATH.read_text())
    for reference, expectation in facts.items():
        raw_table["facts"][reference.replace("__", "@")] = expectation
    return ExpectedFactsTable.model_validate(raw_table)


def _fact_names(outcomes: Any) -> set[str]:
    return {outcome.fact_name for outcome in outcomes}


@pytest.mark.parametrize("harness", BEHAVIOUR_HARNESSES)
def test_each_harnesss_live_cell_matches_the_table_but_for_its_declared_defects(tmp_path: Path, harness: str) -> None:
    trial = _checked(tmp_path, harness)

    assert (trial.verdict, _fact_names(trial.known_facts)) == (
        DiagnosticVerdict.KNOWN,
        set(_KNOWN_FACTS_BY_HARNESS[harness]),
    )
    assert (trial.failed_facts, trial.not_recorded_facts, trial.not_followed_facts, trial.unmet_preconditions) == (
        (),
        (),
        (),
        (),
    )


@pytest.mark.parametrize(
    ("reference", "expectation"),
    [
        pytest.param("progress.nonce_block_count", {"expected": 3}, id="a block count the timeline does not render"),
        pytest.param("steps.upload_marker_present@work", {"expected": True}, id="an upload before its own step"),
        pytest.param(
            "workers.listed_agent_created",
            {"expected": ["diag-worker-7f3a", "other"], "requires": ["agent.worker_launched@work"]},
            id="a worker the listing does not name",
        ),
        pytest.param(
            "process.invoked_skills@work",
            {"expected": ["build-app", "frontend-design"], "requires": ["agent.invoked_skill@work"]},
            id="a skill the transcript does not show",
        ),
        pytest.param(
            "evidence.statuses@work",
            {"expected": {"file_inventory": "passed"}, "requires": ["agent.invoked_skill@work"]},
            id="an entry set missing the process checks",
        ),
    ],
)
def test_a_table_value_the_cell_does_not_record_fails_it(
    tmp_path: Path, reference: str, expectation: Mapping[str, Any]
) -> None:
    """The guard against a table that would pass whatever the trial recorded: each of these entries
    states something a healthy cell does not record, and each has to turn the verdict red."""
    trial = _checked(tmp_path, "claude", **{reference.replace("@", "__"): expectation})

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert _fact_names(trial.failed_facts) == {reference.split("@")[0]}


def test_a_probe_that_did_not_answer_is_the_instruments_failure_and_not_the_agents(tmp_path: Path) -> None:
    """A probe whose exec failed writes its reason instead of its sections, so the compliance facts it
    is the only source for cannot be read -- which is never grounds for saying the agent did not
    comply."""
    trial = _checked(
        tmp_path,
        "claude",
        {_PROBE_PATH: evidence_collection.diagnostic_probe_failure_text("bridge_failed", "")},
    )

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert "probe.read" in _fact_names(trial.failed_facts)
    assert {"agent.step_tickets", "agent.regular_ticket", "agent.worker_launched"} <= _fact_names(trial.failed_facts)
    assert trial.not_followed_facts == ()


def test_a_tool_call_whose_input_was_never_served_fails_the_feeds_health_fact(tmp_path: Path) -> None:
    """The detail read is the only source for what the agent's commands were, so a call it did not
    answer for leaves the feed unable to speak -- a health miss, not a quiet false."""
    trial = _checked(tmp_path, "claude", {_DRIVER_EVENTS_PATH: _driver_events_missing_one_input("claude")})

    assert trial.verdict is DiagnosticVerdict.FAILED
    assert "feed.inputs_read" in _fact_names(trial.failed_facts)


def _driver_events_missing_one_input(harness: str) -> str:
    """The step's driver events with the first detail record emptied, as an event the chat app served
    no detail for leaves it."""
    path = behaviour_step_dir(harness, _CHECK_STEP) / "agent" / "driver_events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    is_emptied = False
    for record in records:
        if record.get("type") == "event_detail" and not is_emptied:
            record["detail"] = None
            is_emptied = True
    assert is_emptied, "the {} cell recorded no detail payload to empty".format(harness)
    return "".join(json.dumps(record) + "\n" for record in records)


def test_an_incomplete_listing_leaves_the_worker_agreements_unasserted(tmp_path: Path) -> None:
    """`mngr list` answers with the agents it reached, so only a complete listing lets a worker's
    absence from it mean anything. The agreements that read it wait on that health fact."""
    incomplete_listing = json.dumps({"exit_code": 1, "errors": ["could not reach the provider"], "is_complete": False})
    trial = _checked(tmp_path, "claude", {_LISTING_PATH: incomplete_listing})

    assert "listing.complete" in _fact_names(trial.failed_facts)
    assert {
        "workers.no_phantoms",
        "workers.listed_agent_created",
        "workers.discovered_equals_listed",
        "workers.captured_equals_listed",
        "workers.embedded_equals_listed",
        "workers.harness_is_lead_harness",
    } <= _fact_names(trial.unmet_preconditions)


def test_the_upload_marker_counts_whatever_tool_read_it(tmp_path: Path) -> None:
    """The prompt asks the agent to read the upload's marker, not to run a command, so a harness that
    answered the item with its own file-reading tool read it just as well as one that ran `cat`."""
    trajectory = _trajectory_with_marker_read_by("claude", "read")

    assert _upload_marker_read(tmp_path, "claude", trajectory) is True


def test_a_trajectory_that_never_shows_the_marker_reads_false(tmp_path: Path) -> None:
    trajectory = _trajectory_without_marker_text("claude")

    assert _upload_marker_read(tmp_path, "claude", trajectory) is False


def _upload_marker_read(tmp_path: Path, harness: str, trajectory: str) -> FactValue:
    """What the cell's check step records for `steps.upload_marker_read` with that trajectory in
    place of the one the trial captured."""
    job_dir = copied_behaviour_job_dir(tmp_path / "trajectory", harness, {_TRAJECTORY_PATH: trajectory})
    (facts,) = compute_job_facts(job_dir, tmp_path / "work")
    return facts.block(_CHECK_STEP)["steps.upload_marker_read"]


def _trajectory_with_marker_read_by(harness: str, tool_name: str) -> str:
    """The check step's trajectory with the calls whose results carried the marker renamed to that
    tool, as a harness that read the file rather than running a command records them."""
    document = behaviour_trajectory_fixture(harness)
    call_ids = {
        str(result.get("source_call_id") or "")
        for result in _captured_results(document)
        if diagnostic_probe.UPLOAD_MARKER_TEXT in str(result.get("content") or "")
    }
    assert call_ids, "the {} cell captured no result carrying the marker".format(harness)
    for step in document["steps"]:
        for call in step.get("tool_calls") or []:
            if str(call.get("tool_call_id") or "") in call_ids:
                call["function_name"] = tool_name
    for result in _captured_results(document):
        if str(result.get("source_call_id") or "") in call_ids and isinstance(result.get("extra"), dict):
            result["extra"]["tool_name"] = tool_name
    return json.dumps(document)


def _trajectory_without_marker_text(harness: str) -> str:
    """The same trajectory with the marker gone from every result, as a trial whose agent never read
    the upload leaves it."""
    document = behaviour_trajectory_fixture(harness)
    for result in _captured_results(document):
        result["content"] = str(result.get("content") or "").replace(diagnostic_probe.UPLOAD_MARKER_TEXT, "")
    return json.dumps(document)


def _captured_results(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every tool result of the captured document, in trajectory order."""
    return [result for step in document["steps"] for result in (step.get("observation") or {}).get("results") or []]


@pytest.mark.parametrize(
    "relative_path", ["tests/verifier/render_judge_transcript.py", "tests/verifier/render_harness_report.py"]
)
def test_the_checkers_executing_tool_set_is_the_one_both_verifier_renderers_read(relative_path: str) -> None:
    """The checker keeps its own copy so that computing a fact never executes a verifier template.
    A name added to one renderer and not here would silently drop that harness's command output from
    every fact read through it.
    """
    renderer = load_template_module(relative_path, "minds_evals_executing_tools_{}".format(Path(relative_path).stem))

    assert renderer.EXECUTING_TOOLS == behaviour_facts.EXECUTING_TOOLS
