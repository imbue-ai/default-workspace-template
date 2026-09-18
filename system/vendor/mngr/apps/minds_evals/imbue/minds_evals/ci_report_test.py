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

from imbue.imbue_common.primitives import PositiveInt
from imbue.minds_evals.check_run import check_job_directory
from imbue.minds_evals.check_run import write_run_check_reports
from imbue.minds_evals.ci_matrix import config_slug
from imbue.minds_evals.ci_report import DETAIL_BUDGET
from imbue.minds_evals.ci_report import DIAGNOSE_BEHAVIOUR_SUMMARY_STEM
from imbue.minds_evals.ci_report import DIAGNOSE_FIXTURE_SUMMARY_STEM
from imbue.minds_evals.ci_report import GREEN_ICON_EMOJI
from imbue.minds_evals.ci_report import SLACK_USERNAME
from imbue.minds_evals.ci_report import SUMMARY_ARTIFACT_PREFIX
from imbue.minds_evals.ci_report import SlackMessage
from imbue.minds_evals.ci_report import UNGREEN_ICON_EMOJI
from imbue.minds_evals.ci_report import as_slack_payload
from imbue.minds_evals.ci_report import diagnose_behaviour_summary_path
from imbue.minds_evals.ci_report import diagnose_fixture_summary_path
from imbue.minds_evals.ci_report import format_budgeted_block
from imbue.minds_evals.ci_report import format_duration
from imbue.minds_evals.ci_report import format_ref_label
from imbue.minds_evals.ci_report import live_invariants_summary_path
from imbue.minds_evals.ci_report import live_summary_path
from imbue.minds_evals.ci_report import oracle_summary_path
from imbue.minds_evals.ci_report import parse_run_check
from imbue.minds_evals.ci_report import render_slack_report
from imbue.minds_evals.cli import main
from imbue.minds_evals.data_types import BehaviourDiagnosticCell
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import CiReportContext
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import DiagnosticRunCheck
from imbue.minds_evals.data_types import DiagnosticTrialCheck
from imbue.minds_evals.data_types import DiagnosticVerdict
from imbue.minds_evals.data_types import FactMatcher
from imbue.minds_evals.data_types import FactMatcherKind
from imbue.minds_evals.data_types import FactOutcome
from imbue.minds_evals.data_types import FactStatus
from imbue.minds_evals.data_types import FixtureDiagnosticCell
from imbue.minds_evals.data_types import JudgeScore
from imbue.minds_evals.data_types import KnownFailure
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.data_types import UnsupportedDiagnosticCell
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import read_scheduled_workflow_text
from imbue.minds_evals.testing import write_trial_dir

RUN_URL: Final[str] = "https://github.com/imbue-ai/mngr-internal/actions/runs/42"
EVAL_CONFIG: Final[str] = "apps/minds_evals/configs/eval-config-small.json"
# The one word every job, artifact and summary name spells that config as, derived the way the
# decided matrix derives it so the composed summary paths are the ones a real run writes.
CONFIG_SLUG: Final[str] = config_slug(EVAL_CONFIG)
# A second suite's eval config, for the runs that evaluate more than one.
SECOND_EVAL_CONFIG: Final[str] = "apps/minds_evals/configs/eval-config-time-to-mock.json"
SECOND_CONFIG_SLUG: Final[str] = config_slug(SECOND_EVAL_CONFIG)
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
# The same silence, on a lane that can never be anything else: mngr's codex transcript emitter names
# no model on a step, so every trial of a codex arm records null however well its switch went.
CODEX_HARNESS_CONFIG: Final[Mapping[str, Any]] = {"lane": "openai", "model": "gpt-5.6-sol", "effort": "low"}


def make_pair(pair_name: str, decision: PairDecision) -> DecidedPair:
    sha = "" if decision is PairDecision.UNRESOLVED else FROZEN_SHA
    return DecidedPair(pair=pair_name, mngr_ref="main", mngr_sha=sha, dwt_ref="main", dwt_sha=sha, decision=decision)


def make_cell(pair_name: str, harness_config: str, decision: CellDecision, config: str = EVAL_CONFIG) -> MatrixCell:
    """One cell of the decided matrix: a pair, one suite's eval config, and one harness config."""
    return MatrixCell(
        pair=pair_name,
        harness_config=harness_config,
        mngr_ref="main",
        mngr_sha=FROZEN_SHA,
        dwt_ref="main",
        dwt_sha=FROZEN_SHA,
        config=config,
        config_slug=config_slug(config),
        attempts=PositiveInt(1),
        lane_key_env="ANTHROPIC_API_KEY",
        harbor_args="[]",
        cache_key="minds-evals-green-{}-{}-{}".format(pair_name, config_slug(config), harness_config),
        decision=decision,
    )


def make_fixture_diagnostic(pair_name: str) -> FixtureDiagnosticCell:
    return FixtureDiagnosticCell(
        pair=pair_name,
        mngr_ref="main",
        mngr_sha=FROZEN_SHA,
        dwt_ref="main",
        dwt_sha=FROZEN_SHA,
        lane_key_env="ANTHROPIC_API_KEY",
        harbor_args="[]",
    )


def make_behaviour_diagnostic(pair_name: str, harness: str) -> BehaviourDiagnosticCell:
    return BehaviourDiagnosticCell(**make_fixture_diagnostic(pair_name).model_dump(), harness=harness)


def write_matrix(
    matrix_path: Path,
    pairs: Sequence[DecidedPair],
    cells: Sequence[MatrixCell],
    *,
    diagnosed_pairs: Sequence[str] = (),
    diagnosed_harnesses: Sequence[str] = (),
    unsupported: Sequence[UnsupportedDiagnosticCell] = (),
) -> None:
    """The decided matrix exactly as `ci-matrix` writes it, computed fields and all.

    Its configs are the ones its cells name, in cell order, which is what a run's suite list leaves
    behind. A matrix with no cell at all still names the suite the run was asked to evaluate.
    """
    configs = tuple(dict.fromkeys(cell.config for cell in cells)) or (EVAL_CONFIG,)
    matrix = CiMatrix(
        configs=configs,
        pairs=tuple(pairs),
        cells=tuple(cells),
        fixture_diagnostics=tuple(make_fixture_diagnostic(pair_name) for pair_name in diagnosed_pairs),
        behaviour_diagnostics=tuple(
            make_behaviour_diagnostic(pair_name, harness)
            for pair_name in diagnosed_pairs
            for harness in diagnosed_harnesses
        ),
        unsupported_diagnostics=tuple(unsupported),
    )
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


def write_passing_oracle(
    summaries_dir: Path, job_root: Path, pair_name: str = "main", slug: str = CONFIG_SLUG
) -> RunCheck:
    """The oracle pass of one pair and eval config, graded green, where the report looks for it.

    An oracle trial replays a canned transcript and boots no workspace, so it records no Modal
    environment -- which is what `is_environment_recorded=False` says here.
    """
    return write_summary(
        oracle_summary_path(summaries_dir, pair_name, slug),
        job_root / "{}-{}-oracle".format(pair_name, slug),
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
    diagnose_fixture_result: str = "success",
    diagnose_behaviour_result: str = "success",
) -> CiReportContext:
    return CiReportContext(
        run_url=RUN_URL,
        trigger="schedule",
        duration_seconds=duration_seconds,
        is_live_pass_skipped=is_live_pass_skipped,
        resolve_result=resolve_result,
        oracle_result=oracle_result,
        evaluate_result=evaluate_result,
        diagnose_fixture_result=diagnose_fixture_result,
        diagnose_behaviour_result=diagnose_behaviour_result,
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


def make_fact_outcome(
    fact_name: str,
    status: FactStatus,
    *,
    step_name: str = "",
    recorded: Any = False,
    expected: Any = True,
    is_compliance: bool = False,
    is_health: bool = False,
    known_failure: KnownFailure | None = None,
    unmet_requirements: Sequence[str] = (),
) -> FactOutcome:
    return FactOutcome(
        fact_name=fact_name,
        step_name=step_name,
        status=status,
        is_compliance=is_compliance,
        is_health=is_health,
        recorded=recorded,
        expected=FactMatcher(kind=FactMatcherKind.EXPECTED, value=expected),
        known_failure=known_failure,
        unmet_requirements=tuple(unmet_requirements),
    )


def make_diagnostic_trial(
    verdict: DiagnosticVerdict,
    *,
    case_id: str = "behaviour",
    trial_name: str = "behaviour__aaaaaaa",
    harness: str = "codex",
    not_measured_reason: str = "",
    failed_facts: Sequence[FactOutcome] = (),
    not_recorded_facts: Sequence[FactOutcome] = (),
    known_facts: Sequence[FactOutcome] = (),
    not_followed_facts: Sequence[FactOutcome] = (),
    unmet_preconditions: Sequence[FactOutcome] = (),
    unexpectedly_passing_facts: Sequence[FactOutcome] = (),
) -> DiagnosticTrialCheck:
    return DiagnosticTrialCheck(
        trial_name=trial_name,
        case_id=case_id,
        harness=harness,
        verdict=verdict,
        not_measured_reason=not_measured_reason,
        failed_facts=tuple(failed_facts),
        not_recorded_facts=tuple(not_recorded_facts),
        known_facts=tuple(known_facts),
        not_followed_facts=tuple(not_followed_facts),
        unmet_preconditions=tuple(unmet_preconditions),
        unexpectedly_passing_facts=tuple(unexpectedly_passing_facts),
    )


def write_diagnostic_summary(summary_path: Path, trials: Sequence[DiagnosticTrialCheck]) -> None:
    """A diagnostic check's summary written straight from the model `check-diagnostics` dumps."""
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(DiagnosticRunCheck(job_name=summary_path.stem, trials=tuple(trials)).model_dump_json())


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
    """The whole shape of a good night: the pair and the eval config in the header, the commits the
    pair froze to under it, and a grid whose rows are the config's cases and whose columns are the
    harness configs it ran them on."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.RUN)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
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


def test_render_slack_report_posts_a_message_for_each_pair_of_a_one_suite_run(tmp_path: Path) -> None:
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_summary(
        live_summary_path(summaries_dir, "released", CONFIG_SLUG, "haiku"),
        tmp_path / "released-haiku-live",
        harness_config=WRONG_MODEL_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: released x eval-config-small -- failed",
    ]
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75"))
    assert read_grid(messages[1]) == (("case", "haiku"), ("todo-app", "large_green_square 0.75\u00a0\u2717"))


def test_render_slack_report_posts_a_message_for_each_eval_config_a_pair_ran(tmp_path: Path) -> None:
    """A suite's cases, arms and oracle pass are its own, so a pair that ran two of them gets two
    messages. One message over both would grade the pair on an oracle verdict that belongs to
    neither suite, and its grid would report every column as not evaluated for the other suite's
    cases."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [
            make_cell("main", "default", CellDecision.RUN),
            make_cell("main", "opus-standard", CellDecision.RUN, SECOND_EVAL_CONFIG),
        ],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_passing_oracle(summaries_dir, tmp_path, "main", SECOND_CONFIG_SLUG)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_two_case_summary(
        live_summary_path(summaries_dir, "main", SECOND_CONFIG_SLUG, "opus-standard"),
        tmp_path / "main-opus-standard-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- passed",
    ]
    # Each grid holds its own suite's arm and its own suite's cases, read out of that suite's own
    # summary files.
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75"))
    assert read_grid(messages[1]) == (
        ("case", "opus-standard"),
        ("greeting", "large_green_square 0.75"),
        ("todo-app", "large_green_square 0.75"),
    )
    assert [row[0] for row in read_judge_table(messages[1])[1:]] == ["opus-standard", "opus-standard"]


def test_render_slack_report_gates_a_cell_on_the_oracle_pass_of_its_own_eval_config(tmp_path: Path) -> None:
    """The oracle builds the box image, generates the task and grades the result, all of which are
    the eval config's own, so a pair has one oracle pass per suite. A cell reads the one its own
    suite ran: gating it on another suite's would either start a cell whose gate never passed or
    report an arm as broken that was never allowed to begin."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [
            make_cell("main", "default", CellDecision.RUN),
            make_cell("main", "opus-standard", CellDecision.RUN, SECOND_EVAL_CONFIG),
        ],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        oracle_summary_path(summaries_dir, "main", SECOND_CONFIG_SLUG),
        tmp_path / "main-time-to-mock-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- failed",
    ]
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75"))
    assert read_sections(messages[1])[0].startswith(":x: {} -- oracle failed".format(PAIR_LABEL))
    assert read_details(messages[1]).splitlines() == [
        "*opus-standard* -- not evaluated (the oracle pass did not pass)",
        "*oracle* `todo-app`: gates failed",
    ]


def test_render_slack_report_reports_a_suite_whose_every_cell_is_already_green_as_skipped(tmp_path: Path) -> None:
    """A pair runs the moment one of its arms moves, and the suites none of whose arms moved run no
    oracle pass -- the run pays for one exactly where a cell gates on it. Such a suite has to read as
    verified-but-not-tonight rather than as a pass whose summary went missing."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [
            make_cell("main", "default", CellDecision.RUN),
            make_cell("main", "opus-standard", CellDecision.SKIP, SECOND_EVAL_CONFIG),
        ],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- skipped (already green)",
    ]
    # The skipped cell keeps its column, so the reader sees an arm that was not measured tonight,
    # and nothing claims a missing oracle summary.
    assert read_grid(messages[1]) == ()
    assert read_details(messages[1]) == "*opus-standard* -- skipped (already green)"
    assert "oracle" not in messages[1].text


def test_render_slack_report_gives_a_skipped_pair_one_message_however_many_suites_it_has_cells_of(
    tmp_path: Path,
) -> None:
    """A pair whose every cell is already green was decided as a whole, not suite by suite: it ran no
    oracle pass and no cell of any suite, so one message per suite would report decisions the run
    never made."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.SKIP)],
        [
            make_cell("main", "default", CellDecision.RUN),
            make_cell("released", "default", CellDecision.SKIP),
            make_cell("released", "opus-standard", CellDecision.SKIP, SECOND_EVAL_CONFIG),
        ],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="skipped"))

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: released -- skipped (already green)",
    ]
    assert read_grid(messages[1]) == ()


def test_render_slack_report_gives_an_unresolved_pair_one_message_however_many_suites_the_run_ran(
    tmp_path: Path,
) -> None:
    """A pair whose refs did not resolve has no cells of any suite, so there is no suite to attribute
    its message to -- and multiplying it by the suites the other pair ran would report a missing
    release tag once per eval config."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN), make_pair("released", PairDecision.UNRESOLVED)],
        [
            make_cell("main", "default", CellDecision.RUN),
            make_cell("main", "opus-standard", CellDecision.RUN, SECOND_EVAL_CONFIG),
        ],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_passing_oracle(summaries_dir, tmp_path, "main", SECOND_CONFIG_SLUG)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_summary(
        live_summary_path(summaries_dir, "main", SECOND_CONFIG_SLUG, "opus-standard"),
        tmp_path / "main-opus-standard-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- passed",
        "minds-evals: released -- not evaluated",
    ]
    assert read_sections(messages[2])[0].startswith(
        ":x: [_mngr_ `main` (`unresolved`) | _dwt_ `main` (`unresolved`)] -- a ref did not resolve"
    )


def test_render_slack_report_still_reports_a_running_pair_whose_matrix_names_no_cell(tmp_path: Path) -> None:
    """A running pair always has a cell, so such a matrix contradicts itself. The report is the whole
    notification of a run, so the pair gets its one message and reads as broken rather than being
    left out of the report altogether."""
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [])

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context())

    assert read_header(message) == "minds-evals: main -- broken"
    assert "-- broken (no oracle summary; the job failed before grading)" in read_sections(message)[0]


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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        test_state="crashed",
    )
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
        errored_entry_ids=("todo-app__first_message",),
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=UNCONFIRMED_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_grid(message) == (("case", "haiku"), ("todo-app", "large_green_square 0.75"))
    # Nothing failed, so the failures table is not drawn and the note has nowhere else to go: the
    # details block is what carries it, naming the arm it belongs to.
    assert read_failed_trials(message) == ()
    assert read_details(message) == "*haiku* `todo-app`: model haiku unconfirmed"


def test_render_slack_report_says_nothing_about_a_lane_that_can_never_confirm_a_model(tmp_path: Path) -> None:
    """A codex arm records null every trial of every night, so the note above would never clear
    there. A permanent line is one a reader learns to skim, which costs the arms that raise it for a
    reason, so the lane's known shape is left to the docs and the trial's own arm block."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "codex-sol-low", CellDecision.RUN)]
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "codex-sol-low"),
        tmp_path / "main-codex-live",
        harness_config=CODEX_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_grid(message) == (("case", "codex-sol-low"), ("todo-app", "large_green_square 0.75"))
    assert "unconfirmed" not in message.text


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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert read_header(message) == "minds-evals: main x eval-config-small -- broken"
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
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
        oracle_summary_path(summaries_dir, "main", CONFIG_SLUG),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
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

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
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
        oracle_summary_path(summaries_dir, "main", CONFIG_SLUG),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )

    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
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
    truncated_path = live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default")
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
        oracle_summary_path(summaries_dir, "main", CONFIG_SLUG),
        tmp_path / "main-oracle",
        is_environment_recorded=False,
        failed_gate_names=("all_turns_completed",),
    )
    truncated_path = live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default")
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

    assert read_header(message) == "minds-evals: main x eval-config-small -- broken"
    assert "-- broken (no oracle summary; the job failed before grading)" in read_sections(message)[0]
    assert read_details(message) == "*default* -- not evaluated (the oracle pass did not pass)"


def test_render_slack_report_tells_an_unreadable_oracle_summary_from_a_missing_one(tmp_path: Path) -> None:
    """A summary that cannot be read means the pass got as far as grading and left something broken
    behind, which sends the reader somewhere else entirely."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    summary_path = oracle_summary_path(summaries_dir, "main", CONFIG_SLUG)
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
    matrix_path.write_text('{"configs": ["x"], "pairs": [')

    (message,) = render_slack_report(matrix_path, tmp_path / "summaries", make_context(resolve_result="cancelled"))

    assert "no pairs were resolved (resolve=cancelled" in message.text


def test_render_slack_report_warns_when_every_arm_passed_but_a_job_did_not(tmp_path: Path) -> None:
    """Cleanup and upload run whatever the trials did, so a job can be red with every arm green. The
    warning names the job, and only fires when nothing else already explains the red run."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"), tmp_path / "main-default-live")

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
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
    write_summary(live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"), tmp_path / "main-default-live")
    write_summary(
        live_summary_path(summaries_dir, "released", CONFIG_SLUG, "default"),
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
    write_summary(live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"), tmp_path / "main-default-live")

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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
    write_run_check_reports(
        check_job_directory(oracle_job_dir), None, oracle_summary_path(summaries_dir, "main", CONFIG_SLUG)
    )

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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
        ":white_check_mark: *minds-evals: main x eval-config-small -- passed*",
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
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

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=WRONG_MODEL_HARNESS_CONFIG,
    )

    messages = render_slack_report(matrix_path, summaries_dir, make_context())
    (undecided,) = render_slack_report(None, summaries_dir, make_context(resolve_result="failure"))

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- failed",
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
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
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
    pass is broken. The workflow cannot import this module, so this composes each name out of the
    shell variables the workflow's check steps hold those parts in, and reads the result back out of
    the workflow -- which pins the order of a name's parts as well as its spelling.
    """
    workflow_text = read_scheduled_workflow_text()
    oracle_name = oracle_summary_path(Path("summaries"), "$PAIR", "$CONFIG_SLUG").name
    live_name = live_summary_path(Path("summaries"), "$PAIR", "$CONFIG_SLUG", "$HARNESS_CONFIG").name

    invariants_name = live_invariants_summary_path(Path("summaries"), "$PAIR", "$CONFIG_SLUG", "$HARNESS_CONFIG").name

    assert '--summary-json "$SUMMARY_DIR/{}"'.format(oracle_name) in workflow_text
    assert '--summary-json "$SUMMARY_DIR/{}"'.format(live_name) in workflow_text
    assert '--summary-json "$SUMMARY_DIR/{}"'.format(invariants_name) in workflow_text
    assert '--summary-json "$SUMMARY_DIR/{}-$PAIR.json"'.format(DIAGNOSE_FIXTURE_SUMMARY_STEM) in workflow_text
    assert (
        '--summary-json "$SUMMARY_DIR/{}-$PAIR-$HARNESS.json"'.format(DIAGNOSE_BEHAVIOUR_SUMMARY_STEM) in workflow_text
    )
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
    """`ci-report` writes an array of webhook payloads, one per pair and eval config, and each is a
    whole message: posting only the first, or the array itself, would drop a suite's report on the
    floor."""
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


def write_green_pair_with_diagnostics(
    tmp_path: Path,
    summaries_dir: Path,
    *,
    fixture_trials: Sequence[DiagnosticTrialCheck] = (),
    behaviour_trials: Sequence[DiagnosticTrialCheck] | None = None,
    unsupported: Sequence[UnsupportedDiagnosticCell] = (),
) -> Path:
    """A pair whose one cell passed, with both of its diagnose jobs' summaries in place.

    The cells are green throughout, so whatever the message says beyond the grid is the diagnostics'
    doing and nothing else's.
    """
    matrix_path = tmp_path / "matrix.json"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN)],
        diagnosed_pairs=["main"],
        diagnosed_harnesses=["codex"],
        unsupported=unsupported,
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_diagnostic_summary(diagnose_fixture_summary_path(summaries_dir, "main"), fixture_trials)
    if behaviour_trials is not None:
        write_diagnostic_summary(diagnose_behaviour_summary_path(summaries_dir, "main", "codex"), behaviour_trials)
    return matrix_path


def test_render_slack_report_carries_the_worst_diagnostics_verdict_on_the_pairs_opening_line(
    tmp_path: Path,
) -> None:
    """The families and harnesses are named at the top, so a night without a measurement is never read
    as a clean one from the opening line alone."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=[
            make_diagnostic_trial(DiagnosticVerdict.NOT_MEASURED, not_measured_reason="the workspace never came up")
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_sections(message)[0].startswith(
        ":white_check_mark: {} -- oracle passed; diagnostics not measured: behaviour codex".format(PAIR_LABEL)
    )
    assert "*diagnose behaviour codex* `behaviour`: not measured: the workspace never came up" in read_details(message)


def test_render_slack_report_says_diagnostics_passed_without_naming_a_job(tmp_path: Path) -> None:
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED)],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert "-- oracle passed; diagnostics passed\n" in read_sections(message)[0]
    assert read_details(message) == ""


def test_render_slack_report_fails_a_pair_whose_diagnostic_failed_and_rows_every_failing_fact(
    tmp_path: Path,
) -> None:
    """A failed diagnostic is the instrument's own fault, so it fails the pair even with every cell
    green; each failing fact is a row of its own, because the fact names the reader that regressed.

    The count on the opening line is of the facts that missed, never of the ones a table held back:
    a trial stopped early holds back everything it asserts, and counting those would turn one defect
    into a number the size of the table."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=[
            make_diagnostic_trial(
                DiagnosticVerdict.FAILED,
                failed_facts=[
                    make_fact_outcome("tools.every_call_has_one_result", FactStatus.FAILED, step_name="work")
                ],
                not_recorded_facts=[make_fact_outcome("probe.parsed", FactStatus.NOT_RECORDED, recorded=None)],
                unmet_preconditions=[
                    make_fact_outcome("progress.step_ids_agree", FactStatus.PRECONDITION_NOT_MET, recorded=None)
                ],
                unexpectedly_passing_facts=[
                    make_fact_outcome(
                        "transcript.agent_steps_with_model_name",
                        FactStatus.UNEXPECTEDLY_PASSING,
                        recorded="all",
                        expected="none",
                        known_failure=KnownFailure(issue=898),
                    )
                ],
            )
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
    assert message.icon_emoji == UNGREEN_ICON_EMOJI
    assert "diagnostics failed: behaviour codex (3 facts)" in read_sections(message)[0]
    rows = read_failed_trials(message)
    assert rows[0] == ("config", "case", "note")
    assert [row[0] for row in rows[1:]] == ["diagnose behaviour codex"] * 3
    assert rows[1][2] == "tools.every_call_has_one_result [work]: false vs expected true"
    assert rows[3][2].startswith("unexpectedly passing: transcript.agent_steps_with_model_name")


def test_render_slack_report_leaves_the_pairs_verdict_alone_for_a_known_or_unfollowed_diagnostic(
    tmp_path: Path,
) -> None:
    """Only a failed diagnostic moves the pair. A known failure is expected every night it is declared,
    and an agent that did not follow the prompt says nothing about the instrument."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[
            make_diagnostic_trial(
                DiagnosticVerdict.KNOWN,
                case_id="fixture",
                harness="claude",
                known_facts=[
                    make_fact_outcome("workers.captured", FactStatus.KNOWN, known_failure=KnownFailure(issue=873))
                ],
            )
        ],
        behaviour_trials=[
            make_diagnostic_trial(
                DiagnosticVerdict.NOT_FOLLOWED,
                not_followed_facts=[
                    make_fact_outcome("agent.worker_launched", FactStatus.NOT_FOLLOWED, step_name="work")
                ],
                unmet_preconditions=[
                    make_fact_outcome("workers.discovered_equals_listed", FactStatus.PRECONDITION_NOT_MET)
                ],
            )
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert message.icon_emoji == GREEN_ICON_EMOJI
    assert "diagnostics not followed: behaviour codex" in read_sections(message)[0]
    details = read_details(message)
    assert (
        "*diagnose behaviour codex* `behaviour`: not followed: agent.worker_launched [work] "
        "(1 dependent fact(s) not asserted)"
    ) in details
    assert "*diagnose fixture* `fixture`: known: workers.captured (#873)" in details


def test_render_slack_report_reads_a_diagnose_job_that_left_no_summary_as_broken(tmp_path: Path) -> None:
    """The job gates nothing, so the details block is the only place its absence is said out loud."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=None,
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context(diagnose_behaviour_result="failure"))

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert "diagnostics broken: behaviour codex" in read_sections(message)[0]
    assert "*diagnose behaviour codex* -- broken (no diagnostics summary" in read_details(message)
    # The job's own red result is already explained by the line above, so it raises no second warning.
    assert "diagnose-behaviour=failure" not in "\n".join(read_sections(message))


def test_render_slack_report_names_a_behaviour_cell_the_pair_cannot_run(tmp_path: Path) -> None:
    """No box is spent to produce a dark cell, so the report is where the missing measurement shows."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED)],
        unsupported=[UnsupportedDiagnosticCell(pair="main", harness="pi-coding", reason="no pasted-key sign-in")],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert "diagnostics passed (pi-coding unsupported)" in read_sections(message)[0]
    assert "*diagnose behaviour* not run on this pair: pi-coding" in read_details(message)


def test_render_slack_report_reports_the_diagnostics_of_a_pair_whose_every_cell_was_skipped(
    tmp_path: Path,
) -> None:
    """An all-green night moves no SHA, which is exactly when the diagnostics are the only measurement
    the run took."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("released", PairDecision.SKIP)],
        [make_cell("released", "default", CellDecision.SKIP)],
        diagnosed_pairs=["released"],
        diagnosed_harnesses=["codex"],
    )
    write_diagnostic_summary(
        diagnose_fixture_summary_path(summaries_dir, "released"),
        [make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
    )
    write_diagnostic_summary(
        diagnose_behaviour_summary_path(summaries_dir, "released", "codex"),
        [
            make_diagnostic_trial(
                DiagnosticVerdict.FAILED,
                failed_facts=[make_fact_outcome("manifest.readable", FactStatus.FAILED)],
            )
        ],
    )

    (message,) = render_slack_report(
        matrix_path, summaries_dir, make_context(oracle_result="skipped", evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: released -- failed"
    assert "diagnostics failed: behaviour codex (1 fact)" in read_sections(message)[0]


def test_render_slack_report_reports_no_diagnostics_on_a_run_that_stopped_after_the_oracle(
    tmp_path: Path,
) -> None:
    """The diagnose jobs are skipped with the rest of the live pass, so nothing about them is claimed."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN)],
        diagnosed_pairs=["main"],
        diagnosed_harnesses=["codex"],
        unsupported=[UnsupportedDiagnosticCell(pair="main", harness="pi-coding", reason="no pasted-key sign-in")],
    )
    write_passing_oracle(summaries_dir, tmp_path)

    (message,) = render_slack_report(
        matrix_path,
        summaries_dir,
        make_context(
            is_live_pass_skipped=True,
            evaluate_result="skipped",
            diagnose_fixture_result="skipped",
            diagnose_behaviour_result="skipped",
        ),
    )

    assert "diagnostics" not in read_sections(message)[0]
    assert read_details(message) == ""


def test_render_slack_report_names_the_live_invariants_a_cells_own_trials_missed(tmp_path: Path) -> None:
    """The reading turns "the readers are checked once a night on a fixture" into "on every real
    trial", and it gates nothing: the cell and the pair stay exactly as their own pass made them."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "haiku", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )
    write_diagnostic_summary(
        live_invariants_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        [
            make_diagnostic_trial(
                DiagnosticVerdict.FAILED,
                case_id="todo-app",
                trial_name="todo-app__{}".format(index),
                harness="claude",
                failed_facts=[
                    make_fact_outcome("prep.stage_reached", FactStatus.FAILED),
                    *(
                        [make_fact_outcome("steps.boundary_markers_match_case", FactStatus.FAILED)]
                        if index == 0
                        else []
                    ),
                ],
            )
            for index in range(3)
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_details(message) == (
        "*haiku* live invariants missed: prep.stage_reached (3 of 3 trials);"
        " steps.boundary_markers_match_case (1 of 3 trials)"
    )
    assert read_failed_trials(message) == ()


def test_render_slack_report_says_nothing_of_a_cell_whose_trials_held_every_invariant(tmp_path: Path) -> None:
    """The line exists to name a miss, so a cell that missed none reads as clean rather than as one
    more line to skip past."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "haiku", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        tmp_path / "main-haiku-live",
        harness_config=HAIKU_HARNESS_CONFIG,
    )
    write_diagnostic_summary(
        live_invariants_summary_path(summaries_dir, "main", CONFIG_SLUG, "haiku"),
        [make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="todo-app", harness="claude")],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_details(message) == ""


def test_render_slack_report_keeps_the_failures_table_within_slacks_row_cap(tmp_path: Path) -> None:
    """A diagnostic lists every failing fact as a row of its own, so a broken reader can fail more facts
    than a Slack table holds; the rows Slack refuses would take the whole message with them."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=[
            make_diagnostic_trial(
                DiagnosticVerdict.FAILED,
                failed_facts=[
                    make_fact_outcome("fact.number_{}".format(index), FactStatus.FAILED) for index in range(150)
                ],
            )
        ],
    )

    (message,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    rows = read_failed_trials(message)
    assert len(rows) == SLACK_TABLE_ROW_LIMIT
    assert "showing 99 of 150 failing rows" in "\n".join(
        element["text"] for block in read_blocks(message, "context") for element in block["elements"]
    )


def test_the_notify_job_waits_for_the_diagnose_jobs_and_passes_their_results() -> None:
    """The report reads every diagnose summary of a pair, so a notify that did not wait for them would
    report a pair whose diagnostics had not finished as one whose diagnostics left no summary."""
    workflow_text = read_scheduled_workflow_text()

    assert "needs: [resolve, oracle, evaluate, diagnose-fixture, diagnose-behaviour]" in workflow_text
    assert '--diagnose-fixture-result "$DIAGNOSE_FIXTURE_RESULT"' in workflow_text
    assert '--diagnose-behaviour-result "$DIAGNOSE_BEHAVIOUR_RESULT"' in workflow_text


def test_the_live_invariants_never_decide_a_cells_own_verdict() -> None:
    """The reading is what turns "the readers are checked once a night on a fixture" into "on every
    real trial". A miss belongs in the report's details; letting its exit code reach the step would
    make an invariant gate the product's own pass, which nothing in the spec asks for."""
    workflow_text = read_scheduled_workflow_text()

    check_step = workflow_text.partition("      - name: Check the live pass\n")[2].partition("\n      - ")[0]

    assert check_step, "no live-pass check step in {}".format(SCHEDULED_WORKFLOW_PATH)
    invariants_call = check_step.partition("minds-evals check-diagnostics")[2]
    assert invariants_call, "the live pass is not read against the invariants"
    assert "check_status" not in invariants_call.partition("\n          {")[0]
    assert 'exit "$check_status"' in check_step
