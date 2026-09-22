import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.minds_evals.check_run import _format_criteria_cell
from imbue.minds_evals.check_run import check_job_directory
from imbue.minds_evals.check_run import collect_criterion_scores
from imbue.minds_evals.check_run import collect_dimension_scores
from imbue.minds_evals.check_run import collect_spend
from imbue.minds_evals.check_run import is_gates_dimension_passed
from imbue.minds_evals.check_run import load_trial_result
from imbue.minds_evals.check_run import render_summary_markdown
from imbue.minds_evals.check_run import total_reply_seconds
from imbue.minds_evals.check_run import write_run_check_reports
from imbue.minds_evals.ci_report import parse_run_check
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import CriterionKind
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import Spender
from imbue.minds_evals.data_types import TokenSnapshot
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.pricing import CacheWriteTtl
from imbue.minds_evals.reporting import AGENT_SPENDERS
from imbue.minds_evals.reporting import SPEND_LEGEND
from imbue.minds_evals.reporting import select_spend
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import FIXTURE_PRICE_MAP
from imbue.minds_evals.testing import GATES_CRITERION_NAMES
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import StepFixture
from imbue.minds_evals.testing import TRIAL_CONVERSATION_SECONDS
from imbue.minds_evals.testing import TRIAL_ELAPSED_SECONDS
from imbue.minds_evals.testing import TRIAL_REPLY_SECONDS
from imbue.minds_evals.testing import USAGE_DECIDER_COST_USD
from imbue.minds_evals.testing import USAGE_VERIFIER_COST_USD
from imbue.minds_evals.testing import USAGE_VERIFIER_MODEL
from imbue.minds_evals.testing import USAGE_WORKSPACE_COST_USD
from imbue.minds_evals.testing import USAGE_WORKSPACE_MODEL
from imbue.minds_evals.testing import USAGE_WORKSPACE_TOKENS
from imbue.minds_evals.testing import expected_modal_environment_name
from imbue.minds_evals.testing import read_scheduled_workflow_text
from imbue.minds_evals.testing import trial_usage_payload
from imbue.minds_evals.testing import usage_cost_usd
from imbue.minds_evals.testing import usage_tokens_costing
from imbue.minds_evals.testing import write_stepped_trial_dir
from imbue.minds_evals.testing import write_trial_dir


def test_check_job_directory_passes_a_run_whose_every_trial_completed_and_gated_open(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is True
    assert run_check.job_name == "nightly-run"
    assert [trial.trial_name for trial in run_check.trials] == ["greeting__bbbbbbb", "todo-app__aaaaaaa"]
    assert [trial.case_id for trial in run_check.trials] == ["greeting", "todo-app"]
    assert all(trial.reward == 0.75 for trial in run_check.trials)
    assert run_check.modal_environment_names == (
        expected_modal_environment_name("greeting__bbbbbbb"),
        expected_modal_environment_name("todo-app__aaaaaaa"),
    )


def test_check_job_directory_reports_every_criterion_without_gating_on_any_of_them(tmp_path: Path) -> None:
    """Every criterion of every dimension, whatever produced it, each on rewardkit's normalized
    scale and carrying the kind that says whether a model answered it. A report of the judges alone
    would leave the checks that moved a dimension out of the only place they are ever read."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", judge_raw_score=1.0)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    # A judge score at the very bottom of the scale is recorded and does not fail the run.
    assert run_check.is_passed is True
    (trial,) = run_check.trials
    assert [score.criterion for score in trial.criterion_scores if score.dimension == "gates"] == list(
        GATES_CRITERION_NAMES
    )
    assert [
        (score.dimension, score.criterion, score.kind, score.value)
        for score in trial.criterion_scores
        if score.dimension != "gates"
    ] == [
        ("harness_quality", "main_harness_success", CriterionKind.LLM, 0.5),
        ("harness_quality", "worker_reports_present", CriterionKind.PROGRAMMATIC, 1.0),
        ("outcome", "app_registered", CriterionKind.PROGRAMMATIC, 1.0),
        ("outcome", "http_expectations_met", CriterionKind.PROGRAMMATIC, 0.5),
        ("quality", "wordiness", CriterionKind.PROGRAMMATIC, 1.0),
        ("quality", "conciseness", CriterionKind.LLM, 0.0),
    ]
    # A flat trial ran no step, so nothing names one.
    assert {score.step for score in trial.criterion_scores} == {""}


def test_check_job_directory_reports_what_every_dimension_of_a_flat_trial_scored(tmp_path: Path) -> None:
    """The dimensions the criteria add up to, and the composed reward beside them: a criterion that
    moved between two runs says nothing on its own about the score the run was gated on."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert [(score.step, score.dimension, score.value) for score in trial.dimension_scores] == [
        ("", "gates", 1.0),
        ("", "outcome", 0.7),
        ("", "quality", 0.8),
        ("", "reward", 0.75),
    ]


def test_check_job_directory_reports_no_dimension_score_for_a_trial_harbor_never_graded(tmp_path: Path) -> None:
    """A trial harbor could not run is never graded, and a row of zeros for it would read as a trial
    that scored nothing rather than as one nothing scored."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.trials[0].dimension_scores == ()


def test_check_job_directory_reports_the_three_spans_a_trial_timed_itself_over(tmp_path: Path) -> None:
    """Three different questions, and the difference between them is where the time went: the whole
    trial holds the workspace bring-up and the evidence phase, the conversation holds what the client
    waited through, and the replies are the turns' own share of that."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert (trial.elapsed_seconds, trial.conversation_seconds) == (TRIAL_ELAPSED_SECONDS, TRIAL_CONVERSATION_SECONDS)
    assert trial.reply_seconds == pytest.approx(sum(TRIAL_REPLY_SECONDS))


def test_check_job_directory_reports_no_duration_a_trials_records_do_not_carry(tmp_path: Path) -> None:
    """A state file written before the driver timed a trial, and a trial that wrote no cost account
    at all. Neither took no time, so neither may read as zero."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", elapsed_seconds=None, conversation_seconds=None)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert (trial.elapsed_seconds, trial.conversation_seconds, trial.reply_seconds) == (None, None, None)


def test_check_job_directory_reports_no_reply_time_for_a_conversation_that_drew_no_reply(tmp_path: Path) -> None:
    """A turn earns a record only once its reply has arrived, so a trial whose conversation never got
    one records the turns it answered as none at all -- which is zero seconds of replies, not an
    unrecorded span."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload(reply_seconds=()))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.trials[0].reply_seconds == 0.0


def test_check_job_directory_fails_a_trial_whose_evidence_the_harness_could_not_measure(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", errored_entry_ids=("http_0_registered_apps_0",))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    failing = next(trial for trial in run_check.trials if trial.trial_name == "greeting__bbbbbbb")
    assert failing.error_entry_ids == ("http_0_registered_apps_0",)
    # The failure is the instrument, not the workspace: the trial ran and its gates held.
    assert failing.is_completed is True
    assert failing.is_gates_passed is True
    assert failing.is_passed is False


def test_check_job_directory_charges_a_trial_for_unmeasured_evidence_but_not_for_failed_evidence(
    tmp_path: Path,
) -> None:
    """The split the whole grading policy rests on: `failed` is the workspace falling short, which
    the judges already price, and `error` is the harness not finding out, which nothing else can
    catch. Widening the run gate to any non-passing status would collapse the two."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", failed_entry_ids=("http_0_registered_apps_0",))
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", errored_entry_ids=("http_0_root_0",))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    fell_short = next(trial for trial in run_check.trials if trial.trial_name == "todo-app__aaaaaaa")
    unmeasured = next(trial for trial in run_check.trials if trial.trial_name == "greeting__bbbbbbb")
    assert fell_short.error_entry_ids == ()
    assert fell_short.is_passed is True
    assert unmeasured.error_entry_ids == ("http_0_root_0",)
    assert unmeasured.is_passed is False


def test_check_job_directory_fails_a_trial_whose_structural_gates_did_not_hold(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", failed_gate_names=("all_turns_completed",))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_gates_passed is False
    assert run_check.trials[0].error_entry_ids == ()


def test_check_job_directory_fails_a_trial_harbor_recorded_an_exception_for(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "DaemonError" in run_check.trials[0].incompletion_reason
    # harbor never grades a trial it could not run, so there is no verifier output to read: the
    # gates are not passed because nothing scored them.
    assert run_check.trials[0].is_gates_passed is False


def test_check_job_directory_fails_a_trial_whose_step_raised(tmp_path: Path) -> None:
    """harbor records a per-step failure on the step alone and leaves the trial-level exception_info
    unset, so a gate that read only the trial level would call this a completed trial."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", step_exception_type="SandboxTimeout")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "step agent raised SandboxTimeout" in run_check.trials[0].incompletion_reason


def test_check_job_directory_fails_a_trial_harbor_never_wrote_a_result_for(tmp_path: Path) -> None:
    """The shape a trial killed on Modal leaves: harbor writes result.json last, so it got no chance
    to record anything. The gate must charge the trial rather than skip over the file it cannot
    find, which is the one reading that would let a dead trial pass."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", is_result_written=False)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "no result.json" in run_check.trials[0].incompletion_reason
    assert run_check.trials[0].reward is None


def test_check_job_directory_fails_a_trial_that_never_wrote_its_state(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", is_state_written=False)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "state.json" in run_check.trials[0].incompletion_reason
    # Nothing to clean up under that name, so the run's environment list must not carry an empty one.
    assert run_check.modal_environment_names == ()


def test_check_job_directory_fails_a_trial_whose_conversation_timed_out(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", test_state="timed_out")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert "timed_out" in run_check.trials[0].incompletion_reason


def test_check_job_directory_passes_an_oracle_trial_that_recorded_no_evidence(tmp_path: Path) -> None:
    """A bare oracle case fabricates no evidence bundle at all, which is not the harness failing to
    measure -- there was nothing there to measure."""
    job_dir = tmp_path / "oracle-run"
    write_trial_dir(job_dir, "greeting__ccccccc", case_id="greeting", is_manifest_written=False)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is True
    assert run_check.trials[0].error_entry_ids == ()


def test_check_job_directory_says_so_when_a_manifest_carries_no_readable_entries(
    tmp_path: Path, captured_log_messages: list[str]
) -> None:
    """Read without a schema on purpose, because an older driver's manifest must stay readable. The
    silent reading of one that is not, though, is "nothing went unmeasured" -- the one verdict this
    gate exists to deny -- so it has to be said out loud."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / "todo-app__aaaaaaa" / "agent" / "verification" / "manifest.json").write_text('{"entries": null}')

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.trials[0].error_entry_ids == ()
    assert any("no readable 'entries' list" in message for message in captured_log_messages)


def test_check_job_directory_names_an_errored_evidence_entry_that_carries_no_id(tmp_path: Path) -> None:
    """The manifest is read without a schema so an older driver's stays readable, which is also how
    an entry with no id of its own can arrive. Both reports join these ids and render an empty join
    as "none", so an empty id would print a trial that failed for unmeasured evidence as one with
    none -- and a lone empty id would let it pass outright."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / "todo-app__aaaaaaa" / "agent" / "verification" / "manifest.json").write_text(
        json.dumps({"entries": [{"check_class": "http", "status": CheckStatus.ERROR.value}]})
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert all(entry_id for entry_id in run_check.trials[0].error_entry_ids)
    assert read_row_cell(render_summary_markdown(run_check), "todo-app__aaaaaaa", "errored evidence") != "none"


# The steps of a stepped case, named so that the order they ran in is not the order their directories
# sort in: harbor runs them in the task's declared order, which only its own record of the trial says,
# and a reader that sorted the step directories would call `amend` the first step and `triage` the
# last.
_STEP_NAMES: Final[tuple[str, str, str]] = ("triage", "build", "amend")


def _finished_steps(*, last_usage: Mapping[str, Any] | None = None) -> tuple[StepFixture, ...]:
    """Three steps that each ran their conversation to the end, the last one carrying the trial's
    cost account."""
    return tuple(StepFixture(name=name, usage=last_usage if name == _STEP_NAMES[-1] else None) for name in _STEP_NAMES)


def test_check_job_directory_passes_a_stepped_trial_that_finished_every_step(tmp_path: Path) -> None:
    """A stepped trial's artifacts are under its steps, so the gate reads nothing at the trial root
    but harbor's own result. Read only there, this trial is one that never wrote a state file at
    all."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(job_dir, "todo-app__aaaaaaa", _finished_steps())

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is True
    assert (trial.is_completed, trial.is_gates_passed) == (True, True)
    assert (trial.step_count, trial.completed_step_count) == (3, 3)
    assert trial.case_id == "todo-app"
    assert trial.modal_environment_name == expected_modal_environment_name("todo-app__aaaaaaa")
    # The step count rides in the completion cell, so one row says how many conversations it covers.
    assert read_row_cell(render_summary_markdown(run_check), "todo-app__aaaaaaa", "completed") == "pass (3 steps)"


def _stepped_usage(*, workspace_cost_usd: float, verifier_cost_usd: float = USAGE_VERIFIER_COST_USD) -> dict[str, Any]:
    """A cost account whose spenders are told apart by what they price to, for the fixtures that are
    about which step's account is read rather than about the arithmetic."""
    return trial_usage_payload(
        workspace_tokens=usage_tokens_costing(workspace_cost_usd),
        verifier_tokens=usage_tokens_costing(verifier_cost_usd, model=USAGE_VERIFIER_MODEL),
    )


def test_check_job_directory_reads_a_stepped_trials_spend_from_its_last_step(tmp_path: Path) -> None:
    """The cost account accumulates across the steps -- one chat and one proxy serve every step -- so
    the trial's spend is the last step's copy of it, the verification agent's trial-wide total among
    it. An earlier step's copy is the same account partway through, and reporting it would under-count
    the trial."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", usage=_stepped_usage(workspace_cost_usd=0.40, verifier_cost_usd=0.10)),
            StepFixture(name="build", usage=_stepped_usage(workspace_cost_usd=1.10, verifier_cost_usd=0.20)),
            StepFixture(name="amend", usage=_stepped_usage(workspace_cost_usd=1.80, verifier_cost_usd=0.30)),
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert [(entry.spender, entry.cost_usd) for entry in run_check.trials[0].spend] == [
        (Spender.WORKSPACE_AGENT, pytest.approx(1.80)),
        (Spender.DECIDER, pytest.approx(USAGE_DECIDER_COST_USD)),
        (Spender.VERIFIER_AGENT, pytest.approx(0.30)),
    ]
    assert read_spend_cells(render_summary_markdown(run_check), "todo-app__aaaaaaa") == ("$1.80", "$0.55")


def test_check_job_directory_falls_back_to_the_last_step_that_wrote_a_cost_account(tmp_path: Path) -> None:
    """A step that wrote no account at all is one harbor recorded an exception for, and the gate
    charges the trial for that exception on its own. The spend the trial did record is still worth
    reporting, so the last account there is read rather than none."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", usage=_stepped_usage(workspace_cost_usd=0.40)),
            StepFixture(name="build", usage=_stepped_usage(workspace_cost_usd=1.10)),
            StepFixture(name="amend", exception_type="SandboxTimeout"),
        ),
    )

    (trial,) = check_job_directory(job_dir, FIXTURE_PRICE_MAP).trials

    assert trial.is_completed is False
    assert [entry.cost_usd for entry in trial.spend] == [
        pytest.approx(1.10),
        pytest.approx(USAGE_DECIDER_COST_USD),
        pytest.approx(USAGE_VERIFIER_COST_USD),
    ]


def test_check_job_directory_reads_a_stepped_trials_state_from_the_last_step_that_wrote_one(tmp_path: Path) -> None:
    """Each step rewrites the whole conversation's state, so the trial ended however the last step
    did. Taking an earlier step's copy would report a trial that ran out of time on its final
    conversation as one that finished."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (StepFixture(name="triage"), StepFixture(name="build"), StepFixture(name="amend", test_state="timed_out")),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert "timed_out" in run_check.trials[0].incompletion_reason


def test_check_job_directory_fails_a_stepped_trial_whose_step_raised(tmp_path: Path) -> None:
    """Harbor records a step's failure on the step alone and abandons the steps after it, so the row
    has to name the step that died rather than report a trial that merely ran fewer steps."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (StepFixture(name="triage"), StepFixture(name="build", exception_type="SandboxTimeout")),
        declared_step_count=3,
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is False
    assert trial.is_completed is False
    assert "step build raised SandboxTimeout" in trial.incompletion_reason
    # A step harbor could not run is never graded, so nothing scored that step's gates.
    assert trial.is_gates_passed is False
    assert (trial.step_count, trial.completed_step_count) == (3, 2)


def test_check_job_directory_fails_a_stepped_trial_that_stopped_short_of_its_declared_steps(tmp_path: Path) -> None:
    """The failure nothing else in a job directory can see. Harbor stops iterating the steps the
    moment one misses its reward floor and records no exception for it: that step ran its conversation
    to the end, so its state file -- the last one there is -- reads exactly like the final step of a
    trial that ran them all. Only the count of steps the task declared tells the two apart, which is
    why the driver records it."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir, "todo-app__aaaaaaa", (StepFixture(name="triage"), StepFixture(name="build")), declared_step_count=3
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is False
    assert trial.is_completed is False
    assert "only 2 of the task's 3 steps ran" in trial.incompletion_reason
    # Every step it did run was graded and held, which is exactly why the shortfall has to be read.
    assert trial.is_gates_passed is True


def test_check_job_directory_passes_a_stepped_trial_whose_record_does_not_say_how_many_steps_it_declared(
    tmp_path: Path,
) -> None:
    """A state file written before the declared count was recorded says nothing about steps the trial
    did not run, and silence is not evidence of a trial that stopped early."""
    job_dir = tmp_path / "nightly-run"
    trial_dir = write_stepped_trial_dir(job_dir, "todo-app__aaaaaaa", _finished_steps())
    state_path = trial_dir / "steps" / "amend" / "agent" / "state.json"
    state = json.loads(state_path.read_text())
    state.pop("step_count")
    state_path.write_text(json.dumps(state))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is True
    assert (run_check.trials[0].step_count, run_check.trials[0].completed_step_count) == (0, 3)


def test_check_job_directory_charges_a_stepped_trial_for_evidence_any_step_left_unmeasured(tmp_path: Path) -> None:
    """Evidence is collected fresh per step, against that step's own expectations, so a step that
    went unmeasured is a step the trial cannot be judged on however clean the steps after it are. The
    id carries its step, because the report is what someone goes looking with."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", errored_entry_ids=("http_0_registered_apps_0",)),
            StepFixture(name="build"),
            StepFixture(name="amend"),
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is False
    assert trial.error_entry_ids == ("triage/http_0_registered_apps_0",)
    assert (trial.is_completed, trial.is_gates_passed) == (True, True)


def test_check_job_directory_fails_a_stepped_trial_whose_gates_held_on_every_step_but_one(tmp_path: Path) -> None:
    """Every step is graded by its own verifier, and a gate that failed on any of them is a structural
    failure of the trial: that step's conversation did not run as the case configured it, whatever a
    later step went on to score."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", failed_gate_names=("all_turns_completed",)),
            StepFixture(name="build"),
            StepFixture(name="amend"),
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_gates_passed is False
    assert run_check.trials[0].is_completed is True


def test_check_job_directory_names_the_step_each_criterion_was_scored_on(tmp_path: Path) -> None:
    """A stepped case scores the same criterion once per step against that step's own expectations.
    Unqualified, the record would hold one criterion three times over with nothing saying which
    conversation an answer was about, and every reader that keys a column on the dimension and the
    criterion would collapse the steps into one column."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", judge_raw_score=7.0),
            StepFixture(name="build", judge_raw_score=8.0),
            StepFixture(name="amend", judge_raw_score=10.0),
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert [(score.step, score.value) for score in trial.criterion_scores if score.criterion == "conciseness"] == [
        ("triage", pytest.approx(6 / 9)),
        ("build", pytest.approx(7 / 9)),
        ("amend", 1.0),
    ]
    criteria_cell = read_row_cell(render_summary_markdown(run_check), "todo-app__aaaaaaa", "criteria")
    assert "triage/quality: wordiness 1.00, conciseness 0.67; " in criteria_cell
    assert criteria_cell.endswith("amend/quality: wordiness 1.00, conciseness 1.00")


def test_check_job_directory_reports_what_every_step_of_a_stepped_trial_scored(tmp_path: Path) -> None:
    """Harbor's own trial-level result is the one step its reward strategy selected, so reporting it
    alone would say nothing about the steps it did not select -- including the step a trial stopped
    at, which is the step a reader goes looking for."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (
            StepFixture(name="triage", rewards={"gates": 1.0, "reward": 0.4}),
            StepFixture(name="build", rewards={"gates": 1.0, "reward": 0.9}),
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert [(score.step, score.dimension, score.value) for score in trial.dimension_scores] == [
        ("triage", "gates", 1.0),
        ("triage", "reward", 0.4),
        ("build", "gates", 1.0),
        ("build", "reward", 0.9),
    ]
    assert read_row_cell(render_summary_markdown(run_check), "todo-app__aaaaaaa", "dimensions") == (
        "triage/gates 1.00, triage/reward 0.40, build/gates 1.00, build/reward 0.90"
    )


def test_check_job_directory_reports_nothing_for_a_step_that_was_never_graded(tmp_path: Path) -> None:
    """A step harbor recorded an exception for was never graded, and it is what stops the steps after
    it: the steps that did score still report theirs."""
    job_dir = tmp_path / "nightly-run"
    write_stepped_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        (StepFixture(name="triage"), StepFixture(name="build", exception_type="SandboxTimeout")),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert {score.step for score in trial.dimension_scores} == {"triage"}


@pytest.mark.parametrize("is_step_recorded", [True, False])
def test_check_job_directory_fails_a_trial_whose_steps_directory_holds_no_step_at_all(
    tmp_path: Path, is_step_recorded: bool
) -> None:
    """The shape a trial killed between creating its steps directory and finishing its first step
    leaves, with and without harbor's record of which step that was. There is no state file anywhere
    under it, which is the reading of an absent one: a trial that never got past setup."""
    job_dir = tmp_path / "nightly-run"
    trial_dir = write_stepped_trial_dir(job_dir, "todo-app__aaaaaaa", (StepFixture(name="triage"),))
    shutil.rmtree(trial_dir / "steps" / "triage")
    if not is_step_recorded:
        result = json.loads((trial_dir / "result.json").read_text())
        result["step_results"] = []
        (trial_dir / "result.json").write_text(json.dumps(result))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is False
    assert "no state.json" in trial.incompletion_reason
    assert (trial.spend, trial.error_entry_ids) == ((), ())


def test_check_job_directory_reads_a_stepped_trial_harbor_never_recorded_a_result_for(tmp_path: Path) -> None:
    """harbor writes result.json last, so a trial killed on Modal has none -- and with it goes the
    only record of the order its steps ran in. The trial is charged for the missing result, as a flat
    one is, and its cost account and the environment it leaked are still recovered from the steps on
    disk."""
    job_dir = tmp_path / "nightly-run"
    trial_dir = write_stepped_trial_dir(
        job_dir, "todo-app__aaaaaaa", _finished_steps(last_usage=_stepped_usage(workspace_cost_usd=1.80))
    )
    (trial_dir / "result.json").unlink()

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert trial.is_completed is False
    assert "no result.json" in trial.incompletion_reason
    assert run_check.modal_environment_names == (expected_modal_environment_name("todo-app__aaaaaaa"),)
    # Directory order puts `amend` first, and that is the only step that wrote a cost account, so a
    # figure at all says the fallback found the steps.
    assert [entry.cost_usd for entry in trial.spend] == [
        pytest.approx(1.80),
        pytest.approx(USAGE_DECIDER_COST_USD),
        pytest.approx(USAGE_VERIFIER_COST_USD),
    ]


def _harness_config_state(
    *,
    model: str,
    is_model_confirmed: bool | None,
    observed_models: tuple[str, ...] = (),
    lane: str = "anthropic",
    harness: str = "claude",
    welcome_model: str = "claude-opus-4-8",
) -> dict[str, Any]:
    """The harness-config block of the arm the driver records, as a trial that ran on the anthropic
    lane leaves it. Name a lane and its harness to describe a trial that ran on another."""
    return {
        "lane": lane,
        "harness": harness,
        "model": model,
        "effort": "medium" if model else "",
        "fast": False,
        "model_choice_switch": "applied" if model else "skipped",
        "observed_models": list(observed_models),
        "welcome_model": welcome_model,
        "is_model_confirmed": is_model_confirmed,
    }


def test_check_job_directory_reads_the_harness_config_out_of_the_arm_block(tmp_path: Path) -> None:
    """The harness settings are one half of a trial's arm and are nested under it, the pinned pair
    being the other half -- which the block repeats, so it describes a treatment on its own."""
    job_dir = tmp_path / "nightly-run"
    trial_dir = write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        harness_config=_harness_config_state(
            model="haiku", is_model_confirmed=True, observed_models=("claude-haiku-4-5-20251001",)
        ),
    )

    (trial,) = check_job_directory(job_dir, FIXTURE_PRICE_MAP).trials

    assert (trial.lane, trial.requested_model, trial.is_model_confirmed) == ("anthropic", "haiku", True)
    state = json.loads((trial_dir / "agent" / "state.json").read_text())
    assert (state["arm"]["mngr_sha"], state["arm"]["dwt_sha"]) == (state["mngr_sha"], state["dwt_sha"])
    assert (trial.mngr_sha, trial.dwt_sha) == (state["arm"]["mngr_sha"], state["arm"]["dwt_sha"])


def test_check_job_directory_fails_a_trial_that_answered_on_another_model_than_it_asked_for(
    tmp_path: Path,
) -> None:
    """The one thing a harness config's record is kept for: a model choice that did not take, or a
    greeting renamed out from under the reader that splits the two, has to break the run rather than
    be reported alongside a green verdict."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        harness_config=_harness_config_state(
            model="haiku", is_model_confirmed=False, observed_models=("claude-opus-5-20260401",)
        ),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (trial,) = run_check.trials
    assert run_check.is_passed is False
    assert trial.is_passed is False
    # Nothing else about the trial went wrong: it ran to the end and its gates held.
    assert (trial.is_completed, trial.is_gates_passed) == (True, True)
    assert "haiku" in trial.wrong_model_reason
    assert "claude-opus-5-20260401" in trial.wrong_model_reason


@pytest.mark.parametrize(
    "harness_config",
    [
        pytest.param(
            _harness_config_state(model="haiku", is_model_confirmed=None), id="a model the trial could not confirm"
        ),
        pytest.param(_harness_config_state(model="", is_model_confirmed=None), id="a config that asked for no model"),
        pytest.param(None, id="a trial that recorded no arm at all"),
    ],
)
def test_check_job_directory_charges_a_trial_only_for_a_model_it_observably_ran_on(
    tmp_path: Path, harness_config: dict[str, Any] | None
) -> None:
    """Null is what the driver writes wherever it cannot tell -- no transcript was captured, or the
    catalog id has no known reported name -- and a config that named no model has nothing to confirm.
    Charging either would fail runs for silence."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", harness_config=harness_config)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert run_check.is_passed is True
    assert run_check.trials[0].wrong_model_reason == ""


@pytest.mark.parametrize(
    ("harness_config", "expected_cell"),
    [
        pytest.param(
            _harness_config_state(
                model="haiku", is_model_confirmed=True, observed_models=("claude-haiku-4-5-20251001",)
            ),
            "anthropic haiku confirmed",
            id="a model the transcript confirmed",
        ),
        pytest.param(
            _harness_config_state(model="haiku", is_model_confirmed=None),
            "anthropic haiku unconfirmed",
            id="one it could not",
        ),
        pytest.param(
            _harness_config_state(
                model="gpt-5.6-sol",
                is_model_confirmed=None,
                lane="openai",
                harness="codex",
                welcome_model="",
            ),
            "openai gpt-5.6-sol not observable",
            id="one whose lane names no model to confirm",
        ),
        pytest.param(
            _harness_config_state(model="", is_model_confirmed=None),
            "anthropic default",
            id="the config that asks for nothing",
        ),
        pytest.param(
            _harness_config_state(
                model="haiku", is_model_confirmed=False, observed_models=("claude-opus-5-20260401",)
            ),
            "the run asked for haiku but the trial answered on claude-opus-5-20260401",
            id="a model it ran on instead",
        ),
        pytest.param(None, "-", id="no arm at all"),
    ],
)
def test_render_summary_markdown_says_which_harness_config_each_trial_ran(
    tmp_path: Path, harness_config: dict[str, Any] | None, expected_cell: str
) -> None:
    """The summary is what a scheduled run is read by, so the harness half of the arm has to be
    legible there rather than only in the JSON beside it."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", harness_config=harness_config)

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_row_cell(summary, "todo-app__aaaaaaa", "arm") == expected_cell


def test_check_job_directory_ignores_the_cache_harbor_leaves_after_a_regrade(tmp_path: Path) -> None:
    """`harbor trial regrade` on a hub trial id caches its download under <job dir>/.sources/<uuid>.
    Reading that as a trial would fail the run over an artifact of having regraded it."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / ".sources" / "9f1c0b3e").mkdir(parents=True)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert [trial.trial_name for trial in run_check.trials] == ["todo-app__aaaaaaa"]
    assert run_check.is_passed is True


def test_check_job_directory_refuses_a_job_directory_that_is_not_there(tmp_path: Path) -> None:
    """A mistyped path is not an empty run. Left to iterdir it would raise a FileNotFoundError from
    the middle of the gate rather than saying which directory the caller named."""
    with pytest.raises(JobReadError, match="not a job directory"):
        check_job_directory(tmp_path / "never-created", FIXTURE_PRICE_MAP)


def test_check_job_directory_refuses_a_directory_with_no_trials(tmp_path: Path) -> None:
    empty_job_dir = tmp_path / "nothing-ran"
    empty_job_dir.mkdir()

    with pytest.raises(JobReadError, match="no trial directories"):
        check_job_directory(empty_job_dir, FIXTURE_PRICE_MAP)


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        # Not JSON at all.
        pytest.param(b"{ not json", "not valid JSON", id="unparseable"),
        # A file truncated mid-write can end on a partial multi-byte sequence, which raises a
        # UnicodeDecodeError -- a ValueError, not an OSError -- unless it is caught where it is read.
        pytest.param(b'{"trial_name": "\xf0\x9f', "cannot read", id="undecodable"),
        # Valid JSON, wrong shape. Every reader downstream calls .get on this, so it has to be
        # refused where it is loaded rather than raising an AttributeError somewhere further in.
        pytest.param(b"[]", "is a list, not a JSON object", id="not-an-object"),
        # A JSON object, but not one harbor wrote.
        pytest.param(
            json.dumps({"trial_name": "todo-app__aaaaaaa"}).encode(),
            "not a harbor trial result",
            id="not-a-trial-result",
        ),
    ],
)
def test_check_job_directory_refuses_a_result_file_it_cannot_read(
    tmp_path: Path, payload: bytes, expected_message: str
) -> None:
    """A result.json truncated by the very crash being diagnosed is a job that cannot be read, which
    is a different claim from a run that failed -- so every unreadable shape of it has to arrive as a
    JobReadError rather than as whatever the reader that met it happened to raise."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / "todo-app__aaaaaaa" / "result.json").write_bytes(payload)

    with pytest.raises(JobReadError, match=expected_message):
        check_job_directory(job_dir, FIXTURE_PRICE_MAP)


def test_check_job_directory_refuses_a_trial_state_it_cannot_read(tmp_path: Path) -> None:
    """The strict half of an asymmetry the branch rests on: cleanup meets the same truncated
    state.json and skips that trial with a warning, because refusing it would leak every other
    trial's environment. The gate must do the opposite -- a trial whose state cannot be read is one
    nothing can say completed, and an unjudged run must never come out as a pass."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / "todo-app__aaaaaaa" / "agent" / "state.json").write_text('{"case_name": "todo-a')

    with pytest.raises(JobReadError, match="not valid JSON"):
        check_job_directory(job_dir, FIXTURE_PRICE_MAP)


def test_render_summary_markdown_puts_every_trial_and_the_verdict_in_the_table(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", failed_gate_names=("not_timed_out",))

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert summary.startswith("## minds-evals: nightly-run -- FAIL")
    assert "| todo-app__aaaaaaa | todo-app |" in summary
    assert expected_modal_environment_name("greeting__bbbbbbb") in summary
    assert "conciseness 0.78" in summary
    # One header row, one separator, and one row per trial.
    assert len([line for line in summary.splitlines() if line.startswith("|")]) == 4


def test_render_summary_markdown_renders_a_trial_that_never_got_graded(tmp_path: Path) -> None:
    """The row a red nightly is actually read for: harbor recorded an exception, so the trial has no
    verifier result. The reward cell has to degrade to a dash rather than to a formatting error on
    None, and the row still has to say what went wrong."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert "DaemonError" in read_row_cell(summary, "todo-app__aaaaaaa", "completed")
    assert read_row_cell(summary, "todo-app__aaaaaaa", "reward") == "-"


@pytest.mark.parametrize("is_markdown_wanted", [True, False])
def test_write_run_check_reports_writes_only_what_was_asked_for(tmp_path: Path, is_markdown_wanted: bool) -> None:
    """The scheduled job asks for both, but each is optional and the other must not appear."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    summary_md = tmp_path / "out" / "summary.md"
    summary_json = tmp_path / "out" / "summary.json"

    write_run_check_reports(
        check_job_directory(job_dir, FIXTURE_PRICE_MAP),
        summary_md if is_markdown_wanted else None,
        None if is_markdown_wanted else summary_json,
    )

    assert summary_md.exists() is is_markdown_wanted
    assert summary_json.exists() is not is_markdown_wanted
    if not is_markdown_wanted:
        assert json.loads(summary_json.read_text())["is_passed"] is True


def test_render_summary_markdown_keeps_a_pipe_in_an_exception_message_inside_its_cell(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="Daemon|Error")

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    trial_row = next(line for line in summary.splitlines() if line.startswith("| todo-app__aaaaaaa"))
    assert "Daemon\\|Error" in trial_row
    # One unescaped pipe per column boundary and no more: a pipe left unescaped would add a column.
    assert trial_row.count("|") - trial_row.count("\\|") == 16


def test_render_summary_markdown_keeps_every_free_text_cell_inside_its_column(tmp_path: Path) -> None:
    """Exception messages are not the only free text in the row: case ids come from the eval config
    and entry ids from the evidence manifest, and neither is held to a vocabulary. One unescaped pipe
    shifts every cell after it into the wrong column, so the table misreports the run it is read for."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", case_id="todo|app", errored_entry_ids=("http|0",))

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    trial_row = next(line for line in summary.splitlines() if line.startswith("| todo-app__aaaaaaa"))
    assert "todo\\|app" in trial_row
    assert "http\\|0" in trial_row
    assert trial_row.count("|") - trial_row.count("\\|") == 16


def _row_cells(row: str) -> list[str]:
    return row.removeprefix("| ").removesuffix(" |").split(" | ")


def read_row_cell(summary: str, trial_name: str, heading: str) -> str:
    """One cell of one trial's row, addressed by the heading above it.

    The table grows columns, and an index into the row pins nothing: it would go on holding while
    reading whatever ended up beside the column the test is about. Split on the separator with its
    spaces, which an escaped pipe inside a cell does not carry.
    """
    lines = summary.splitlines()
    headings = _row_cells(next(line for line in lines if line.startswith("| trial |")))
    cells = _row_cells(next(line for line in lines if line.startswith("| {} ".format(trial_name))))
    return cells[headings.index(heading)]


def read_spend_cells(summary: str, trial_name: str) -> tuple[str, str]:
    """One trial's two cost cells."""
    return (read_row_cell(summary, trial_name, "agent cost"), read_row_cell(summary, trial_name, "harness cost"))


def test_render_summary_markdown_gives_a_row_every_scoring_input_and_the_time_it_took(tmp_path: Path) -> None:
    """The whole of what a reward was composed from, in two cells on the scale rewardkit scored them
    on, and the trial's three spans in a third."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_row_cell(summary, "todo-app__aaaaaaa", "dimensions") == (
        "gates 1.00, outcome 0.70, quality 0.80, reward 0.75"
    )
    assert read_row_cell(summary, "todo-app__aaaaaaa", "criteria") == (
        "gates: transcript_has_agent_reply 1.00, agent_engaged_substantively 1.00, all_turns_completed 1.00,"
        " not_timed_out 1.00; harness_quality: main_harness_success 0.50, worker_reports_present 1.00;"
        " outcome: app_registered 1.00, http_expectations_met 0.50; quality: wordiness 1.00, conciseness 0.78"
    )
    assert read_row_cell(summary, "todo-app__aaaaaaa", "time") == "elapsed 612s / conversation 545s / replies 320s"


def test_render_summary_markdown_says_which_of_a_trials_spans_went_unrecorded(tmp_path: Path) -> None:
    """A trial that wrote no cost account timed no replies, and one whose every span is unrecorded
    has nothing to print at all -- neither is a trial that took no time."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", elapsed_seconds=None, conversation_seconds=None)

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_row_cell(summary, "todo-app__aaaaaaa", "time") == "elapsed 612s / conversation 545s / replies -"
    assert read_row_cell(summary, "greeting__bbbbbbb", "time") == "-"


def test_render_summary_markdown_says_when_a_trial_was_scored_on_nothing(tmp_path: Path) -> None:
    """A trial harbor never graded has no dimension and no criterion to report, and a dash is how
    both cells say so rather than reading as a trial that scored zero."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_row_cell(summary, "todo-app__aaaaaaa", "dimensions") == "-"
    assert read_row_cell(summary, "todo-app__aaaaaaa", "criteria") == "-"


def test_check_job_directory_reports_every_scoring_input_through_the_json_summary(tmp_path: Path) -> None:
    """The JSON summary is what the Slack report reads a pass back out of, so every field the report
    is built from has to survive the round trip through it."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())
    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    parsed = parse_run_check(run_check.model_dump_json())

    (parsed_trial,) = parsed.trials
    (trial,) = run_check.trials
    assert parsed_trial.criterion_scores == trial.criterion_scores
    assert parsed_trial.dimension_scores == trial.dimension_scores
    assert (parsed_trial.elapsed_seconds, parsed_trial.conversation_seconds, parsed_trial.reply_seconds) == (
        TRIAL_ELAPSED_SECONDS,
        TRIAL_CONVERSATION_SECONDS,
        pytest.approx(sum(TRIAL_REPLY_SECONDS)),
    )


def test_check_job_directory_reports_no_spend_for_a_trial_that_wrote_no_usage_account(tmp_path: Path) -> None:
    """An oracle trial runs no models of its own, and a trial written before usage.json carried a
    block has none either. Neither spent nothing -- there is simply nothing recorded -- so the
    columns have to say that rather than print a zero, and the run has no total to announce."""
    job_dir = tmp_path / "oracle-run"
    write_trial_dir(job_dir, "greeting__ccccccc", case_id="greeting")

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
    summary = render_summary_markdown(run_check)

    assert run_check.trials[0].spend == ()
    assert read_spend_cells(summary, "greeting__ccccccc") == ("-", "-")
    assert "agent spend" not in summary
    assert SPEND_LEGEND not in summary


def test_check_job_directory_records_every_spender_usage_json_accounts_for(tmp_path: Path) -> None:
    """One record per spender, each keeping the flags it was recorded with, and in the order the
    enum names them however the file happens to be keyed -- the columns a run is compared on cannot
    be ordered by whichever driver version wrote the trial. The harness's two spenders are direct
    API calls on one model at one rate, so nothing about them can be partial; only the workspace
    agent's figure carries that question."""
    job_dir = tmp_path / "nightly-run"
    payload = trial_usage_payload(workspace_tokens=usage_tokens_costing(22.48), is_cost_complete=False)
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        usage={key: payload[key] for key in reversed(list(payload))},
    )

    spend = check_job_directory(job_dir, FIXTURE_PRICE_MAP).trials[0].spend

    assert [entry.spender for entry in spend] == [Spender.WORKSPACE_AGENT, Spender.DECIDER, Spender.VERIFIER_AGENT]
    assert [entry.cost_usd for entry in spend] == [
        pytest.approx(22.48),
        pytest.approx(USAGE_DECIDER_COST_USD),
        pytest.approx(USAGE_VERIFIER_COST_USD),
    ]
    assert [entry.is_complete for entry in spend] == [False, True, True]
    assert [entry.is_rate_certain for entry in spend] == [True, True, True]


def test_collect_spend_prices_the_fixtures_tokens() -> None:
    """What the round figures every assertion in this file names are worth in tokens, in one place.
    The arithmetic behind all of them: four buckets at the fixture map's Opus rates for the agent, and
    input plus output at its Haiku rates for the two harness callers. Change a fixture rate or a
    fixture token count and this is the one test that says so."""
    spend = collect_spend(trial_usage_payload(), FIXTURE_PRICE_MAP, CacheWriteTtl.FIVE_MINUTES)

    assert [entry.cost_usd for entry in spend] == [
        pytest.approx(USAGE_WORKSPACE_COST_USD),
        pytest.approx(USAGE_DECIDER_COST_USD),
        pytest.approx(USAGE_VERIFIER_COST_USD),
    ]


def test_collect_spend_names_a_model_nothing_can_price() -> None:
    """A trial whose model is not in the map prices nothing at all rather than the part of it that
    could be priced, and names the model: that says to go and price the model, not to go looking for a
    broken trial. Every pi arm on OpenRouter reads this way until its greeting model is carried."""
    spend = collect_spend(
        trial_usage_payload(workspace_model="kimi-k2.6", verifier_model="glm-4.7-flash"),
        FIXTURE_PRICE_MAP,
        CacheWriteTtl.FIVE_MINUTES,
    )

    assert [(entry.spender, entry.cost_usd, entry.unpriced_models) for entry in spend] == [
        (Spender.WORKSPACE_AGENT, None, ("kimi-k2.6",)),
        (Spender.DECIDER, pytest.approx(USAGE_DECIDER_COST_USD), ()),
        (Spender.VERIFIER_AGENT, None, ("glm-4.7-flash",)),
    ]


def test_collect_spend_prices_a_gateway_arm_under_the_model_it_asked_for() -> None:
    """A harness reports a model without the gateway that billed it: an arm that asked for
    `openrouter/openai/gpt-5-mini` reports `openai/gpt-5-mini`, a key no price map holds while it does
    hold the tag. The trial records the tag it asked for, so such an arm is priced at the
    gateway's own rate -- and a row that did not come from the tag is still unpriced."""
    payload = trial_usage_payload(workspace_model="openai/gpt-5-mini")

    asked = collect_spend(payload, FIXTURE_PRICE_MAP, CacheWriteTtl.FIVE_MINUTES, "openrouter/openai/gpt-5-mini")
    unasked = collect_spend(payload, FIXTURE_PRICE_MAP, CacheWriteTtl.FIVE_MINUTES)

    assert asked[0].cost_usd == pytest.approx(
        usage_cost_usd(
            "openrouter/openai/gpt-5-mini", USAGE_WORKSPACE_TOKENS, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES
        )
    )
    assert (unasked[0].cost_usd, unasked[0].unpriced_models) == (None, ("openai/gpt-5-mini",))


def test_check_job_directory_prices_a_gateway_arm_from_the_model_its_state_file_names(tmp_path: Path) -> None:
    """The catalog id that prices such a row comes out of the trial's own arm record, so a written
    trial is what pins that it reaches the pricing at all."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        harness_config=_harness_config_state(model="openrouter/openai/gpt-5-mini", is_model_confirmed=True),
        usage=trial_usage_payload(workspace_model="openai/gpt-5-mini"),
    )

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (workspace_spend,) = select_spend(run_check.trials[0].spend, AGENT_SPENDERS)
    assert workspace_spend.cost_usd == pytest.approx(
        usage_cost_usd("openrouter/openai/gpt-5-mini", USAGE_WORKSPACE_TOKENS, cache_write_ttl=CacheWriteTtl.ONE_HOUR)
    )


def test_check_job_directory_takes_each_trials_cache_write_rate_from_its_own_harness(tmp_path: Path) -> None:
    """Claude Code asks for the 1-hour prompt cache, which Anthropic bills at 2x an input token
    against the 1.25x a 5-minute write costs; every other harness is priced at the base rate. Which of
    the two a trial gets is read off its own arm record, so two trials of one run can be priced apart
    -- and pricing them all alike would understate a claude arm's biggest bucket by 37.5%."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        harness_config=_harness_config_state(model="", is_model_confirmed=None),
        usage=trial_usage_payload(),
    )
    write_trial_dir(
        job_dir,
        "todo-app__bbbbbbb",
        harness_config=_harness_config_state(
            model="", is_model_confirmed=None, lane="openrouter", harness="pi-coding"
        ),
        usage=trial_usage_payload(),
    )

    claude_trial, pi_trial = check_job_directory(job_dir, FIXTURE_PRICE_MAP).trials

    (claude_spend,) = select_spend(claude_trial.spend, AGENT_SPENDERS)
    (pi_spend,) = select_spend(pi_trial.spend, AGENT_SPENDERS)
    assert claude_spend.cost_usd == pytest.approx(
        usage_cost_usd(USAGE_WORKSPACE_MODEL, USAGE_WORKSPACE_TOKENS, cache_write_ttl=CacheWriteTtl.ONE_HOUR)
    )
    assert pi_spend.cost_usd == pytest.approx(USAGE_WORKSPACE_COST_USD)
    assert claude_spend.cost_usd is not None and claude_spend.cost_usd > USAGE_WORKSPACE_COST_USD


def test_collect_spend_tells_an_unreadable_counter_from_a_block_that_names_no_tokens() -> None:
    """usage.json is read without a schema, because the driver that wrote a trial decides its shape,
    so the two ways a block can fail to state a count are read apart. A counter that is not a number
    -- a boolean, a string a hand-edited file carries -- counts nothing, so a row naming a model the
    map prices is a priced row that spent nothing in that bucket. A block naming no token map at all
    states nothing to price, which is not a claim that the spender cost nothing."""
    spend = collect_spend(
        {
            "workspace_agent": {"per_model": [{"model": USAGE_WORKSPACE_MODEL, "tokens": {"output": True}}]},
            "decider": {"model": "claude-haiku-4-5", "call_count": 3},
        },
        FIXTURE_PRICE_MAP,
        CacheWriteTtl.FIVE_MINUTES,
    )

    assert [(entry.spender, entry.cost_usd, entry.unpriced_models) for entry in spend] == [
        (Spender.WORKSPACE_AGENT, 0.0, ()),
        (Spender.DECIDER, None, ()),
    ]


def test_collect_spend_charges_the_claude_harnesss_cache_writes_at_the_one_hour_rate() -> None:
    """Claude Code asks for the 1-hour prompt cache, which Anthropic bills at 2x an input token
    against the 1.25x a 5-minute write costs. Pricing every harness the same way would understate that
    bucket by 37.5% on every claude arm -- the arms that write the most cache."""
    payload = trial_usage_payload()

    workspace_spend = collect_spend(payload, FIXTURE_PRICE_MAP, CacheWriteTtl.ONE_HOUR)[0]

    assert workspace_spend.cost_usd == pytest.approx(
        usage_cost_usd(USAGE_WORKSPACE_MODEL, USAGE_WORKSPACE_TOKENS, cache_write_ttl=CacheWriteTtl.ONE_HOUR)
    )
    assert workspace_spend.cost_usd is not None and workspace_spend.cost_usd > USAGE_WORKSPACE_COST_USD


def test_collect_spend_prices_a_trials_two_speed_tiers_apart() -> None:
    """Fast mode bills the same tokens at twice the standard rate, and the proxy records which tier
    served each request. The fast portion rides as a subset of the totals, so a trial that ran half its
    output fast costs half as much again -- not twice, and not the standard figure."""
    tokens = TokenSnapshot(output=20_000)
    fast_tokens = TokenSnapshot(output=10_000)

    workspace_spend = collect_spend(
        trial_usage_payload(workspace_tokens=tokens, workspace_fast_tokens=fast_tokens),
        FIXTURE_PRICE_MAP,
        CacheWriteTtl.FIVE_MINUTES,
    )[0]

    standard_figure = usage_cost_usd(USAGE_WORKSPACE_MODEL, tokens, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
    assert workspace_spend.cost_usd == pytest.approx(1.5 * standard_figure)


def test_collect_spend_refuses_a_figure_for_fast_traffic_on_a_model_that_cannot_serve_the_tier() -> None:
    """Haiku is priced, but the API rejects the fast tier on it, so a row claiming fast traffic there
    is a record the standard rate does not describe: halving a real bill is worse than reporting no
    figure. The model is named as the reason, which is what the cell prints."""
    workspace_spend = collect_spend(
        trial_usage_payload(
            workspace_model="claude-haiku-4-5",
            workspace_tokens=TokenSnapshot(output=20_000),
            workspace_fast_tokens=TokenSnapshot(output=10_000),
        ),
        FIXTURE_PRICE_MAP,
        CacheWriteTtl.FIVE_MINUTES,
    )[0]

    assert workspace_spend.cost_usd is None
    assert workspace_spend.unpriced_models == ("claude-haiku-4-5",)


def test_collect_spend_reads_a_fast_subset_larger_than_its_own_totals_as_no_standard_traffic() -> None:
    """The fast counts ride as a subset of the totals, so what is left of them is the standard
    portion. A record whose subset exceeds its own totals is contradicting itself, and a negative
    bucket would come out as a discount against the fast portion beside it."""
    fast_tokens = TokenSnapshot(output=10_000)

    workspace_spend = collect_spend(
        trial_usage_payload(workspace_tokens=TokenSnapshot(output=5_000), workspace_fast_tokens=fast_tokens),
        FIXTURE_PRICE_MAP,
        CacheWriteTtl.FIVE_MINUTES,
    )[0]

    fast_figure = usage_cost_usd(USAGE_WORKSPACE_MODEL, fast_tokens, cache_write_ttl=CacheWriteTtl.FIVE_MINUTES)
    assert workspace_spend.cost_usd == pytest.approx(2 * fast_figure)


def test_render_summary_markdown_splits_a_trials_cost_into_the_agents_and_the_harnesss(tmp_path: Path) -> None:
    """The agent under test is what the eval measures and the harness is what running it costs, so
    the two never share a column. The harness's own column sums the decider and the verification
    agent, which are one source: host-side calls on one model at one rate."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        usage=trial_usage_payload(),
    )

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_spend_cells(summary, "todo-app__aaaaaaa") == ("$1.50", "$0.35")
    assert "agent spend $1.50 over 1 trial; harness spend $0.35 over 1 trial" in summary
    # Nothing about this figure is in doubt, so the marks -- and the legend explaining them -- stay
    # out of the report.
    assert SPEND_LEGEND not in summary


@pytest.mark.parametrize(
    ("is_cost_complete", "is_cost_rate_certain"),
    [
        pytest.param(False, True, id="traffic the total does not hold"),
        pytest.param(True, False, id="a rate nobody observed"),
        pytest.param(False, False, id="both"),
    ],
)
def test_render_summary_markdown_marks_a_cost_that_is_only_a_lower_bound(
    tmp_path: Path, is_cost_complete: bool, is_cost_rate_certain: bool
) -> None:
    """Either flag makes the figure a floor, and a floor printed as a plain number is the one
    reading that misleads: an arm that delegated, or ran fast and was priced standard, looks cheaper
    than one that did neither."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(
        job_dir,
        "todo-app__aaaaaaa",
        usage=trial_usage_payload(is_cost_complete=is_cost_complete, is_cost_rate_certain=is_cost_rate_certain),
    )

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    # The harness's own half is unaffected: its calls are this host's, on one model at one rate.
    assert read_spend_cells(summary, "todo-app__aaaaaaa") == ("$1.50+", "$0.35")
    assert "agent spend $1.50+ over 1 trial" in summary
    assert summary.rstrip().endswith(SPEND_LEGEND)


def test_render_summary_markdown_marks_a_workspace_block_that_states_neither_flag(tmp_path: Path) -> None:
    """A block that does not say plainly is read as saying no, which every usage.json written before
    the flags existed is. Reading an unstated flag as yes would let the oldest trials in a comparison
    be the only ones whose figures look exact."""
    payload = trial_usage_payload()
    for flag_name in ("is_cost_complete", "is_cost_rate_certain"):
        payload["workspace_agent"].pop(flag_name)
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=payload)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    (workspace_spend,) = select_spend(run_check.trials[0].spend, AGENT_SPENDERS)
    assert (workspace_spend.is_complete, workspace_spend.is_rate_certain) == (False, False)
    assert read_spend_cells(render_summary_markdown(run_check), "todo-app__aaaaaaa")[0] == "$1.50+"


@pytest.mark.parametrize(
    ("usage", "spender"),
    [
        pytest.param(trial_usage_payload(verifier_failed_call_count=1), Spender.VERIFIER_AGENT, id="a failed call"),
        pytest.param(trial_usage_payload(decider_fallback_count=1), Spender.DECIDER, id="a decider fallback"),
    ],
)
def test_render_summary_markdown_marks_a_harness_call_that_came_back_with_nothing(
    tmp_path: Path, usage: dict[str, Any], spender: Spender
) -> None:
    """A harness call that raised reports no tokens, and the provider may have billed it anyway, so a
    block carrying one holds less than the spender spent. The decider's fallbacks read the same way;
    neither can be told apart from a call that answered unusably and was billed, which is in the
    total, so the mark is the conservative reading of both -- and one of them is enough to mark the
    half they are summed into."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=usage)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
    summary = render_summary_markdown(run_check)

    harness_spend = next(entry for entry in run_check.trials[0].spend if entry.spender is spender)
    assert harness_spend.is_complete is False
    assert read_spend_cells(summary, "todo-app__aaaaaaa") == ("$1.50", "$0.35+")


def test_render_summary_markdown_names_the_models_that_left_a_cost_unpriced(tmp_path: Path) -> None:
    """A trial whose model is not in the price map reports no cost at all rather than the part of it
    that could be priced. Naming the model is what makes that recoverable: it says to price the model,
    not to go looking for a broken trial."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload(workspace_model="kimi-k2.6"))

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
    summary = render_summary_markdown(run_check)

    assert read_spend_cells(summary, "todo-app__aaaaaaa")[0] == "? unpriced: kimi-k2.6"
    assert "agent spend unknown over 1 trial" in summary
    assert summary.rstrip().endswith(SPEND_LEGEND)
    # Cost is reported, never gated: an unpriceable model is not a failing trial.
    assert run_check.is_passed is True


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(trial_usage_payload(is_workspace_traffic_recorded=False), id="no traffic to price"),
        pytest.param(trial_usage_payload(workspace_model=""), id="traffic whose model the stream never named"),
    ],
)
def test_render_summary_markdown_marks_a_cost_nothing_named_a_reason_for(
    tmp_path: Path, usage: dict[str, Any]
) -> None:
    """A trial that priced no model at all -- it recorded no traffic to price, or recorded traffic
    under no model id -- has no figure and nothing to blame for it. The bare mark is still a refusal
    to price rather than a zero, and the legend has to read true of it: the models are named where
    the trial recorded them, so a trial that recorded none names none."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=usage)

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert read_spend_cells(summary, "todo-app__aaaaaaa")[0] == "?"
    assert "agent spend unknown over 1 trial" in summary
    assert summary.rstrip().endswith(SPEND_LEGEND)


@pytest.mark.parametrize(
    ("verifier_block", "is_verifier_key_present"),
    [
        pytest.param(None, True, id="a phase that did not run"),
        pytest.param({}, True, id="a block with nothing in it"),
        pytest.param(None, False, id="a file written before the block existed"),
    ],
)
def test_check_job_directory_reads_a_usage_file_with_no_verification_agent_in_it(
    tmp_path: Path, verifier_block: dict[str, Any] | None, is_verifier_key_present: bool
) -> None:
    """`null` says the verification agent never ran, an absent key says the driver that wrote the
    file could not have recorded it, and a block with nothing in it says neither. All three leave
    the same two spenders, and none may be read as a verification agent that cost nothing."""
    payload = trial_usage_payload()
    if is_verifier_key_present:
        payload["verifier_agent"] = verifier_block
    else:
        payload.pop("verifier_agent")
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=payload)

    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)

    assert [entry.spender for entry in run_check.trials[0].spend] == [Spender.WORKSPACE_AGENT, Spender.DECIDER]
    assert read_spend_cells(render_summary_markdown(run_check), "todo-app__aaaaaaa")[1] == "$0.25"


def test_render_summary_markdown_totals_the_run_over_the_trials_that_recorded_a_cost(tmp_path: Path) -> None:
    """The totals line is what a run is compared with the last one on, so it has to say what it
    covers: the trials whose figure is known are summed, the ones that priced nothing are counted
    beside them rather than dropped, and a trial that recorded no spend at all is in neither."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())
    write_trial_dir(
        job_dir,
        "greeting__bbbbbbb",
        case_id="greeting",
        usage=trial_usage_payload(workspace_tokens=usage_tokens_costing(2.25), is_cost_rate_certain=False),
    )
    write_trial_dir(job_dir, "probe__ccccccc", case_id="probe", usage=trial_usage_payload(workspace_model="kimi-k2.6"))
    write_trial_dir(job_dir, "oracle__ddddddd", case_id="oracle-case")

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    totals_line = summary.splitlines()[2]
    assert totals_line == ("agent spend $3.75+ over 3 trials, 1 unknown; harness spend $1.05 over 3 trials")


def test_render_summary_markdown_says_which_price_map_the_figures_came_from(tmp_path: Path) -> None:
    """The trials carry tokens, so the same run checked again after a price change reports different
    money. The line names whichever map priced the run -- the fixture one here -- and is left out of a
    run that recorded no spend at all, where nothing was priced to qualify."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert "priced by fixture 0.0 (fixture price map)" in summary.splitlines()

    priceless_job_dir = tmp_path / "oracle-run"
    write_trial_dir(priceless_job_dir, "greeting__bbbbbbb", case_id="greeting")

    assert "priced by" not in render_summary_markdown(check_job_directory(priceless_job_dir, FIXTURE_PRICE_MAP))


def test_render_summary_markdown_says_a_runs_agent_spend_is_unknown_when_no_trial_priced_one(
    tmp_path: Path,
) -> None:
    """Every trial of a pi arm on OpenRouter is unpriced today. A total of `$0.00` over those trials
    would be a claim that the run was free, so the figure gives way to the word and the trial count
    stays."""
    job_dir = tmp_path / "nightly-run"
    for trial_name in ("todo-app__aaaaaaa", "greeting__bbbbbbb"):
        write_trial_dir(
            job_dir,
            trial_name,
            case_id=trial_name.partition("__")[0],
            usage=trial_usage_payload(workspace_model="kimi-k2.6"),
        )

    summary = render_summary_markdown(check_job_directory(job_dir, FIXTURE_PRICE_MAP))

    assert summary.splitlines()[2].startswith("agent spend unknown over 2 trials;")


def test_check_job_directory_refuses_a_usage_file_it_cannot_read(tmp_path: Path) -> None:
    """Absent and corrupt are different claims about a trial's cost account, and only the first one
    is expected. A truncated file read as "this trial recorded nothing" would quietly drop a trial
    out of the run's totals."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload())
    (job_dir / "todo-app__aaaaaaa" / "agent" / "usage.json").write_text('{"workspace_agent": {"cost_u')

    with pytest.raises(JobReadError, match="not valid JSON"):
        check_job_directory(job_dir, FIXTURE_PRICE_MAP)


def test_write_run_check_reports_carries_every_spend_record_into_the_json_summary(tmp_path: Path) -> None:
    """The Slack report is built from this file rather than from the job directory, so a record that
    does not survive the dump is one no scheduled run ever sees."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", usage=trial_usage_payload(is_cost_complete=False))
    summary_json = tmp_path / "out" / "summary.json"

    write_run_check_reports(check_job_directory(job_dir, FIXTURE_PRICE_MAP), None, summary_json)

    assert json.loads(summary_json.read_text())["trials"][0]["spend"] == [
        {
            "spender": "workspace_agent",
            "cost_usd": 1.50,
            "is_complete": False,
            "is_rate_certain": True,
            "unpriced_models": [],
        },
        {
            "spender": "decider",
            "cost_usd": 0.25,
            "is_complete": True,
            "is_rate_certain": True,
            "unpriced_models": [],
        },
        {
            "spender": "verifier_agent",
            "cost_usd": 0.10,
            "is_complete": True,
            "is_rate_certain": True,
            "unpriced_models": [],
        },
    ]


def test_is_gates_dimension_passed_rejects_a_dimension_that_was_never_scored() -> None:
    assert is_gates_dimension_passed(None) is False
    assert is_gates_dimension_passed({}) is False
    assert is_gates_dimension_passed({"gates": {"score": 1.0, "criteria": []}}) is False


def test_is_gates_dimension_passed_reads_both_shapes_rewardkit_emits() -> None:
    passing_criteria = [{"name": name, "value": 1.0} for name in GATES_CRITERION_NAMES]

    assert is_gates_dimension_passed({"gates": {"criteria": passing_criteria}}) is True
    assert is_gates_dimension_passed({"gates": [{"criteria": passing_criteria}]}) is True
    assert is_gates_dimension_passed({"gates": [{"criteria": passing_criteria}, {"criteria": [{"value": 0.0}]}]}) is (
        False
    )


def test_is_gates_dimension_passed_rejects_a_dimension_whose_criteria_are_not_a_list() -> None:
    """reward-details.json is read without a schema, so a `criteria` of the wrong shape has to be
    absorbed rather than raise -- and absorbing it means the gates went unscored, not that they held."""
    assert is_gates_dimension_passed({"gates": {"criteria": {"not_timed_out": 1.0}}}) is False


def test_is_gates_dimension_passed_treats_a_value_it_cannot_read_as_a_failed_gate() -> None:
    """reward-details.json is read without a schema, so a criterion whose value is not a number has
    to resolve to a verdict rather than to a TypeError out of the middle of the gate."""
    assert is_gates_dimension_passed({"gates": {"criteria": [{"name": "not_timed_out", "value": "yes"}]}}) is False
    assert is_gates_dimension_passed({"gates": {"criteria": [{"name": "not_timed_out", "value": True}]}}) is False
    assert is_gates_dimension_passed({"gates": {"criteria": [{"name": "not_timed_out"}]}}) is False


def _passing_gates_criteria() -> list[dict[str, Any]]:
    return [{"name": name, "value": 1.0} for name in GATES_CRITERION_NAMES]


# Every reward-details shape the two gate deciders both have to answer, and answer alike. The one
# documented divergence is left out on purpose: a criterion value neither of them can read (a
# string, a None) resolves to zero on this side, because the run gate must always reach a verdict,
# and raises on the verifier's side, where a malformed file its own verifier wrote is a bug worth
# stopping on.
_MIRRORED_GATE_SHAPES: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("no-gates-dimension", {}),
    ("dimension-is-not-a-mapping", {"gates": "nonsense"}),
    ("dimension-emitted-as-one-dict", {"gates": {"criteria": _passing_gates_criteria()}}),
    ("dimension-emitted-as-a-list", {"gates": [{"criteria": _passing_gates_criteria()}]}),
    ("no-criteria-at-all", {"gates": {"criteria": []}}),
    ("criteria-are-not-a-list", {"gates": {"criteria": {"not_timed_out": 1.0}}}),
    ("one-criterion-scored-zero", {"gates": {"criteria": [*_passing_gates_criteria(), {"value": 0.0}]}}),
    ("one-criterion-scored-negative", {"gates": {"criteria": [{"name": "not_timed_out", "value": -1.0}]}}),
    ("criterion-carries-no-value", {"gates": {"criteria": [{"name": "not_timed_out"}]}}),
    ("criterion-scored-true", {"gates": {"criteria": [{"name": "not_timed_out", "value": True}]}}),
    ("criterion-scored-false", {"gates": {"criteria": [{"name": "not_timed_out", "value": False}]}}),
    (
        "a-second-reward-dict-fails",
        {"gates": [{"criteria": _passing_gates_criteria()}, {"criteria": [{"value": 0.0}]}]},
    ),
    ("other-dimensions-are-ignored", {"gates": {"criteria": _passing_gates_criteria()}, "quality": {"score": 0.0}}),
)


@pytest.mark.parametrize(
    "reward_details", [pytest.param(shape, id=shape_id) for shape_id, shape in _MIRRORED_GATE_SHAPES]
)
def test_both_ends_of_a_trial_read_the_structural_gates_the_same_way(reward_details: dict[str, Any]) -> None:
    """`is_gates_dimension_passed` decides the scheduled run's exit code; `_gates_all_passed` in
    templates/tests/verifier/finalize.py decides the same trial's own reward, inside the verifier container.
    They are hand-mirrored, because that container has stdlib and rewardkit and no imbue package, so
    nothing but this test keeps them saying the same thing about the same file. Split them and a
    trial's reward and the nightly's verdict disagree, with the run reporting neither."""
    finalize = load_template_module("tests/verifier/finalize.py", "minds_evals_finalize")

    assert is_gates_dimension_passed(reward_details) == finalize._gates_all_passed(reward_details)


def test_collect_criterion_scores_reports_a_criterion_whose_value_is_unreadable(
    captured_log_messages: list[str],
) -> None:
    """There is always a number to report, because an unreadable value reads as the bottom of the
    scale -- the same reading the gate verdict takes of one. And it is said out loud, because that
    bottom score is what a criterion nobody could read and a criterion that scored nothing both
    print as."""
    scores = collect_criterion_scores("", {"quality": [{"kind": "llm", "criteria": [{"name": "tone", "value": {}}]}]})

    assert [(score.criterion, score.value) for score in scores] == [("tone", 0.0)]
    assert any("tone" in message for message in captured_log_messages)


def test_collect_criterion_scores_records_every_kind_rewardkit_emits() -> None:
    """rewardkit tags an AgentJudge's rewards `agent`, an LLMJudge's `llm` and a .py criterion's
    `programmatic`. All three are collected, and the kind is what separates a number a model gave
    from one a check computed."""
    scores = collect_criterion_scores(
        "",
        {
            "outcome": [{"kind": "agent", "criteria": [{"name": "delivered", "value": 0.5}]}],
            "quality": [
                {"kind": "programmatic", "criteria": [{"name": "wordiness", "value": 1.0, "raw": True}]},
                {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.777, "raw": 8}]},
            ],
        },
    )

    assert [(score.dimension, score.criterion, score.kind) for score in scores] == [
        ("outcome", "delivered", CriterionKind.AGENT),
        ("quality", "wordiness", CriterionKind.PROGRAMMATIC),
        ("quality", "conciseness", CriterionKind.LLM),
    ]
    assert scores[-1].value == pytest.approx(0.777)


def test_the_criteria_cell_keeps_two_dimensions_scores_of_one_name_apart() -> None:
    """A criterion name belongs to the dimension that scored it, not to the case, so two dimensions
    can both score `conciseness`. Listed flat the cell would read `conciseness 0.70, conciseness
    0.90` -- one name twice, with nothing saying which number measured the product and which
    measured the harness that drove it."""
    scores = collect_criterion_scores(
        "",
        {
            "outcome": {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.7}]},
            "quality": {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.9}]},
        },
    )

    assert _format_criteria_cell(scores) == "outcome: conciseness 0.70; quality: conciseness 0.90"


def test_collect_criterion_scores_names_the_step_it_is_given() -> None:
    scores = collect_criterion_scores(
        "build", {"quality": {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.5}]}}
    )

    assert [(score.step, score.criterion) for score in scores] == [("build", "conciseness")]


@pytest.mark.parametrize("reward_dict", [{"kind": "oracle"}, {}], ids=["unknown-kind", "no-kind-at-all"])
def test_collect_criterion_scores_says_so_when_a_kind_cannot_be_placed(
    reward_dict: dict[str, Any], captured_log_messages: list[str]
) -> None:
    """A kind this side has no member for leaves its criteria nowhere to be recorded. This report is
    the only place a criterion score exists, so criteria dropped in silence leave a column that reads
    as a case nothing scored rather than as one whose scores went unread."""
    scores = collect_criterion_scores(
        "", {"quality": {**reward_dict, "criteria": [{"name": "conciseness", "value": 0.78}]}}
    )

    assert scores == ()
    assert any("conciseness" in message for message in captured_log_messages)


def test_collect_criterion_scores_passes_over_the_markers_the_verifier_stamps_in() -> None:
    """finalize.py stamps `timed_out`, `harness` and `outcome_evidence` into the same file. None of
    them is a reward, so none has criteria to drop, and none may be reported as a score or announced
    as one that went unread."""
    scores = collect_criterion_scores(
        "",
        {
            "timed_out": False,
            "harness": {"name": "claude", "is_harness_quality_scored": True},
            "outcome_evidence": {"is_complete": True},
            "quality": {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.78}]},
        },
    )

    assert [score.criterion for score in scores] == ["conciseness"]


def test_collect_dimension_scores_reports_nothing_for_a_trial_that_was_never_graded(tmp_path: Path) -> None:
    """Two ways a trial reaches this with no scores to report: harbor never wrote a result for it at
    all, and it wrote one carrying no verifier result."""
    job_dir = tmp_path / "nightly-run"
    trial_dir = write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    assert collect_dimension_scores(None) == ()
    assert collect_dimension_scores(load_trial_result(trial_dir / "result.json")) == ()


@pytest.mark.parametrize(
    ("usage", "expected_seconds"),
    [
        pytest.param(None, None, id="no-account-at-all"),
        pytest.param({"workspace_agent": {}}, None, id="an-account-written-before-the-turns-were"),
        pytest.param({"per_turn": []}, 0.0, id="an-account-of-no-answered-turn"),
        pytest.param({"per_turn": [{"reply_seconds": 12.5}, {"reply_seconds": 7.5}]}, 20.0, id="two-answered-turns"),
    ],
)
def test_total_reply_seconds_tells_an_unrecorded_span_from_a_conversation_of_no_replies(
    usage: dict[str, Any] | None, expected_seconds: float | None
) -> None:
    assert total_reply_seconds(usage) == expected_seconds


def test_the_workflow_gates_a_cell_on_a_name_this_package_still_writes() -> None:
    """A cell reads its pair's oracle verdict straight out of the summary JSON with `jq`, because a
    matrix job cannot depend on one leg of another matrix job. Renaming that field makes every jq
    read print `null`, which fails closed -- but only after the pair's oracle pass has been paid
    for, and with no diagnosis of why."""
    names_read = set(re.findall(r"""jq -r '\.([a-z_]+)' "\$ORACLE_SUMMARY""", read_scheduled_workflow_text()))

    assert names_read, "no oracle summary reads found in {}".format(SCHEDULED_WORKFLOW_PATH)
    known = set(RunCheck.model_fields) | set(RunCheck.model_computed_fields)
    assert names_read <= known, "RunCheck does not carry {}".format(sorted(names_read - known))
