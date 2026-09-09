import json
import re
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_evals.check_run import check_job_directory
from imbue.minds_evals.check_run import collect_judge_scores
from imbue.minds_evals.check_run import is_gates_dimension_passed
from imbue.minds_evals.check_run import render_summary_markdown
from imbue.minds_evals.check_run import write_run_check_reports
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import JudgeScore
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import GATES_CRITERION_NAMES
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import expected_modal_environment_name
from imbue.minds_evals.testing import write_trial_dir


def test_check_job_directory_passes_a_run_whose_every_trial_completed_and_gated_open(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting")

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is True
    assert run_check.job_name == "nightly-run"
    assert [trial.trial_name for trial in run_check.trials] == ["greeting__bbbbbbb", "todo-app__aaaaaaa"]
    assert [trial.case_id for trial in run_check.trials] == ["greeting", "todo-app"]
    assert all(trial.reward == 0.75 for trial in run_check.trials)
    assert run_check.modal_environment_names == (
        expected_modal_environment_name("greeting__bbbbbbb"),
        expected_modal_environment_name("todo-app__aaaaaaa"),
    )


def test_check_job_directory_reports_judge_scores_without_gating_on_them(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", judge_raw_score=1.0)

    run_check = check_job_directory(job_dir)

    # A judge score at the very bottom of the likert scale is recorded and does not fail the run.
    assert run_check.is_passed is True
    assert [(score.criterion, score.raw_score) for score in run_check.trials[0].judge_scores] == [("conciseness", 1.0)]


def test_check_job_directory_fails_a_trial_whose_evidence_the_harness_could_not_measure(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", errored_entry_ids=("http_0_registered_apps_0",))

    run_check = check_job_directory(job_dir)

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

    run_check = check_job_directory(job_dir)

    fell_short = next(trial for trial in run_check.trials if trial.trial_name == "todo-app__aaaaaaa")
    unmeasured = next(trial for trial in run_check.trials if trial.trial_name == "greeting__bbbbbbb")
    assert fell_short.error_entry_ids == ()
    assert fell_short.is_passed is True
    assert unmeasured.error_entry_ids == ("http_0_root_0",)
    assert unmeasured.is_passed is False


def test_check_job_directory_fails_a_trial_whose_structural_gates_did_not_hold(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", failed_gate_names=("all_turns_completed",))

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_gates_passed is False
    assert run_check.trials[0].error_entry_ids == ()


def test_check_job_directory_fails_a_trial_harbor_recorded_an_exception_for(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    run_check = check_job_directory(job_dir)

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

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "step agent raised SandboxTimeout" in run_check.trials[0].incompletion_reason


def test_check_job_directory_fails_a_trial_harbor_never_wrote_a_result_for(tmp_path: Path) -> None:
    """The shape a trial killed on Modal leaves: harbor writes result.json last, so it got no chance
    to record anything. The gate must charge the trial rather than skip over the file it cannot
    find, which is the one reading that would let a dead trial pass."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", is_result_written=False)

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "no result.json" in run_check.trials[0].incompletion_reason
    assert run_check.trials[0].reward is None


def test_check_job_directory_fails_a_trial_that_never_wrote_its_state(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", is_state_written=False)

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert run_check.trials[0].is_completed is False
    assert "state.json" in run_check.trials[0].incompletion_reason
    # Nothing to clean up under that name, so the run's environment list must not carry an empty one.
    assert run_check.modal_environment_names == ()


def test_check_job_directory_fails_a_trial_whose_conversation_timed_out(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", test_state="timed_out")

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert "timed_out" in run_check.trials[0].incompletion_reason


def test_check_job_directory_passes_an_oracle_trial_that_recorded_no_evidence(tmp_path: Path) -> None:
    """A bare oracle case fabricates no evidence bundle at all, which is not the harness failing to
    measure -- there was nothing there to measure."""
    job_dir = tmp_path / "oracle-run"
    write_trial_dir(job_dir, "greeting__ccccccc", case_id="greeting", is_manifest_written=False)

    run_check = check_job_directory(job_dir)

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

    run_check = check_job_directory(job_dir)

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

    run_check = check_job_directory(job_dir)

    assert run_check.is_passed is False
    assert all(entry_id for entry_id in run_check.trials[0].error_entry_ids)
    trial_row = next(
        line for line in render_summary_markdown(run_check).splitlines() if line.startswith("| todo-app__aaaaaaa")
    )
    assert trial_row.split(" | ")[4] != "none"


def test_check_job_directory_ignores_the_cache_harbor_leaves_after_a_regrade(tmp_path: Path) -> None:
    """`harbor trial regrade` on a hub trial id caches its download under <job dir>/.sources/<uuid>.
    Reading that as a trial would fail the run over an artifact of having regraded it."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / ".sources" / "9f1c0b3e").mkdir(parents=True)

    run_check = check_job_directory(job_dir)

    assert [trial.trial_name for trial in run_check.trials] == ["todo-app__aaaaaaa"]
    assert run_check.is_passed is True


def test_check_job_directory_refuses_a_job_directory_that_is_not_there(tmp_path: Path) -> None:
    """A mistyped path is not an empty run. Left to iterdir it would raise a FileNotFoundError from
    the middle of the gate rather than saying which directory the caller named."""
    with pytest.raises(JobReadError, match="not a job directory"):
        check_job_directory(tmp_path / "never-created")


def test_check_job_directory_refuses_a_directory_with_no_trials(tmp_path: Path) -> None:
    empty_job_dir = tmp_path / "nothing-ran"
    empty_job_dir.mkdir()

    with pytest.raises(JobReadError, match="no trial directories"):
        check_job_directory(empty_job_dir)


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
        check_job_directory(job_dir)


def test_check_job_directory_refuses_a_trial_state_it_cannot_read(tmp_path: Path) -> None:
    """The strict half of an asymmetry the branch rests on: cleanup meets the same truncated
    state.json and skips that trial with a warning, because refusing it would leak every other
    trial's environment. The gate must do the opposite -- a trial whose state cannot be read is one
    nothing can say completed, and an unjudged run must never come out as a pass."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    (job_dir / "todo-app__aaaaaaa" / "agent" / "state.json").write_text('{"case_name": "todo-a')

    with pytest.raises(JobReadError, match="not valid JSON"):
        check_job_directory(job_dir)


def test_render_summary_markdown_puts_every_trial_and_the_verdict_in_the_table(tmp_path: Path) -> None:
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    write_trial_dir(job_dir, "greeting__bbbbbbb", case_id="greeting", failed_gate_names=("not_timed_out",))

    summary = render_summary_markdown(check_job_directory(job_dir))

    assert summary.startswith("## minds-evals: nightly-run -- FAIL")
    assert "| todo-app__aaaaaaa | todo-app |" in summary
    assert expected_modal_environment_name("greeting__bbbbbbb") in summary
    assert "conciseness 8.0" in summary
    # One header row, one separator, and one row per trial.
    assert len([line for line in summary.splitlines() if line.startswith("|")]) == 4


def test_render_summary_markdown_renders_a_trial_that_never_got_graded(tmp_path: Path) -> None:
    """The row a red nightly is actually read for: harbor recorded an exception, so the trial has no
    verifier result. The reward cell has to degrade to a dash rather than to a formatting error on
    None, and the row still has to say what went wrong."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", exception_type="DaemonError")

    trial_row = next(
        line
        for line in render_summary_markdown(check_job_directory(job_dir)).splitlines()
        if line.startswith("| todo-app__aaaaaaa")
    )

    assert "DaemonError" in trial_row
    assert trial_row.split(" | ")[5] == "-"


@pytest.mark.parametrize("is_markdown_wanted", [True, False])
def test_write_run_check_reports_writes_only_what_was_asked_for(tmp_path: Path, is_markdown_wanted: bool) -> None:
    """The scheduled job asks for both, but each is optional and the other must not appear."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa")
    summary_md = tmp_path / "out" / "summary.md"
    summary_json = tmp_path / "out" / "summary.json"

    write_run_check_reports(
        check_job_directory(job_dir),
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

    summary = render_summary_markdown(check_job_directory(job_dir))

    trial_row = next(line for line in summary.splitlines() if line.startswith("| todo-app__aaaaaaa"))
    assert "Daemon\\|Error" in trial_row
    # Ten columns means every pipe in the message stayed escaped.
    assert trial_row.count("|") - trial_row.count("\\|") == 11


def test_render_summary_markdown_keeps_every_free_text_cell_inside_its_column(tmp_path: Path) -> None:
    """Exception messages are not the only free text in the row: case ids come from the eval config
    and entry ids from the evidence manifest, and neither is held to a vocabulary. One unescaped pipe
    shifts every cell after it into the wrong column, so the table misreports the run it is read for."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", case_id="todo|app", errored_entry_ids=("http|0",))

    summary = render_summary_markdown(check_job_directory(job_dir))

    trial_row = next(line for line in summary.splitlines() if line.startswith("| todo-app__aaaaaaa"))
    assert "todo\\|app" in trial_row
    assert "http\\|0" in trial_row
    assert trial_row.count("|") - trial_row.count("\\|") == 11


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


def test_collect_judge_scores_reports_a_score_whose_normalization_is_unreadable() -> None:
    scores = collect_judge_scores(
        {"quality": [{"kind": "llm", "criteria": [{"name": "tone", "value": {}, "raw": 7}]}]}
    )

    assert [(score.criterion, score.raw_score, score.normalized_score) for score in scores] == [("tone", 7.0, 0.0)]


def test_collect_judge_scores_says_so_when_a_likert_answer_cannot_be_read(captured_log_messages: list[str]) -> None:
    """The report is the only place a judge score exists, so a criterion dropped in silence leaves a
    column that reads as a case with no judges rather than as one whose answers went unread."""
    scores = collect_judge_scores(
        {"quality": {"kind": "llm", "criteria": [{"name": "conciseness", "raw": "eight", "value": 0.78}]}}
    )

    assert scores == ()
    assert any("quality.conciseness" in message for message in captured_log_messages)


def test_collect_judge_scores_ignores_the_programmatic_guards_scored_alongside_the_judges() -> None:
    scores = collect_judge_scores(
        {
            "quality": [
                {"kind": "programmatic", "criteria": [{"name": "wordiness", "value": 1.0, "raw": True}]},
                {"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.777, "raw": 8}]},
            ]
        }
    )

    assert [(score.dimension, score.criterion, score.raw_score) for score in scores] == [
        ("quality", "conciseness", 8.0)
    ]
    assert scores[0].normalized_score == pytest.approx(0.777)


def test_collect_judge_scores_reports_both_judge_kinds_rewardkit_emits() -> None:
    """rewardkit tags an AgentJudge's rewards `agent` and an LLMJudge's `llm`. Both are judges, so
    matching only one kind would drop a case's scores from the report with no sign it happened."""
    scores = collect_judge_scores(
        {
            "outcome": [{"kind": "agent", "criteria": [{"name": "delivered", "value": 0.5, "raw": 5}]}],
            "quality": [{"kind": "llm", "criteria": [{"name": "conciseness", "value": 0.777, "raw": 8}]}],
        }
    )

    assert [(score.dimension, score.criterion) for score in scores] == [
        ("outcome", "delivered"),
        ("quality", "conciseness"),
    ]


# The local name the scheduled workflow's Slack report binds each of check-run's rows to, and the
# model that writes that row. The workflow cannot import Python, so it reads the rows by string
# literal; the test below asserts every literal is still a field of the model behind it.
_REPORTED_ROW_MODELS: Final[tuple[tuple[str, type[FrozenModel]], ...]] = (
    ("trial", TrialCheck),
    ("score", JudgeScore),
    ("report", RunCheck),
)


@pytest.mark.parametrize(
    ("row_name", "model"),
    [pytest.param(row_name, model, id=model.__name__) for row_name, model in _REPORTED_ROW_MODELS],
)
def test_the_slack_report_reads_keys_this_package_still_writes(row_name: str, model: type[FrozenModel]) -> None:
    """The Slack message is the only thing anyone reads on a nightly, and it reads check-run's JSON
    by string key with a fallback on every access -- deliberately, so a notification can never be
    what makes a run red. That forgiveness is what makes a rename here silent rather than loud:
    rename incompletion_reason and every trial row says "did not complete (unknown)"; rename
    judge_scores and the judge column simply disappears. So the keys are pinned from this side, the
    way the sweep prefix is."""
    key_pattern = r'{}(?:\.get\(|\[)"([a-z_]+)"'.format(row_name)
    reported_keys = set(re.findall(key_pattern, SCHEDULED_WORKFLOW_PATH.read_text()))

    # A rewrite that renamed the script's own local variables would otherwise leave this test
    # passing over nothing at all.
    assert reported_keys, "no {} keys found in {}; the report no longer reads it the way this test looks for".format(
        model.__name__, SCHEDULED_WORKFLOW_PATH
    )
    # Computed fields count: `is_passed` is one on both TrialCheck and RunCheck, and it decides
    # which emoji every row gets.
    known_fields = set(model.model_fields) | set(model.model_computed_fields)
    assert reported_keys <= known_fields, "{} does not carry {}".format(
        model.__name__, sorted(reported_keys - known_fields)
    )
