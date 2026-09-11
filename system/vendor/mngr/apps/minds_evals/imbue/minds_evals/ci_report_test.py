import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import get_args

import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from imbue.minds_evals.check_run import check_job_directory
from imbue.minds_evals.check_run import write_run_check_reports
from imbue.minds_evals.ci_report import DETAIL_BUDGET
from imbue.minds_evals.ci_report import GREEN_ICON_EMOJI
from imbue.minds_evals.ci_report import LIVE_SUMMARY_STEM
from imbue.minds_evals.ci_report import ORACLE_SUMMARY_STEM
from imbue.minds_evals.ci_report import SLACK_USERNAME
from imbue.minds_evals.ci_report import SUMMARY_ARTIFACT_PREFIX
from imbue.minds_evals.ci_report import SlackMessage
from imbue.minds_evals.ci_report import UNGREEN_ICON_EMOJI
from imbue.minds_evals.ci_report import as_slack_payload
from imbue.minds_evals.ci_report import format_budgeted_block
from imbue.minds_evals.ci_report import format_duration
from imbue.minds_evals.ci_report import format_ref_label
from imbue.minds_evals.ci_report import live_summary_path
from imbue.minds_evals.ci_report import oracle_summary_path
from imbue.minds_evals.ci_report import parse_run_check
from imbue.minds_evals.ci_report import render_slack_report
from imbue.minds_evals.cli import main
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import CiReportContext
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import JudgeScore
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import read_scheduled_workflow_text
from imbue.minds_evals.testing import write_trial_dir

RUN_URL: Final[str] = "https://github.com/imbue-ai/mngr-internal/actions/runs/42"
EVAL_CONFIG: Final[str] = "apps/minds_evals/configs/eval-config-small.json"
# A SHA whose first twelve characters are recognisable in the rendered pair label.
FROZEN_SHA: Final[str] = "abcdef123456" + "0" * 28
PAIR_LABEL: Final[str] = "[_mngr_ `main` (`abcdef123456`) | _dwt_ `main` (`abcdef123456`)]"

# What a cell running the default harness config records, and what one running a named model records
# when the switch took.
DEFAULT_HARNESS_CONFIG: Final[Mapping[str, Any]] = {"lane": "anthropic"}
HAIKU_HARNESS_CONFIG: Final[Mapping[str, Any]] = {
    "lane": "anthropic",
    "model": "haiku",
    "effort": "medium",
    "is_model_confirmed": True,
    "observed_models": ["claude-haiku-4-5-20251001"],
}
WRONG_MODEL_HARNESS_CONFIG: Final[Mapping[str, Any]] = {
    "lane": "anthropic",
    "model": "haiku",
    "effort": "medium",
    "is_model_confirmed": False,
    "observed_models": ["claude-opus-5"],
}
# What a cell records when nothing in the trial says which model answered: no transcript was
# captured, or the catalog id has no known reported name. The driver writes null there, which is
# silence rather than evidence of a wrong model, so the trial still passes.
UNCONFIRMED_HARNESS_CONFIG: Final[Mapping[str, Any]] = {"lane": "anthropic", "model": "haiku", "effort": "medium"}


def make_pair(pair_name: str, decision: PairDecision) -> DecidedPair:
    sha = "" if decision is PairDecision.UNRESOLVED else FROZEN_SHA
    return DecidedPair(pair=pair_name, mngr_ref="main", mngr_sha=sha, dwt_ref="main", dwt_sha=sha, decision=decision)


def make_cell(pair_name: str, harness_config: str, decision: CellDecision) -> MatrixCell:
    return MatrixCell(
        pair=pair_name,
        harness_config=harness_config,
        mngr_ref="main",
        mngr_sha=FROZEN_SHA,
        dwt_ref="main",
        dwt_sha=FROZEN_SHA,
        config=EVAL_CONFIG,
        lane_key_env="ANTHROPIC_API_KEY",
        harbor_args="[]",
        cache_key="minds-evals-green-{}-{}".format(pair_name, harness_config),
        decision=decision,
    )


def write_matrix(matrix_path: Path, pairs: Sequence[DecidedPair], cells: Sequence[MatrixCell]) -> None:
    """The decided matrix exactly as `ci-matrix` writes it, computed fields and all."""
    matrix = CiMatrix(config=EVAL_CONFIG, pairs=tuple(pairs), cells=tuple(cells))
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_path.write_text(matrix.model_dump_json())


def write_summary(summary_path: Path, job_dir: Path, **trial_arguments: Any) -> RunCheck:
    """One trial's job directory, graded the way `check-run` grades it and dumped where the report
    looks for it."""
    write_trial_dir(job_dir, "todo-app__aaaaaaa", **trial_arguments)
    run_check = check_job_directory(job_dir)
    write_run_check_reports(run_check, None, summary_path)
    return run_check


def write_two_case_summary(summary_path: Path, job_dir: Path, **trial_arguments: Any) -> RunCheck:
    """A pass over two cases, so the grid it feeds has more than one row."""
    write_trial_dir(job_dir, "greeting__aaaaaaa", case_id="greeting", **trial_arguments)
    write_trial_dir(job_dir, "todo-app__bbbbbbb", case_id="todo-app", **trial_arguments)
    run_check = check_job_directory(job_dir)
    write_run_check_reports(run_check, None, summary_path)
    return run_check


def write_passing_oracle(summaries_dir: Path, job_root: Path, pair_name: str = "main") -> RunCheck:
    """The pair's oracle pass, graded green, where the report looks for it.

    An oracle trial replays a canned transcript and boots no workspace, so it records no Modal
    environment -- which is what `is_environment_recorded=False` says here.
    """
    return write_summary(
        oracle_summary_path(summaries_dir, pair_name),
        job_root / "{}-oracle".format(pair_name),
        harness_config=DEFAULT_HARNESS_CONFIG,
        is_environment_recorded=False,
    )


def make_context(
    *,
    duration_seconds: int | None = 2480,
    is_live_pass_skipped: bool = False,
    resolve_result: str = "success",
    oracle_result: str = "success",
    evaluate_result: str = "success",
) -> CiReportContext:
    return CiReportContext(
        run_url=RUN_URL,
        trigger="schedule",
        duration_seconds=duration_seconds,
        is_live_pass_skipped=is_live_pass_skipped,
        resolve_result=resolve_result,
        oracle_result=oracle_result,
        evaluate_result=evaluate_result,
    )


def read_blocks(message: SlackMessage, block_type: str) -> tuple[dict[str, Any], ...]:
    return tuple(block for block in message.blocks if block["type"] == block_type)


def read_header(message: SlackMessage) -> str:
    return read_blocks(message, "header")[0]["text"]["text"]


def read_sections(message: SlackMessage) -> tuple[str, ...]:
    return tuple(block["text"]["text"] for block in read_blocks(message, "section"))


def read_details(message: SlackMessage) -> str:
    """The details section, without its heading; empty when the message has none."""
    details = [section for section in read_sections(message) if section.startswith("*details*")]
    return "" if not details else details[0].partition("\n")[2]


def read_cell(cell: Mapping[str, Any]) -> str:
    """One table cell as a single string, whichever of the two shapes it is in.

    A plain cell reads as its text and a rich cell as its emoji's name followed by whatever it puts
    beside it, so one assertion covers every half of what a cell says. The trailing spacer a passing
    reward carries is dropped, because it is there to hold the column in line rather than to say
    anything.
    """
    if cell["type"] == "raw_text":
        return cell["text"]
    (section,) = cell["elements"]
    return "".join(
        element["name"] if element["type"] == "emoji" else element["text"] for element in section["elements"]
    ).rstrip()


def read_table_rows(table: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(read_cell(cell) for cell in row) for row in table["rows"])


def read_grid(message: SlackMessage) -> tuple[tuple[str, ...], ...]:
    """The grid's rows, its cells flattened to strings; empty when the message carries no grid.

    The grid is the message's first table. A failures table is drawn only under a grid, because a
    trial that failed is a trial that was graded, and the judge table is nested in a container
    rather than standing at the top level.
    """
    tables = read_blocks(message, "table")
    if not tables:
        return ()
    return read_table_rows(tables[0])


def read_failed_trials(message: SlackMessage) -> tuple[tuple[str, ...], ...]:
    """The failures table; empty when nothing failed and the message drew none."""
    tables = read_blocks(message, "table")
    return () if len(tables) < 2 else read_table_rows(tables[1])


def read_container(message: SlackMessage) -> Mapping[str, Any]:
    """The collapsed container the judge table lives in; empty when the message has none."""
    containers = read_blocks(message, "container")
    return containers[0] if containers else {}


def read_judge_table(message: SlackMessage) -> tuple[tuple[str, ...], ...]:
    """Every graded trial of the pair, out of the collapsed container; empty when there is none."""
    container = read_container(message)
    if not container:
        return ()
    (table,) = [child for child in container["child_blocks"] if child["type"] == "table"]
    return read_table_rows(table)


def read_judge_legend(message: SlackMessage) -> str:
    """The line above the judge table naming which dimension scored which criteria."""
    container = read_container(message)
    if not container:
        return ""
    (context,) = [child for child in container["child_blocks"] if child["type"] == "context"]
    return context["elements"][0]["text"]


def read_all_tables(message: SlackMessage) -> tuple[Mapping[str, Any], ...]:
    """Every table in the message, the ones nested in a container included."""
    return read_blocks(message, "table") + tuple(
        child
        for container in read_blocks(message, "container")
        for child in container["child_blocks"]
        if child["type"] == "table"
    )


def make_bold_cell(text: str) -> dict[str, Any]:
    """A table cell as the report builds a bold one: harness configs are named this way."""
    return {
        "type": "rich_text",
        "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": text, "style": {"bold": True}}]}
        ],
    }


def make_judge_score(dimension: str, criterion: str, raw_score: float) -> JudgeScore:
    return JudgeScore(
        dimension=dimension, criterion=criterion, normalized_score=(raw_score - 1) / 9, raw_score=raw_score
    )


def make_graded_trial(case_id: str, judge_scores: Sequence[JudgeScore], reward: float | None = 0.75) -> TrialCheck:
    """One graded trial as a summary carries it.

    Built as a model rather than out of a job directory, because the judge criteria are what is under
    test and every fixture directory scores the same single one.
    """
    return TrialCheck(
        trial_name="{}__aaaaaaa".format(case_id),
        case_id=case_id,
        is_completed=True,
        incompletion_reason="",
        is_gates_passed=True,
        error_entry_ids=(),
        reward=reward,
        judge_scores=tuple(judge_scores),
        lane="anthropic",
        requested_model="",
        is_model_confirmed=None,
        wrong_model_reason="",
        modal_environment_name="minds-evals-{}".format(case_id),
        mngr_sha=FROZEN_SHA,
        dwt_sha=FROZEN_SHA,
    )


def write_model_summary(summary_path: Path, trials: Sequence[TrialCheck]) -> None:
    """A pass's summary written straight from the model `check-run` dumps."""
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(RunCheck(job_name=summary_path.stem, trials=tuple(trials)).model_dump_json())


# What Slack accepts in the blocks this report builds. A block over its limit is not rendered
# smaller: the whole message is refused, and the run falls back to posting its plain text.
SLACK_HEADER_LIMIT: Final[int] = 150
SLACK_SECTION_LIMIT: Final[int] = 3000
SLACK_TABLE_ROW_LIMIT: Final[int] = 100
SLACK_TABLE_CELL_LIMIT: Final[int] = 20
SLACK_CONTAINER_CHILD_LIMIT: Final[int] = 10
SLACK_TABLE_CHARACTER_LIMIT: Final[int] = 10000
SLACK_TABLE_CELL_CHARACTER_LIMIT: Final[int] = 120


def assert_within_slack_limits(message: SlackMessage) -> None:
    assert len(read_header(message)) <= SLACK_HEADER_LIMIT
    assert [len(section) for section in read_sections(message) if len(section) > SLACK_SECTION_LIMIT] == []
    for table in read_all_tables(message):
        assert len(table["rows"]) <= SLACK_TABLE_ROW_LIMIT
        assert [len(row) for row in table["rows"] if len(row) > SLACK_TABLE_CELL_LIMIT] == []
    for container in read_blocks(message, "container"):
        assert len(container["child_blocks"]) <= SLACK_CONTAINER_CHILD_LIMIT


def test_render_slack_report_reports_a_green_run_as_a_grid_of_cases_by_config(tmp_path: Path) -> None:
    """The whole shape of a good night: the pair in the header, the commits it froze to under it,
    and a grid whose rows are the cases and whose columns are the harness configs."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    assert read_header(message) == "minds-evals: main -- passed"
    assert read_sections(message)[0] == (
        ":white_check_mark: {} -- oracle passed\npassed in 41m20s, _trigger=_ `schedule`".format(PAIR_LABEL)
    )
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("greeting", "large_green_square 0.75", "large_green_square 0.75"),
        ("todo-app", "large_green_square 0.75", "large_green_square 0.75"),
    )
    assert read_failed_trials(message) == ()
    assert read_judge_table(message) == (
        ("config", "case", "reward", "conciseness", "state"),
        ("default", "greeting", "0.75", "8", "ok"),
        ("default", "todo-app", "0.75", "8", "ok"),
        ("haiku", "greeting", "0.75", "8", "ok"),
        ("haiku", "todo-app", "0.75", "8", "ok"),
    )
    assert read_judge_legend(message) == "_criteria by dimension:_ quality: conciseness"
    assert read_details(message) == ""
    assert read_blocks(message, "context")[0]["elements"][0]["text"].startswith("_reward_ :large_red_square: `<0.25`")
    assert read_blocks(message, "context")[-1]["elements"][0]["text"] == (
        "<{url}|run logs> | <{url}#artifacts|artifacts>".format(url=RUN_URL)
    )


def test_render_slack_report_posts_one_message_per_pair(tmp_path: Path) -> None:
    """The two pairs answer different questions -- what we are about to ship, and what users are
    running -- and a reader acts on one at a time, so neither is a paragraph of the other's."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("released", "haiku", CellDecision.RUN)],
    )
    for pair_name in ("main", "released"):
        write_passing_oracle(summaries_dir, tmp_path, pair_name)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_summary(
        live_summary_path(summaries_dir, "released", "haiku"),
        tmp_path / "released-haiku-live",
        harness_config=WRONG_MODEL_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main -- passed",
        "minds-evals: released -- failed",
    ]
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75"))
    assert read_grid(messages[1]) == (("case", "haiku"), ("todo-app", "large_green_square 0.75\u00a0\u2717"))


def test_render_slack_report_names_a_failing_cells_reason_in_the_failures_table(tmp_path: Path) -> None:
    """The grid says which arm fell over on which case; the failures table says what to go and look
    at, and the pair-level details block is left with nothing to repeat."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        test_state="crashed",
    )
    write_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
        errored_entry_ids=("todo-app__first_message",),
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main -- failed"
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("todo-app", "large_green_square 0.75\u00a0\u2717", "large_green_square 0.75\u00a0\u2717"),
    )
    assert read_failed_trials(message) == (
        ("config", "case", "note"),
        ("default", "todo-app", "did not complete (the conversation ended in state 'crashed')"),
        ("haiku", "todo-app", "unmeasured evidence: todo-app__first_message"),
    )
    assert read_details(message) == ""


def test_render_slack_report_fails_a_cell_whose_trial_answered_on_another_model(tmp_path: Path) -> None:
    """The point of running a matrix of arms: a cell that did not run on the model it named has
    measured nothing about that model, and must not be reported as a green arm."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "haiku", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=WRONG_MODEL_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_grid(message) == (("case", "haiku"), ("todo-app", "large_green_square 0.75\u00a0\u2717"))
    assert read_failed_trials(message) == (
        ("config", "case", "note"),
        ("haiku", "todo-app", "the run asked for haiku but the trial answered on claude-opus-5"),
    )
    # The failure is named once, in the trial's reason; a failing trial gets no confirmation line.
    assert "unconfirmed" not in message.text


def test_render_slack_report_names_a_passing_arm_whose_model_nothing_confirmed(tmp_path: Path) -> None:
    """A green cell that asked for a model and got no confirmation is still green -- null is silence,
    not a wrong model -- but the reader has to be told, or the grid reads as an arm measured on the
    model it names."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "haiku", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=UNCONFIRMED_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main -- passed"
    assert read_grid(message) == (("case", "haiku"), ("todo-app", "large_green_square 0.75"))
    # Nothing failed, so the failures table is not drawn and the note has nowhere else to go: the
    # details block is what carries it, naming the arm it belongs to.
    assert read_failed_trials(message) == ()
    assert read_details(message) == "*haiku* `todo-app`: model haiku unconfirmed"


def test_render_slack_report_marks_a_green_cell_of_a_running_pair_as_not_attempted(tmp_path: Path) -> None:
    """The ordinary nightly once a matrix has settled: the pair runs because one arm moved, and its
    other arms are already green. A skipped cell keeps its column, so the reader can see it was not
    measured tonight rather than see it disappear or be counted as a pass of tonight's."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.SKIP), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main -- passed"
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("todo-app", "heavy_minus_sign", "large_green_square 0.75"),
    )
    # The skipped cell graded nothing, so it has no row in the judge table and the details block is
    # what says why its column is empty.
    assert [row[0] for row in read_judge_table(message)[1:]] == ["haiku"]
    assert read_details(message) == "*default* -- skipped (already green)"


def test_render_slack_report_marks_a_cell_whose_summary_never_arrived_as_unknown(tmp_path: Path) -> None:
    """A cell that graded nothing says nothing about its arm, which is a different thing from an arm
    that was deliberately not run: the grid tells the two apart."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert read_header(message) == "minds-evals: main -- broken"
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("todo-app", "large_green_square 0.75", "grey_question"),
    )
    assert read_details(message) == "*haiku* -- broken (no live summary; the job failed before grading)"


def test_render_slack_report_marks_a_case_a_cell_never_ran_as_unknown(tmp_path: Path) -> None:
    """The grid's rows are the union of every column's cases, so a cell that graded fewer of them
    has a hole rather than a verdict it never reached."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_summary(
        live_summary_path(summaries_dir, "main", "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("greeting", "large_green_square 0.75", "grey_question"),
        ("todo-app", "large_green_square 0.75", "large_green_square 0.75"),
    )
    # The judge table has a row per trial that was actually graded, so the case the haiku cell never
    # ran is absent from it rather than carried as a hole the way the grid has to carry it.
    assert read_judge_table(message) == (
        ("config", "case", "reward", "conciseness", "state"),
        ("default", "greeting", "0.75", "8", "ok"),
        ("default", "todo-app", "0.75", "8", "ok"),
        ("haiku", "todo-app", "0.75", "8", "ok"),
    )


def test_render_slack_report_marks_the_cells_of_a_pair_whose_oracle_failed_as_not_attempted(
    tmp_path: Path,
) -> None:
    """The oracle gates the live passes, so a cell downstream of a red oracle never ran. Reporting it
    as a missing summary would name a pass that was never started."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_summary(
        oracle_summary_path(summaries_dir, "main"),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main -- failed"
    assert read_sections(message)[0].startswith(":x: {} -- oracle failed".format(PAIR_LABEL))
    # No cell graded anything, so there is no grid to draw; the oracle's own trial is the story.
    assert read_grid(message) == ()
    assert read_details(message).splitlines() == [
        "*default* -- not evaluated (the oracle pass did not pass)",
        "*haiku* -- not evaluated (the oracle pass did not pass)",
        "*oracle* `todo-app`: gates failed",
    ]


def test_render_slack_report_reports_a_skipped_pair_without_a_grid(tmp_path: Path) -> None:
    """A night where nothing moved. Both paid jobs are gated off, so GitHub reports them `skipped`,
    and the message has to read that as a healthy pair rather than as a job that did not do its
    work."""
    matrix_path = tmp_path / "matrix.json"
    write_matrix(
        matrix_path, [make_pair("released", PairDecision.SKIP)], [make_cell("released", "default", CellDecision.SKIP)]
    )

    (message,) = render_slack_report(
        matrix_path,
        tmp_path / "summaries",
        make_context(oracle_result="skipped", evaluate_result="skipped"),
    )

    assert read_header(message) == "minds-evals: released -- skipped (already green)"
    assert read_sections(message) == (
        ":white_check_mark: {}\nskipped (already green) in 41m20s, _trigger=_ `schedule`".format(PAIR_LABEL),
    )
    assert read_grid(message) == ()


def test_render_slack_report_reports_an_unresolved_pair_without_pretending_it_has_shas(
    tmp_path: Path,
) -> None:
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path, [make_pair("released", PairDecision.UNRESOLVED)], [])

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context())

    assert read_header(message) == "minds-evals: released -- not evaluated"
    assert read_sections(message) == (
        ":x: [_mngr_ `main` (`unresolved`) | _dwt_ `main` (`unresolved`)] -- a ref did not resolve"
        "\nnot evaluated in 41m20s, _trigger=_ `schedule`",
    )
    assert read_grid(message) == ()


def test_render_slack_report_gives_an_oracle_only_run_a_single_oracle_column(tmp_path: Path) -> None:
    """A run that stops after the oracle passes has cells in its matrix that never ran; putting a
    column on them would claim a live pass nobody paid for, so the oracle is the grid."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)

    # The evaluate job is gated off on such a run, so GitHub reports it `skipped`.
    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: main -- passed"
    assert read_sections(message)[0].endswith("passed in 41m20s, _trigger=_ `schedule` _(oracle only)_")
    assert read_grid(message) == (("case", "oracle"), ("todo-app", "large_green_square 0.75"))
    assert read_judge_table(message) == (
        ("config", "case", "reward", "conciseness", "state"),
        ("oracle", "todo-app", "0.75", "8", "ok"),
    )


def test_render_slack_report_gives_a_failed_oracle_only_run_its_reason_in_the_failures_table(
    tmp_path: Path,
) -> None:
    """The oracle is the grid on such a run, so it is also a graded column -- and a graded column's
    failing trials are rows of the failures table rather than lines of the pair-level block."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_summary(
        oracle_summary_path(summaries_dir, "main"),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )

    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: main -- failed"
    assert read_sections(message)[0].startswith(":x: {} -- oracle failed".format(PAIR_LABEL))
    assert read_grid(message) == (("case", "oracle"), ("todo-app", "large_green_square 0.75\u00a0\u2717"))
    assert read_failed_trials(message) == (
        ("config", "case", "note"),
        ("oracle", "todo-app", "gates failed"),
    )
    assert read_details(message) == ""


def test_render_slack_report_tells_an_unreadable_summary_from_a_missing_one(tmp_path: Path) -> None:
    """A summary that is there but broken is a pass that got as far as grading, which is a different
    place to look than a pass that never wrote anything."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    truncated_path = live_summary_path(summaries_dir, "main", "default")
    truncated_path.parent.mkdir(parents=True, exist_ok=True)
    truncated_path.write_text('{"job_name": "main-default-live", "trials": [{"trial_name"')

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_details(message) == "*default* -- broken (the live summary could not be read)"
    assert "no live summary" not in message.text


def test_render_slack_report_reports_an_unreadable_cell_summary_even_when_the_oracle_failed(
    tmp_path: Path,
) -> None:
    """A cell that wrote a summary got further than a red oracle allows, so its own broken artifact
    is the thing to report rather than the oracle's story."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_summary(
        oracle_summary_path(summaries_dir, "main"),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )
    truncated_path = live_summary_path(summaries_dir, "main", "default")
    truncated_path.parent.mkdir(parents=True, exist_ok=True)
    truncated_path.write_text("{not json at all")

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert "*default* -- broken (the live summary could not be read)" in read_details(message)
    assert "not evaluated (the oracle pass did not pass)" not in message.text


def test_render_slack_report_says_a_pair_that_wrote_no_oracle_summary_is_broken(tmp_path: Path) -> None:
    """The pair's job died before grading. Nothing about the pair was measured, so the message has to
    read as an absence rather than as an oracle that ran and failed."""
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context())

    assert read_header(message) == "minds-evals: main -- broken"
    assert "-- broken (no oracle summary; the job failed before grading)" in read_sections(message)[0]
    assert read_details(message) == "*default* -- not evaluated (the oracle pass did not pass)"


def test_render_slack_report_tells_an_unreadable_oracle_summary_from_a_missing_one(tmp_path: Path) -> None:
    """A summary that cannot be read means the pass got as far as grading and left something broken
    behind, which sends the reader somewhere else entirely."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [])
    summary_path = oracle_summary_path(summaries_dir, "main")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("{not json at all")

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert "-- broken (the oracle summary could not be read)" in read_sections(message)[0]


def test_render_slack_report_reports_a_run_that_never_decided_what_to_evaluate(tmp_path: Path) -> None:
    (message,) = render_slack_report(
        tmp_path / "absent-matrix.json",
        tmp_path / "summaries",
        make_context(duration_seconds=None, resolve_result="failure", oracle_result="", evaluate_result="skipped"),
    )

    assert read_header(message) == "minds-evals -- broken"
    assert read_sections(message) == (
        ":x: no pairs were resolved (resolve=failure, oracle=, evaluate=skipped)"
        " -- the run broke before deciding what to evaluate",
    )
    assert read_grid(message) == ()
    assert message.text.endswith("<{url}|run logs> | <{url}#artifacts|artifacts>".format(url=RUN_URL))


def test_render_slack_report_reports_a_matrix_that_decided_no_pairs_as_broken(tmp_path: Path) -> None:
    """A matrix that parses but names no pair is a run that got as far as deciding and decided
    nothing, which is a break rather than a night with nothing to do: the freeze step always writes
    at least one pair."""
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path, [], [])

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context())

    assert read_header(message) == "minds-evals -- broken"
    assert "no pairs were resolved" in read_sections(message)[0]


def test_render_slack_report_reports_a_matrix_file_it_cannot_read_the_same_way(tmp_path: Path) -> None:
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text('{"config": "x", "pairs": [')

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context(resolve_result="cancelled"))

    assert "no pairs were resolved (resolve=cancelled" in message.text


def test_render_slack_report_warns_when_every_arm_passed_but_a_job_did_not(tmp_path: Path) -> None:
    """Cleanup and upload run whatever the trials did, so a job can be red with every arm green. The
    warning names the job, and only fires when nothing else already explains the red run."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(live_summary_path(summaries_dir, "main", "default"), tmp_path / "main-default-live")

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert read_header(message) == "minds-evals: main -- passed"
    assert read_sections(message)[0] == (
        ":warning: {} -- oracle passed\npassed in 41m20s, _trigger=_ `schedule`"
        " _every arm passed, but the job did not (evaluate=failure; check cleanup and uploads)_".format(PAIR_LABEL)
    )


def test_render_slack_report_does_not_blame_the_job_when_an_arm_already_explains_the_red_run(
    tmp_path: Path,
) -> None:
    """A red evaluate job with a failing cell under it needs no second explanation, and pointing at
    cleanup and uploads on every red run would train the reader to ignore that warning."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("released", "default", CellDecision.RUN)],
    )
    for pair_name in ("main", "released"):
        write_passing_oracle(summaries_dir, tmp_path, pair_name)
    write_summary(live_summary_path(summaries_dir, "main", "default"), tmp_path / "main-default-live")
    write_summary(
        live_summary_path(summaries_dir, "released", "default"),
        tmp_path / "released-default-live",
        test_state="crashed",
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    # The green pair's own message stays quiet about a job the other pair's failure accounts for.
    assert "check cleanup and uploads" not in messages[0].text
    assert "check cleanup and uploads" not in messages[1].text


def test_render_slack_report_names_every_job_of_a_red_run_that_nothing_else_explains(
    tmp_path: Path,
) -> None:
    """More than one job can go red at once -- a cancelled run takes them all -- and the note is
    what says which, so it lists them rather than naming the first."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(live_summary_path(summaries_dir, "main", "default"), tmp_path / "main-default-live")

    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(oracle_result="cancelled", evaluate_result="failure")
    )

    assert "(oracle=cancelled, evaluate=failure; check cleanup and uploads)" in read_sections(message)[0]


def test_render_slack_report_marks_a_trial_that_was_never_graded_without_a_reward(tmp_path: Path) -> None:
    """A trial that died before the verifier ran has no reward to print, and a bare `FAIL` says that
    -- printing a zero would read as a graded run that scored nothing."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_trial_dir(tmp_path / "main-default-live", "todo-app__aaaaaaa", exception_type="TimeoutError")
    write_run_check_reports(
        check_job_directory(tmp_path / "main-default-live"),
        None,
        live_summary_path(summaries_dir, "main", "default"),
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_grid(message) == (("case", "default"), ("todo-app", "grey_question\u00a0\u2717"))
    table = read_judge_table(message)
    assert table[0] == ("config", "case", "reward", "state")
    assert table[1][:3] == ("default", "todo-app", "-")


def test_render_slack_report_builds_a_grid_cell_out_of_a_band_and_a_reward(tmp_path: Path) -> None:
    """A cell is the reward's band as a coloured square, the reward, and then the mark that says
    whether the trial passed -- so the colour means one thing throughout, and the verdict never
    competes with it for the reader's eye.

    A passing trial carries a spacer where a failing one carries its mark, so that the
    right-aligned rewards stay in line.
    """
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.SKIP)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    grid = read_blocks(message, "table")[0]
    assert grid["rows"][0] == [
        {"type": "raw_text", "text": "case"},
        make_bold_cell("default"),
        make_bold_cell("haiku"),
    ]
    case_cell, graded_cell, skipped_cell = grid["rows"][1]
    assert case_cell == {"type": "raw_text", "text": "todo-app"}
    assert graded_cell == {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "emoji", "name": "large_green_square"},
                    {"type": "text", "text": " 0.75"},
                    {"type": "text", "text": "\u00a0\u2007", "style": {"bold": True}},
                ],
            }
        ],
    }
    assert skipped_cell == {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "emoji", "name": "heavy_minus_sign"}]}],
    }


def test_render_slack_report_names_the_dimension_beside_each_judge_criterion(tmp_path: Path) -> None:
    """A criterion's name alone does not say what it measured: two dimensions can score criteria of
    the same name, and the dimension is what says whether a score is about the product or about the
    harness that drove it. The columns are the union across the cell's cases, in first-seen order, so
    a case the judges scored differently has holes rather than zeros."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", "default"),
        [
            make_graded_trial(
                "greeting",
                [make_judge_score("quality", "conciseness", 9.0), make_judge_score("outcome", "conciseness", 7.0)],
            ),
            make_graded_trial(
                "todo-app",
                [
                    make_judge_score("quality", "conciseness", 6.0),
                    make_judge_score("harness_quality", "main_harness", 10.0),
                ],
                reward=0.5,
            ),
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    # `conciseness` is scored under two dimensions, so those two columns carry theirs in their
    # headings; `main_harness` is scored under one, so its heading stays bare.
    assert read_judge_table(message) == (
        ("config", "case", "reward", "quality: conciseness", "outcome: conciseness", "main_harness", "state"),
        ("default", "greeting", "0.75", "9", "7", "-", "ok"),
        ("default", "todo-app", "0.50", "6", "-", "10", "ok"),
    )
    assert read_judge_legend(message) == (
        "_criteria by dimension:_ quality: conciseness; outcome: conciseness; harness_quality: main_harness"
    )


@pytest.mark.parametrize(
    ("criteria_count", "expected_overflow_line"),
    [
        # Exactly the criteria a row has room for: nothing is dropped, and nothing is announced.
        (SLACK_TABLE_CELL_LIMIT - 4, ""),
        (25, "_showing 16 of 25 judge criteria; the rest are in the run's summary_"),
    ],
)
def test_render_slack_report_fills_a_judge_table_row_and_says_when_columns_did_not_fit(
    tmp_path: Path, criteria_count: int, expected_overflow_line: str
) -> None:
    """Slack refuses a table whose row is over the cap, and the whole message with it, so the columns
    that do not fit go rather than the table -- and the legend above it says so, since a table that
    quietly lost columns reads as the whole of what the judges scored. A pair scored on exactly what
    fits loses nothing, and must not say it did."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", "default"),
        [
            make_graded_trial(
                "todo-app",
                [make_judge_score("quality", "criterion-{}".format(index), 8.0) for index in range(criteria_count)],
            )
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    table = read_judge_table(message)
    assert [len(row) for row in table] == [SLACK_TABLE_CELL_LIMIT, SLACK_TABLE_CELL_LIMIT]
    assert table[0][-2:] == ("criterion-15", "state")
    overflow = read_judge_legend(message).partition("\n")[2]
    assert overflow == expected_overflow_line


def test_render_slack_report_states_the_dimensions_above_the_judge_table_rather_than_in_it(
    tmp_path: Path,
) -> None:
    """A criterion's dimension is stated once, above the table, in the blocks and in the fence
    alike. Qualifying every heading makes the columns far wider than the numbers under them, and a
    fence wider than a narrow client wraps is no longer a table at all."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", "default"),
        [
            make_graded_trial(
                "todo-app",
                [
                    make_judge_score("harness_quality", "main_harness_success", 10.0),
                    make_judge_score("outcome", "works_as_expected", 9.0),
                    make_judge_score("outcome", "no_placeholder_content", 8.0),
                ],
            )
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    # No criterion name is scored under two dimensions here, so every heading stays bare.
    assert read_judge_table(message)[0] == (
        "config",
        "case",
        "reward",
        "main_harness_success",
        "works_as_expected",
        "no_placeholder_content",
        "state",
    )
    legend = (
        "_criteria by dimension:_ harness_quality: main_harness_success;"
        " outcome: works_as_expected, no_placeholder_content"
    )
    assert read_judge_legend(message) == legend
    # The fence carries the same legend and the same bare headings.
    fence = message.text.partition(legend + "\n")[2].splitlines()
    assert fence[:3] == [
        "```",
        "config   case      reward  main_harness_success  works_as_expected  no_placeholder_content  state",
        "default  todo-app  0.75    10                    9                  8                       ok",
    ]


def test_format_budgeted_block_cuts_one_enormous_line_inside_the_budget() -> None:
    """The only line the budget cuts mid-word, because a block of one line still has to say
    something about it -- and the truncation notice comes out of the budget rather than on top of
    it, so what a section is handed is never over."""
    block = format_budgeted_block(["x" * (DETAIL_BUDGET * 2)])

    assert len(block) == DETAIL_BUDGET
    assert block.endswith("\n... (truncated; see the run summary)")


def test_render_slack_report_keeps_a_huge_judge_table_inside_slacks_character_budget(
    tmp_path: Path,
) -> None:
    """Slack caps a message at 10000 characters across the cells of all its tables and refuses the
    whole message past it, so a pair with more graded trials than fit loses rows rather than blocks.

    Whole rows go, or the scores that survived would line up under the wrong headings, and the
    legend above the table says how many were dropped -- a table that quietly lost rows reads as
    every trial the run graded.
    """
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", "default"),
        [
            make_graded_trial(
                "case-{}-{}".format(index, "x" * 300),
                [make_judge_score("quality", "criterion-{}".format(score), 8.0) for score in range(16)],
            )
            for index in range(60)
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    table = read_judge_table(message)
    assert sum(len(cell) for row in table for cell in row) <= SLACK_TABLE_CHARACTER_LIMIT
    # Rows were dropped, and every row that survived is whole.
    assert 0 < len(table) - 1 < 60
    assert {len(row) for row in table} == {SLACK_TABLE_CELL_LIMIT}
    assert read_judge_legend(message).splitlines()[-1] == (
        "_showing {} of 60 graded trials; the rest are in the run's summary_".format(len(table) - 1)
    )
    # No single cell may run away with the budget either, so the enormous case ids are cut too.
    assert [row[1] for row in table[1:] if len(row[1]) > SLACK_TABLE_CELL_CHARACTER_LIMIT] == []
    assert table[1][1].endswith("...")


def test_render_slack_report_says_so_when_no_judge_row_fits_at_all(tmp_path: Path) -> None:
    """A budget that leaves room for no row leaves no table to say so in, so the message says it
    where the table would have gone -- otherwise a pair whose scores did not fit reads as a pair the
    judges never scored."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    # Enough cases that the grid alone spends the message's whole table budget.
    write_model_summary(
        live_summary_path(summaries_dir, "main", "default"),
        [
            make_graded_trial(
                "case-{}-{}".format(index, "x" * 300),
                [make_judge_score("quality", "conciseness", 8.0)],
            )
            for index in range(90)
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    assert read_judge_table(message) == ()
    notice = "_the judge scores did not fit this message; they are in the run's summary_"
    assert notice in [element["text"] for block in read_blocks(message, "context") for element in block["elements"]]
    assert notice in message.text


def test_render_slack_report_truncates_a_long_details_block(tmp_path: Path) -> None:
    """A Slack section is capped at 3000 characters and refused past it. A failed oracle is what
    fills the details block: the oracle has no column of its own once a pair has cells, so every one
    of its reasons lands there."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    oracle_job_dir = tmp_path / "main-oracle"
    for index in range(12):
        write_trial_dir(
            oracle_job_dir,
            "trial-{}".format(index),
            case_id="todo-app-" + "x" * 200,
            test_state="crashed",
            is_environment_recorded=False,
        )
    write_run_check_reports(check_job_directory(oracle_job_dir), None, oracle_summary_path(summaries_dir, "main"))

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    details = read_details(message)
    assert len(details) <= DETAIL_BUDGET
    lines = details.splitlines()
    assert lines[0] == "*default* -- not evaluated (the oracle pass did not pass)"
    assert lines[-1] == "... (truncated; see the run summary)"
    assert 0 < len(lines) - 2 < 12
    assert all(line.startswith("*oracle* `todo-app-") for line in lines[1:-1])


def test_as_slack_payload_posts_as_the_run_and_carries_the_whole_report_as_text(tmp_path: Path) -> None:
    """The webhook carries no identity of its own, and Slack shows `text` wherever the blocks cannot
    be rendered -- including the retry the workflow posts if a block type is refused -- so the text
    has to be the whole report rather than a caption for it."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.SKIP)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())
    payload = as_slack_payload(message)

    assert payload["username"] == SLACK_USERNAME
    assert payload["icon_emoji"] == GREEN_ICON_EMOJI
    assert payload["text"] == message.text
    assert [block["type"] for block in payload["blocks"]] == [
        "header",
        "section",
        "table",
        "context",
        # Nothing failed, so no failures table stands between the grid and the collapsed scores.
        "container",
        "section",
        "context",
    ]
    # The grid keeps its words rather than its emoji here: Slack renders no emoji inside a fence.
    assert message.text.splitlines() == [
        ":white_check_mark: *minds-evals: main -- passed*",
        ":white_check_mark: {} -- oracle passed".format(PAIR_LABEL),
        "passed in 41m20s, _trigger=_ `schedule`",
        "```",
        "case      default  haiku",
        "greeting  ok 0.75  -",
        "todo-app  ok 0.75  -",
        "```",
        "_criteria by dimension:_ quality: conciseness",
        "```",
        "config   case      reward  conciseness  state",
        "default  greeting  0.75    8            ok",
        "default  todo-app  0.75    8            ok",
        "```",
        "*details*",
        "*haiku* -- skipped (already green)",
        "<{url}|run logs> | <{url}#artifacts|artifacts>".format(url=RUN_URL),
    ]


def test_as_slack_payload_posts_a_green_pair_under_the_green_icon(tmp_path: Path) -> None:
    """The avatar is the verdict in a channel list, before anyone opens the message. A pair whose
    every cell was skipped is green too: it was verified, just not tonight."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.SKIP)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("released", "default", CellDecision.SKIP)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main -- passed",
        "minds-evals: released -- skipped (already green)",
    ]
    assert [as_slack_payload(message)["icon_emoji"] for message in messages] == [
        GREEN_ICON_EMOJI,
        GREEN_ICON_EMOJI,
    ]


def test_as_slack_payload_posts_an_oracle_only_run_whose_oracle_passed_under_the_green_icon(
    tmp_path: Path,
) -> None:
    """Such a run pays for no cell, so its oracle is the whole of what it measured -- and a passing
    one is a green run rather than an unfinished one."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)

    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: main -- passed"
    assert as_slack_payload(message)["icon_emoji"] == GREEN_ICON_EMOJI


def test_as_slack_payload_posts_anything_that_wants_reading_under_the_ungreen_icon(tmp_path: Path) -> None:
    """A measured shortfall, an arm nothing was attempted on, and a run that decided nothing at all
    are each something a reader has to act on, and the avatar says so for all three."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.UNRESOLVED)],
        [make_cell("main", "default", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=WRONG_MODEL_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())
    (undecided,) = render_slack_report(None, summaries_dir, make_context(resolve_result="failure"))

    assert [read_header(message) for message in messages] == [
        "minds-evals: main -- failed",
        "minds-evals: released -- not evaluated",
    ]
    assert [as_slack_payload(message)["icon_emoji"] for message in (*messages, undecided)] == [
        UNGREEN_ICON_EMOJI,
        UNGREEN_ICON_EMOJI,
        UNGREEN_ICON_EMOJI,
    ]


def test_as_slack_payload_posts_a_run_whose_arms_all_passed_but_whose_job_did_not_under_the_green_icon(
    tmp_path: Path,
) -> None:
    """The icon answers "are the arms good?", which a cleanup step that could not delete a Modal
    environment does not change -- the message's own warning is what says the job went red."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert ":warning:" in read_sections(message)[0]
    assert as_slack_payload(message)["icon_emoji"] == GREEN_ICON_EMOJI


def test_parse_run_check_reads_back_the_summary_check_run_wrote(tmp_path: Path) -> None:
    """`check-run` dumps a model whose verdict fields are computed, and computed fields are extras on
    the way back in, so the dump does not validate as-is."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", harness_config=HAIKU_HARNESS_CONFIG)
    run_check = check_job_directory(job_dir)
    summary_path = tmp_path / "live-summary.json"
    write_run_check_reports(run_check, None, summary_path)
    assert '"is_passed"' in summary_path.read_text()

    parsed = parse_run_check(summary_path.read_text())

    assert parsed == run_check
    assert parsed.is_passed is True
    assert parsed.trials[0].requested_model == "haiku"


def _nested_model_types(model: type[BaseModel]) -> set[type[BaseModel]]:
    """Every model reachable from one field hop of this one, through a container or a union."""
    reachable: set[type[BaseModel]] = set()
    for field in model.model_fields.values():
        for annotation in (field.annotation, *get_args(field.annotation)):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                reachable.add(annotation)
    return reachable


def test_only_the_levels_parse_run_check_strips_carry_computed_fields() -> None:
    """`parse_run_check` strips computed fields from the run and from each trial, which is every
    level of the summary as it stands. A computed field one level deeper would make every summary
    read as one that could not be read -- the failure the report must never show for a bug of its
    own -- and a new model nested under RunCheck would put a whole unstripped level between them.
    Growing the parsing by a level is the fix; this is what says a level has appeared."""
    assert _nested_model_types(RunCheck) == {TrialCheck}

    deeper_models = _nested_model_types(TrialCheck)

    assert deeper_models == {JudgeScore}
    assert [model for model in deeper_models if model.model_computed_fields] == []


def test_format_duration_reads_as_a_wall_clock() -> None:
    assert format_duration(None) == "an unknown time"
    assert format_duration(41) == "41s"
    assert format_duration(61) == "1m01s"
    assert format_duration(2480) == "41m20s"


def test_ci_report_writes_one_payload_per_message_and_exits_zero_without_a_matrix(tmp_path: Path) -> None:
    """The notification is the whole report of a nightly, so a run broken enough to have decided
    nothing still gets a message rather than a failing job. The workflow posts the file as it
    stands, one payload at a time, so it is an array whatever the run decided."""
    output_path = tmp_path / "reports" / "slack-payloads.json"

    result = CliRunner().invoke(
        main,
        [
            "ci-report",
            "--matrix",
            str(tmp_path / "absent-matrix.json"),
            "--summaries-dir",
            str(tmp_path / "summaries"),
            "--run-url",
            RUN_URL,
            "--trigger",
            "schedule",
            "--resolve-result",
            "failure",
            "--evaluate-result",
            "skipped",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payloads = json.loads(output_path.read_text())
    assert len(payloads) == 1
    assert payloads[0]["blocks"][0]["text"]["text"] == "minds-evals -- broken"
    assert "the run broke before deciding what to evaluate" in payloads[0]["text"]


def test_format_ref_label_does_not_repeat_a_ref_that_is_already_the_sha() -> None:
    """`mngr_ref` accepts a full SHA, and a pair frozen from one would otherwise print the same
    hex twice."""
    assert format_ref_label("main", FROZEN_SHA) == "`main` (`abcdef123456`)"
    assert format_ref_label(FROZEN_SHA, FROZEN_SHA) == "`abcdef123456`"
    assert format_ref_label("minds-v9.9.9", "") == "`minds-v9.9.9` (`unresolved`)"
    # A branch name is held to no length, and one that is as long as a SHA is still a name.
    forty_character_ref = "release/" + "x" * 32
    assert format_ref_label(forty_character_ref, FROZEN_SHA) == "`{}` (`abcdef123456`)".format(forty_character_ref)


def test_the_summary_file_names_the_report_composes_are_the_ones_the_workflow_writes() -> None:
    """The report finds a pass's summary by composing its file name from the matrix, never by
    discovering what is on disk, so a rename on either side is silent: the report simply says every
    pass is broken. The workflow cannot import these constants, so this reads the check-run output
    paths and the download pattern back out of it."""
    workflow_text = read_scheduled_workflow_text()

    assert '--summary-json "$SUMMARY_DIR/{}-$PAIR.json"'.format(ORACLE_SUMMARY_STEM) in workflow_text
    assert '--summary-json "$SUMMARY_DIR/{}-$PAIR-$HARNESS_CONFIG.json"'.format(LIVE_SUMMARY_STEM) in workflow_text
    assert "pattern: {}*\n".format(SUMMARY_ARTIFACT_PREFIX) in workflow_text


def test_the_notify_job_merges_the_summary_artifacts_into_one_directory() -> None:
    """The paths above are flat, which download-artifact only guarantees under `merge-multiple`: on
    its own it gives each artifact a directory of its own, except when exactly one matches the
    pattern -- an oracle-only run on one pair -- which it extracts flat instead. Either layout the
    report did not expect makes it call a passing run broken.

    Read inside the step that carries the summary pattern, because the workflow has a second
    download step (a cell fetching its pair's oracle summary) whose own `merge-multiple` would
    satisfy a bare search of the file while this one had gone.
    """
    workflow_text = read_scheduled_workflow_text()

    download_step = workflow_text.partition("pattern: {}*\n".format(SUMMARY_ARTIFACT_PREFIX))[2]

    assert download_step, "no summary download step in {}".format(SCHEDULED_WORKFLOW_PATH)
    assert "merge-multiple: true" in download_step.partition("\n      - ")[0]


def test_the_notify_job_posts_every_payload_the_report_writes() -> None:
    """`ci-report` writes an array of webhook payloads, one per pair, and each is a whole message:
    posting only the first, or the array itself, would drop a pair's report on the floor."""
    workflow_text = read_scheduled_workflow_text()

    assert "jq -c '.[]' /tmp/slack-payloads.json > /tmp/slack-payloads.jsonl" in workflow_text
    assert "done < /tmp/slack-payloads.jsonl" in workflow_text


def test_the_notify_job_falls_back_to_a_rejected_messages_own_text() -> None:
    """A payload Slack refuses is most likely one carrying a block type the workspace cannot render.
    Its `text` says everything its blocks do, so the message is posted rather than lost, and the
    warning says which of the two happened."""
    workflow_text = read_scheduled_workflow_text()

    assert "jq '{username, icon_emoji, text}' /tmp/slack-payload.json > /tmp/slack-fallback.json" in workflow_text
    assert "::warning::Slack rejected the report's blocks" in workflow_text
