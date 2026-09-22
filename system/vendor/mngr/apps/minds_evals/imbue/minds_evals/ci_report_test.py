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
from imbue.minds_evals.ci_report import SlackThread
from imbue.minds_evals.ci_report import UNGREEN_ICON_EMOJI
from imbue.minds_evals.ci_report import as_slack_payload
from imbue.minds_evals.ci_report import as_slack_thread_payload
from imbue.minds_evals.ci_report import band_emoji_name
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
from imbue.minds_evals.data_types import CriterionKind
from imbue.minds_evals.data_types import CriterionScore
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import DiagnosticRunCheck
from imbue.minds_evals.data_types import DiagnosticTrialCheck
from imbue.minds_evals.data_types import DiagnosticVerdict
from imbue.minds_evals.data_types import DimensionScore
from imbue.minds_evals.data_types import FactMatcher
from imbue.minds_evals.data_types import FactMatcherKind
from imbue.minds_evals.data_types import FactOutcome
from imbue.minds_evals.data_types import FactStatus
from imbue.minds_evals.data_types import FixtureDiagnosticCell
from imbue.minds_evals.data_types import KnownFailure
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import PricingSource
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import Spender
from imbue.minds_evals.data_types import SpenderCost
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.data_types import UnsupportedDiagnosticCell
from imbue.minds_evals.slack_post import SLACK_BOT_TOKEN_ENV_VAR
from imbue.minds_evals.slack_post import SLACK_WEBHOOK_ENV_VAR
from imbue.minds_evals.testing import FIXTURE_PRICE_MAP
from imbue.minds_evals.testing import SCHEDULED_WORKFLOW_PATH
from imbue.minds_evals.testing import read_scheduled_workflow_text
from imbue.minds_evals.testing import trial_usage_payload
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
    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
    write_run_check_reports(run_check, None, summary_path)
    return run_check


def write_two_case_summary(summary_path: Path, job_dir: Path, **trial_arguments: Any) -> RunCheck:
    """A pass over two cases, so the grid it feeds has more than one row."""
    write_trial_dir(job_dir, "greeting__aaaaaaa", case_id="greeting", **trial_arguments)
    write_trial_dir(job_dir, "todo-app__bbbbbbb", case_id="todo-app", **trial_arguments)
    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
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


def render_messages(
    matrix_path: Path | None, summaries_dir: Path, context: CiReportContext
) -> tuple[SlackMessage, ...]:
    """The top message of every thread the report renders, for the tests that are about those."""
    return tuple(thread.message for thread in render_slack_report(matrix_path, summaries_dir, context))


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
    beside it, so one assertion covers every half of what a cell says.
    """
    if cell["type"] == "raw_text":
        return cell["text"]
    (section,) = cell["elements"]
    return "".join(
        element["name"] if element["type"] == "emoji" else element["text"] for element in section["elements"]
    )


def read_table_rows(table: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(read_cell(cell) for cell in row) for row in table["rows"])


def read_grid(message: SlackMessage) -> tuple[tuple[str, ...], ...]:
    """The grid's rows, its cells flattened to strings; empty when the message carries no grid.

    The grid is the message's first table, and a failures table is the only other one it can carry:
    a trial that failed is a trial that was graded, so the failures are drawn only under a grid.
    """
    tables = read_blocks(message, "table")
    if not tables:
        return ()
    return read_table_rows(tables[0])


def read_failed_trials(message: SlackMessage) -> tuple[tuple[str, ...], ...]:
    """The failures table; empty when nothing failed and the message drew none."""
    tables = read_blocks(message, "table")
    return () if len(tables) < 2 else read_table_rows(tables[1])


def read_scoring_reply(thread: SlackThread) -> SlackMessage | None:
    """The reply carrying the scoring tables; None when the thread has none.

    It is the reply that carries tables, which the diagnostics reply -- one section block -- does not.
    """
    replies = [reply for reply in thread.replies if any(block["type"] == "table" for block in reply.blocks)]
    return replies[0] if replies else None


def read_diagnostics_reply(thread: SlackThread) -> SlackMessage | None:
    """The reply carrying the diagnostics detail; None when the thread has none."""
    replies = [reply for reply in thread.replies if any(block["type"] == "section" for block in reply.blocks)]
    return replies[0] if replies else None


def read_scoring_tables(thread: SlackThread) -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Every scoring table of the thread's reply, its cells flattened to strings."""
    reply = read_scoring_reply(thread)
    return () if reply is None else tuple(read_table_rows(table) for table in read_blocks(reply, "table"))


def read_scoring_contexts(thread: SlackThread) -> tuple[str, ...]:
    """The context block above each scoring table, one string each."""
    reply = read_scoring_reply(thread)
    if reply is None:
        return ()
    return tuple(block["elements"][0]["text"] for block in read_blocks(reply, "context"))


def read_all_tables(message: SlackMessage) -> tuple[Mapping[str, Any], ...]:
    return read_blocks(message, "table")


def count_table_characters(message: SlackMessage) -> int:
    """What Slack counts against a message's table budget: the text in its cells.

    An emoji element is a name rather than text, so it is not counted -- reading a cell's rendering
    instead would charge the budget for characters no cell holds.
    """
    return sum(
        len(cell["text"])
        if cell["type"] == "raw_text"
        else sum(len(element.get("text", "")) for section in cell["elements"] for element in section["elements"])
        for table in read_all_tables(message)
        for row in table["rows"]
        for cell in row
    )


def make_bold_cell(text: str) -> dict[str, Any]:
    """A table cell as the report builds a bold one: harness configs are named this way."""
    return {
        "type": "rich_text",
        "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": text, "style": {"bold": True}}]}
        ],
    }


def make_judge_score(dimension: str, criterion: str, value: float, *, step: str = "") -> CriterionScore:
    """One criterion a judge scored, on rewardkit's own normalized scale."""
    return CriterionScore(step=step, dimension=dimension, criterion=criterion, kind=CriterionKind.LLM, value=value)


def make_check_score(dimension: str, criterion: str, value: float, *, step: str = "") -> CriterionScore:
    """One criterion a programmatic check scored, which a scoring table reports beside the judges'."""
    return CriterionScore(
        step=step, dimension=dimension, criterion=criterion, kind=CriterionKind.PROGRAMMATIC, value=value
    )


def make_dimension_score(dimension: str, value: float, *, step: str = "") -> DimensionScore:
    return DimensionScore(step=step, dimension=dimension, value=value)


def make_spend(
    cost_usd: float | None,
    *,
    is_complete: bool = True,
    is_rate_certain: bool = True,
    unpriced_models: Sequence[str] = (),
    harness_cost_usd: float | None = None,
) -> tuple[SpenderCost, ...]:
    """What one trial cost, as its summary carries it: the workspace agent's half, and the harness's
    where a test asks for one.

    The harness half is what tells the grid cell's agent-only reading apart from a reading of the
    whole record, so a test that cares about the difference has to build a trial that has both.
    """
    agent = SpenderCost(
        spender=Spender.WORKSPACE_AGENT,
        cost_usd=cost_usd,
        is_complete=is_complete,
        is_rate_certain=is_rate_certain,
        unpriced_models=tuple(unpriced_models),
    )
    if harness_cost_usd is None:
        return (agent,)
    return (
        agent,
        SpenderCost(
            spender=Spender.DECIDER,
            cost_usd=harness_cost_usd,
            is_complete=True,
            is_rate_certain=True,
            unpriced_models=(),
        ),
    )


def make_graded_trial(
    case_id: str,
    criterion_scores: Sequence[CriterionScore],
    reward: float | None = 0.75,
    *,
    dimension_scores: Sequence[DimensionScore] = (),
    spend: Sequence[SpenderCost] = (),
    conversation_seconds: float | None = None,
    is_gates_passed: bool = True,
) -> TrialCheck:
    """One graded trial as a summary carries it.

    Built as a model rather than out of a job directory, because the scores are what is under test
    and every fixture directory scores the same few. The composed reward rides among the dimensions
    the way a real summary carries it, and a caller that scores dimensions of its own adds them to it.
    """
    composed = () if reward is None else (make_dimension_score("reward", reward),)
    return TrialCheck(
        trial_name="{}__aaaaaaa".format(case_id),
        case_id=case_id,
        is_completed=True,
        incompletion_reason="",
        step_count=0,
        completed_step_count=0,
        is_gates_passed=is_gates_passed,
        error_entry_ids=(),
        reward=reward,
        criterion_scores=tuple(criterion_scores),
        dimension_scores=(*dimension_scores, *composed),
        conversation_seconds=conversation_seconds,
        spend=tuple(spend),
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
SLACK_TABLE_CHARACTER_LIMIT: Final[int] = 10000
SLACK_TABLE_CELL_CHARACTER_LIMIT: Final[int] = 120


def assert_within_slack_limits(message: SlackMessage) -> None:
    """Every cap Slack refuses a whole message over. A reply carries no header, which is why the
    header limit is checked over whatever headers the message has rather than over its first."""
    assert [
        header["text"]["text"]
        for header in read_blocks(message, "header")
        if len(header["text"]["text"]) > SLACK_HEADER_LIMIT
    ] == []
    assert [len(section) for section in read_sections(message) if len(section) > SLACK_SECTION_LIMIT] == []
    for table in read_all_tables(message):
        assert len(table["rows"]) <= SLACK_TABLE_ROW_LIMIT
        assert [len(row) for row in table["rows"] if len(row) > SLACK_TABLE_CELL_LIMIT] == []
        # Header cells included: a criterion name is as unbounded as a case id, and a cell over the
        # cap costs the whole message rather than its own column.
        assert [
            cell
            for row in table["rows"]
            for cell in (read_cell(cell) for cell in row)
            if len(cell) > SLACK_TABLE_CELL_CHARACTER_LIMIT
        ] == []


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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_sections(message)[0] == (
        ":white_check_mark: {} -- oracle passed\npassed in 41m20s, _trigger=_ `schedule`".format(PAIR_LABEL)
    )
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("greeting", "large_green_square 0.75 9m05s", "large_green_square 0.75 9m05s"),
        ("todo-app", "large_green_square 0.75 9m05s", "large_green_square 0.75 9m05s"),
    )
    assert read_failed_trials(message) == ()
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

    messages = render_messages(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: released x eval-config-small -- failed",
    ]
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75 9m05s"))
    assert read_grid(messages[1]) == (("case", "haiku"), ("todo-app", "large_green_square 0.75 9m05s\u00a0\u2717"))


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

    threads = render_slack_report(matrix_path, summaries_dir, make_context())

    assert [read_header(thread.message) for thread in threads] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- passed",
    ]
    # Each grid holds its own suite's arm and its own suite's cases, read out of that suite's own
    # summary files, and each thread's scoring reply holds that suite's own rows.
    assert read_grid(threads[0].message) == (("case", "default"), ("todo-app", "large_green_square 0.75 9m05s"))
    assert read_grid(threads[1].message) == (
        ("case", "opus-standard"),
        ("greeting", "large_green_square 0.75 9m05s"),
        ("todo-app", "large_green_square 0.75 9m05s"),
    )
    assert [row[0] for row in read_scoring_tables(threads[1])[0][1:]] == ["opus-standard", "opus-standard"]


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

    messages = render_messages(matrix_path, summaries_dir, make_context())

    assert [read_header(message) for message in messages] == [
        "minds-evals: main x eval-config-small -- passed",
        "minds-evals: main x eval-config-time-to-mock -- failed",
    ]
    assert read_grid(messages[0]) == (("case", "default"), ("todo-app", "large_green_square 0.75 9m05s"))
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

    messages = render_messages(matrix_path, summaries_dir, make_context())

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

    messages = render_messages(matrix_path, summaries_dir, make_context(evaluate_result="skipped"))

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

    messages = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(matrix_path, tmp_path / "summaries", make_context())

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("todo-app", "large_green_square 0.75 9m05s\u00a0\u2717", "large_green_square 0.75 9m05s\u00a0\u2717"),
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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_grid(message) == (("case", "haiku"), ("todo-app", "large_green_square 0.75 9m05s\u00a0\u2717"))
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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_grid(message) == (("case", "haiku"), ("todo-app", "large_green_square 0.75 9m05s"))
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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_header(message) == "minds-evals: main x eval-config-small -- passed"
    assert read_grid(message) == (("case", "codex-sol-low"), ("todo-app", "large_green_square 0.75 9m05s"))
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

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(thread.message) == "minds-evals: main x eval-config-small -- passed"
    assert read_grid(thread.message) == (
        ("case", "default", "haiku"),
        ("todo-app", "heavy_minus_sign", "large_green_square 0.75 9m05s"),
    )
    # The skipped cell graded nothing, so it has no row in the scoring reply and the details block is
    # what says why its column is empty.
    assert [row[0] for row in read_scoring_tables(thread)[0][1:]] == ["haiku"]
    assert read_details(thread.message) == "*default* -- skipped (already green)"


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

    (message,) = render_messages(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert read_header(message) == "minds-evals: main x eval-config-small -- broken"
    assert read_grid(message) == (
        ("case", "default", "haiku"),
        ("todo-app", "large_green_square 0.75 9m05s", "grey_question"),
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

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_grid(thread.message) == (
        ("case", "default", "haiku"),
        ("greeting", "large_green_square 0.75 9m05s", "grey_question"),
        ("todo-app", "large_green_square 0.75 9m05s", "large_green_square 0.75 9m05s"),
    )
    # The scoring reply has a row per trial that was actually graded, so the case the haiku cell never
    # ran is absent from it rather than carried as a hole the way the grid has to carry it.
    assert [row[:2] for row in read_scoring_tables(thread)[0][1:]] == [
        ("default", "greeting"),
        ("default", "todo-app"),
        ("haiku", "todo-app"),
    ]


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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(
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

    (message,) = render_messages(matrix_path, tmp_path / "summaries", make_context())

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
    (thread,) = render_slack_report(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(thread.message) == "minds-evals: main x eval-config-small -- passed"
    assert read_sections(thread.message)[0].endswith("`schedule` _(oracle only)_")
    assert read_grid(thread.message) == (("case", "oracle"), ("todo-app", "large_green_square 0.75 9m05s"))
    assert [row[:2] for row in read_scoring_tables(thread)[0][1:]] == [("oracle", "todo-app")]


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

    (message,) = render_messages(
        matrix_path, summaries_dir, make_context(is_live_pass_skipped=True, evaluate_result="skipped")
    )

    assert read_header(message) == "minds-evals: main x eval-config-small -- failed"
    assert read_sections(message)[0].startswith(":x: {} -- oracle failed".format(PAIR_LABEL))
    assert read_grid(message) == (("case", "oracle"), ("todo-app", "large_green_square 0.75 9m05s\u00a0\u2717"))
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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert "*default* -- broken (the live summary could not be read)" in read_details(message)
    assert "not evaluated (the oracle pass did not pass)" not in message.text


def test_render_slack_report_says_a_pair_that_wrote_no_oracle_summary_is_broken(tmp_path: Path) -> None:
    """The pair's job died before grading. Nothing about the pair was measured, so the message has to
    read as an absence rather than as an oracle that ran and failed."""
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])

    (message,) = render_messages(matrix_path, tmp_path / "summaries", make_context())

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert "-- broken (the oracle summary could not be read)" in read_sections(message)[0]


def test_render_slack_report_reports_a_run_that_never_decided_what_to_evaluate(tmp_path: Path) -> None:
    (message,) = render_messages(
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

    (message,) = render_messages(matrix_path, tmp_path / "summaries", make_context())

    assert read_header(message) == "minds-evals -- broken"
    assert "no pairs were resolved" in read_sections(message)[0]


def test_render_slack_report_reports_a_matrix_file_it_cannot_read_the_same_way(tmp_path: Path) -> None:
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text('{"configs": ["x"], "pairs": [')

    (message,) = render_messages(matrix_path, tmp_path / "summaries", make_context(resolve_result="cancelled"))

    assert "no pairs were resolved (resolve=cancelled" in message.text


def test_render_slack_report_warns_when_every_arm_passed_but_a_job_did_not(tmp_path: Path) -> None:
    """Cleanup and upload run whatever the trials did, so a job can be red with every arm green. The
    warning names the job, and only fires when nothing else already explains the red run."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"), tmp_path / "main-default-live")

    (message,) = render_messages(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

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

    messages = render_messages(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

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

    (message,) = render_messages(
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
        check_job_directory(tmp_path / "main-default-live", FIXTURE_PRICE_MAP),
        None,
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_grid(thread.message) == (("case", "default"), ("todo-app", "grey_question 9m05s\u00a0\u2717"))
    # Nothing scored it, so the scoring reply has no row to draw for it and no reply at all.
    assert read_scoring_tables(thread) == ()


def test_render_slack_report_builds_a_grid_cell_out_of_a_band_a_reward_and_what_the_trial_cost(
    tmp_path: Path,
) -> None:
    """A cell is the reward's band as a coloured square, the reward, what the arm spent on the case
    and how long it talked, and then the mark that says whether the trial passed.

    The colour therefore means one thing throughout and never competes with the verdict, and the two
    figures an arm is actually chosen on ride beside its score rather than in a report nobody opens.
    They are italic so that the score stays what the eye lands on, and every column is left-aligned:
    a cell is a run of elements of different widths, so right-aligning it would line the failure
    marks up rather than the rewards.
    """
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN), make_cell("main", "haiku", CellDecision.SKIP)],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            # The harness spent forty times what the agent did on this trial, and the cell says
            # nothing about it: a grid is read to compare arms, and the decider costs what it costs
            # whichever arm it decided for.
            make_graded_trial(
                "greeting",
                [make_judge_score("quality", "conciseness", 0.8)],
                spend=make_spend(1.234, harness_cost_usd=50.0),
                conversation_seconds=545.0,
            ),
            # A figure that holds less than the trial spent, on a trial that also failed its gates.
            make_graded_trial(
                "roadmap",
                [make_judge_score("quality", "conciseness", 0.4)],
                reward=0.55,
                spend=make_spend(12.5, is_complete=False),
                conversation_seconds=41.0,
                is_gates_passed=False,
            ),
            # A spender whose model carries no price, and a trial whose records do not time it.
            make_graded_trial(
                "todo-app",
                [make_judge_score("quality", "conciseness", 0.2)],
                reward=0.2,
                spend=make_spend(None, unpriced_models=["kimi-k2.6"]),
                conversation_seconds=None,
            ),
            # Real money that rounds to nothing at the width a cell prints, and a trial that recorded
            # no spend at all.
            make_graded_trial(
                "welcome",
                [make_judge_score("quality", "conciseness", 0.9)],
                reward=0.95,
                spend=make_spend(0.004),
                conversation_seconds=120.0,
            ),
            make_graded_trial("bare", [make_judge_score("quality", "conciseness", 0.9)], conversation_seconds=None),
        ],
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    grid = read_blocks(message, "table")[0]
    assert grid["column_settings"] == [{"align": "left"}, {"align": "left"}, {"align": "left"}]
    assert grid["rows"][0] == [
        {"type": "raw_text", "text": "case"},
        make_bold_cell("default"),
        make_bold_cell("haiku"),
    ]
    case_cell, graded_cell, skipped_cell = grid["rows"][1]
    assert case_cell == {"type": "raw_text", "text": "greeting"}
    assert graded_cell == {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {"type": "emoji", "name": "large_green_square"},
                    {"type": "text", "text": " 0.75"},
                    {"type": "text", "text": " $1.23 9m05s", "style": {"italic": True}},
                ],
            }
        ],
    }
    assert skipped_cell == {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "emoji", "name": "heavy_minus_sign"}]}],
    }
    # A floor, spend nothing could price, a trial that reports no time, and one that recorded no
    # spend at all: four readings a reader must not confuse, so each is spelled differently.
    assert read_grid(message)[2:] == (
        ("roadmap", "large_yellow_square 0.55 $12.50+ 41s\u00a0\u2717", "heavy_minus_sign"),
        ("todo-app", "large_red_square 0.20 $?", "heavy_minus_sign"),
        ("welcome", "large_green_square 0.95 <$0.01 2m00s", "heavy_minus_sign"),
        ("bare", "large_green_square 0.75", "heavy_minus_sign"),
    )


def test_render_slack_report_explains_the_grid_cell_under_the_grid(tmp_path: Path) -> None:
    """Nothing in a cell says what its figures are, and both marks a cost can carry are about how the
    figure was arrived at rather than about the money, which no number can show."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    note = read_blocks(message, "context")[0]["elements"][0]["text"]
    assert note == (
        "_reward_ :large_red_square: `<0.25` :large_orange_square: `<0.50` :large_yellow_square:"
        " `<0.75` :large_green_square: `>=0.75`  --  *✗* = the trial failed its gates  --"
        "  _italics_ = agent spend and conversation time, `+` a floor and `$?` spend nothing"
        " could price"
    )


@pytest.mark.parametrize(
    ("reward", "emoji_name"),
    [
        (0.0, "large_red_square"),
        (0.249, "large_red_square"),
        (0.25, "large_orange_square"),
        (0.499, "large_orange_square"),
        (0.5, "large_yellow_square"),
        (0.749, "large_yellow_square"),
        (0.75, "large_green_square"),
        (1.0, "large_green_square"),
    ],
)
def test_band_emoji_name_reads_a_bands_figure_as_the_ceiling_it_does_not_reach(reward: float, emoji_name: str) -> None:
    """The legend prints each band as `<0.25`, so a reward exactly on a band's figure belongs to the
    band above it. Every band is exercised, since the one a cell wears is the whole of what the grid
    says about a score at a glance."""
    assert band_emoji_name(reward) == emoji_name


def test_render_slack_report_carries_the_messages_own_spend_on_the_pair_line(tmp_path: Path) -> None:
    """What a night cost is read per arm, beside the verdict of that arm: a total across every pair
    answers a question nobody asks at the top of one pair's message. The halves are named the way the
    run's markdown summary names them, so the two reports can be read against each other."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "greeting",
                [make_judge_score("quality", "conciseness", 0.8)],
                spend=(
                    *make_spend(1.25),
                    SpenderCost(
                        spender=Spender.DECIDER,
                        cost_usd=0.1,
                        is_complete=True,
                        is_rate_certain=True,
                        unpriced_models=(),
                    ),
                ),
            ),
            make_graded_trial("todo-app", [make_judge_score("quality", "conciseness", 0.8)], spend=make_spend(0.75)),
        ],
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_sections(message)[0].splitlines()[1] == (
        "passed in 41m20s, _trigger=_ `schedule`, agent spend $2.00 over 2 trials; harness spend $0.10 over 1 trial"
    )


def test_render_slack_report_leaves_the_spend_off_a_pair_line_with_nothing_to_sum(tmp_path: Path) -> None:
    """A run whose trials recorded no spend at all -- every summary written before usage.json existed
    -- says nothing rather than reporting a $0.00 nobody earned."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [make_graded_trial("greeting", [make_judge_score("quality", "conciseness", 0.8)])],
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert read_sections(message)[0].splitlines()[1] == "passed in 41m20s, _trigger=_ `schedule`"


def test_render_slack_report_replies_with_every_scoring_input_behind_its_grid(tmp_path: Path) -> None:
    """The grid says what an arm scored; the reply says what that score was made of.

    Two tables, because a row holding every dimension's criteria is far past what a Slack table row
    takes: the first is what was built, the second the harness that drove it. Each dimension is a bold
    super-column carrying its own score, with the criteria that make it up beside it -- programmatic
    checks and judges alike, since a report of the judges alone is not the scoring input.
    """
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
                    make_judge_score("outcome", "works_as_expected", 0.9),
                    make_check_score("outcome", "app_registered", 1.0),
                    make_judge_score("quality", "conciseness", 0.78),
                    make_judge_score("harness_quality", "main_harness_success", 0.5),
                    make_check_score("gates", "not_timed_out", 1.0),
                ],
                dimension_scores=[
                    make_dimension_score("outcome", 0.95),
                    make_dimension_score("quality", 0.78),
                    make_dimension_score("harness_quality", 0.5),
                    make_dimension_score("gates", 1.0),
                ],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    assert reply.icon_emoji == thread.message.icon_emoji
    assert read_scoring_tables(thread) == (
        (
            ("config", "case", "outcome", "works_as_expected", "app_registered", "quality", "conciseness"),
            ("default", "todo-app", "0.95", "0.90", "1.00", "0.78", "0.78"),
        ),
        (
            ("config", "case", "harness_quality", "main_harness_success", "gates", "not_timed_out"),
            ("default", "todo-app", "0.50", "0.50", "\u2713", "\u2713"),
        ),
    )
    # The dimension headings are bold, as the grid's own column headings are: nothing else in the row
    # says where one dimension's block of columns ends and the next begins.
    (first_table, second_table) = read_blocks(reply, "table")
    assert first_table["rows"][0][2] == make_bold_cell("outcome")
    assert first_table["rows"][0][3] == {"type": "raw_text", "text": "works_as_expected"}
    assert first_table["rows"][1][0] == make_bold_cell("default")
    # The columns that say which trial a row is are left-aligned and every score is right-aligned, so
    # the numbers line up under their headings however long the case ids beside them run.
    assert first_table["column_settings"] == [{"align": "left"}] * 2 + [{"align": "right"}] * 5
    assert second_table["column_settings"] == [{"align": "left"}] * 2 + [{"align": "right"}] * 4
    # Each table says which dimensions it holds and the scale its cells are on, and the one holding
    # the gates says how a gate reads.
    assert read_scoring_contexts(thread) == (
        "_outcome, quality: each dimension's own score, then the criteria under it; every score 0.00-1.00_",
        "_harness_quality, gates: each dimension's own score, then the criteria under it;"
        " a gate reads ✓ or :x:; every score 0.00-1.00_",
    )
    # The scale is stated once above each table, because nothing in a cell shows it.
    assert read_scoring_contexts(thread)[0] == (
        "_outcome, quality: each dimension's own score, then the criteria under it; every score 0.00-1.00_"
    )
    # The reply's text is the same content as fixed-width fences, for wherever the blocks do not render.
    assert reply.text.splitlines()[:4] == [
        "_outcome, quality: each dimension's own score, then the criteria under it; every score 0.00-1.00_",
        "```",
        "config   case      outcome  works_as_expected  app_registered  quality  conciseness",
        "default  todo-app  0.95     0.90               1.00            0.78     0.78",
    ]


def test_render_slack_report_keeps_two_dimensions_criteria_of_the_same_name_apart(tmp_path: Path) -> None:
    """A criterion name comes out of a case's own expectations, so two dimensions can score one of
    the same name -- and a cell that found its value by name alone would report one dimension's
    answer under the other's heading."""
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
                    make_judge_score("outcome", "completeness", 0.20),
                    make_judge_score("quality", "completeness", 0.90),
                ],
                dimension_scores=[make_dimension_score("outcome", 0.20), make_dimension_score("quality", 0.90)],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_scoring_tables(thread)[0] == (
        ("config", "case", "outcome", "completeness", "quality", "completeness"),
        ("default", "todo-app", "0.20", "0.20", "0.90", "0.90"),
    )


def test_render_slack_report_counts_a_stepped_rows_step_column_against_the_cell_cap(tmp_path: Path) -> None:
    """The step column is one of the columns that identify a row, so it is one fewer for the scores.
    A table that did not count it would build a row over Slack's cell cap, which is refused together
    with the whole message."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "roadmap",
                [make_judge_score("outcome", "criterion-{}".format(index), 0.8, step="build") for index in range(25)],
                dimension_scores=[make_dimension_score("outcome", 0.8, step="build")],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    (table,) = read_scoring_tables(thread)
    # Three columns identify the row and one carries the dimension's own score, so sixteen criteria
    # are what is left of the twenty-five -- one fewer than a flat row fits.
    assert [len(row) for row in table] == [SLACK_TABLE_CELL_LIMIT, SLACK_TABLE_CELL_LIMIT]
    assert table[0][:4] == ("config", "case", "step", "outcome")
    assert table[0][-1] == "criterion-15"
    # The columns that say which trial a row is are left-aligned and every score is right-aligned.
    assert read_blocks(reply, "table")[0]["column_settings"] == [{"align": "left"}] * 3 + [{"align": "right"}] * 17


def test_render_slack_report_gives_each_step_of_a_stepped_case_a_row_of_its_own(tmp_path: Path) -> None:
    """A stepped case is scored once per step against that step's own expectations, so one criterion
    is as many answers as the case has steps. One row per step keeps them apart, and the step column
    is drawn only where some row has one -- a suite of flat cases has nothing to put in it."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "roadmap",
                [
                    make_judge_score("quality", "conciseness", 0.6, step="triage"),
                    make_judge_score("quality", "conciseness", 0.9, step="build"),
                ],
                dimension_scores=[
                    make_dimension_score("quality", 0.6, step="triage"),
                    make_dimension_score("quality", 0.9, step="build"),
                ],
            ),
            make_graded_trial("greeting", [make_judge_score("quality", "conciseness", 0.8)]),
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    # The flat trial's row keeps the column and says it has no step, rather than leaving a cell that
    # reads as a step nobody named. Its composed reward is not a row of its own either.
    assert read_scoring_tables(thread)[0] == (
        ("config", "case", "step", "quality", "conciseness"),
        ("default", "roadmap", "triage", "0.60", "0.60"),
        ("default", "roadmap", "build", "0.90", "0.90"),
        ("default", "greeting", "-", "-", "0.80"),
    )


def test_render_slack_report_marks_a_gate_rather_than_scoring_it(tmp_path: Path) -> None:
    """A gate is a property a trial either has or has not, so a number would read as a measurement
    where there is none. Only the failure is coloured: a check blends with the numbers beside it, and
    a table full of red emoji says nothing about which row is the one to read."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "todo-app",
                [make_check_score("gates", "not_timed_out", 0.0)],
                dimension_scores=[make_dimension_score("gates", 0.0)],
                is_gates_passed=False,
            ),
            make_graded_trial(
                "greeting",
                [make_check_score("gates", "not_timed_out", 1.0)],
                dimension_scores=[make_dimension_score("gates", 1.0)],
            ),
            # A gate that landed between the two: there is no mark for half a property, so the
            # number is what the cell says.
            make_graded_trial(
                "roadmap",
                [make_check_score("gates", "not_timed_out", 0.5)],
                dimension_scores=[make_dimension_score("gates", 0.5)],
            ),
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    # A failed gate is the `x` emoji as an element of its own: a table cell renders no `:name:`
    # shortcode written into its text.
    (gates_table,) = read_blocks(reply, "table")
    assert gates_table["rows"][1][2] == {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [{"type": "emoji", "name": "x"}]}],
    }
    assert gates_table["rows"][2][2] == {"type": "raw_text", "text": "\u2713"}
    # The fence says both in glyphs, because Slack renders no emoji inside one.
    assert read_scoring_tables(thread)[0][1:] == (
        ("default", "todo-app", "x", "x"),
        ("default", "greeting", "\u2713", "\u2713"),
        ("default", "roadmap", "0.50", "0.50"),
    )
    assert "\u2717" in reply.text
    # The gates ride in the second of the two tables, so its own context line is the one that has to
    # explain the marks -- and it is the only line above the only table this message drew.
    (context,) = read_scoring_contexts(thread)
    assert context.endswith("a gate reads \u2713 or :x:; every score 0.00-1.00_")


def test_render_slack_report_keeps_a_row_out_of_the_table_it_scored_nothing_in(tmp_path: Path) -> None:
    """Each table draws two of the four dimensions, so a trial scored on one table's pair has nothing
    to say in the other -- and a row of dashes there reads as a trial that scored bottom rather than
    as one that was never scored on those. The row cap and the character budget are each table's own
    for the same reason: a row it does not draw must not cost it one it does."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "todo-app",
                [make_judge_score("quality", "conciseness", 0.8)],
                dimension_scores=[make_dimension_score("quality", 0.8)],
            ),
            make_graded_trial(
                "greeting",
                [make_check_score("gates", "not_timed_out", 1.0)],
                dimension_scores=[make_dimension_score("gates", 1.0)],
            ),
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_scoring_tables(thread) == (
        (
            ("config", "case", "quality", "conciseness"),
            ("default", "todo-app", "0.80", "0.80"),
        ),
        (
            ("config", "case", "gates", "not_timed_out"),
            ("default", "greeting", "\u2713", "\u2713"),
        ),
    )


def test_render_slack_report_takes_the_scoring_columns_as_the_union_across_the_rows(tmp_path: Path) -> None:
    """A case can be scored on criteria another case is not: the dataset's cases carry their own
    expectations. So the columns are the union across the rows, in first-seen order, and a row that
    was never scored on one prints a dash rather than a zero it did not earn."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial("greeting", [make_judge_score("quality", "conciseness", 0.9)]),
            make_graded_trial(
                "todo-app",
                [
                    make_judge_score("quality", "conciseness", 0.6),
                    make_judge_score("outcome", "works_as_expected", 0.5),
                ],
            ),
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    # `outcome` leads the table wherever it is scored, whichever row first names it; the row that was
    # not scored on it has a hole in both of its columns. The harness table is not drawn at all.
    assert read_scoring_tables(thread) == (
        (
            ("config", "case", "outcome", "works_as_expected", "quality", "conciseness"),
            ("default", "greeting", "-", "-", "-", "0.90"),
            ("default", "todo-app", "-", "0.50", "-", "0.60"),
        ),
    )


def test_render_slack_report_says_when_scoring_columns_did_not_fit_a_row(tmp_path: Path) -> None:
    """Slack refuses a table whose row is over the cap, and the whole message with it, so the columns
    that do not fit go rather than the table -- and the line above it says how many, since a table
    that quietly lost columns reads as the whole of what a trial was scored on."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "todo-app",
                [make_judge_score("quality", "criterion-{}".format(index), 0.8) for index in range(25)],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    (table,) = read_scoring_tables(thread)
    # Two columns identify the row and one carries the dimension's own score, so seventeen criteria
    # are what is left of the twenty-five.
    assert [len(row) for row in table] == [SLACK_TABLE_CELL_LIMIT, SLACK_TABLE_CELL_LIMIT]
    assert table[0][-1] == "criterion-16"
    assert read_scoring_contexts(thread)[0].splitlines()[1] == (
        "_8 criterion columns did not fit this row; they are in the run's summary_"
    )


def test_render_slack_report_drops_a_scoring_dimension_left_no_room_for_its_own_score(
    tmp_path: Path,
) -> None:
    """The columns that do not fit go from the end, so the dimension a reader is handed first keeps
    its criteria whole. One with no room left for even its own score goes entirely -- a criterion
    under a dimension nobody can see says nothing -- and the line above the table stops naming it."""
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
                    *(make_judge_score("outcome", "criterion-{}".format(index), 0.8) for index in range(25)),
                    *(make_judge_score("quality", "conciseness-{}".format(index), 0.8) for index in range(3)),
                ],
                dimension_scores=[make_dimension_score("outcome", 0.8), make_dimension_score("quality", 0.8)],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    (table,) = read_scoring_tables(thread)
    # Seventeen of `outcome`'s criteria fit beside its own score; `quality` and all three of its own
    # are counted among the columns that did not.
    assert [len(row) for row in table] == [SLACK_TABLE_CELL_LIMIT, SLACK_TABLE_CELL_LIMIT]
    assert table[0][2] == "outcome"
    assert "quality" not in table[0]
    context = read_scoring_contexts(thread)[0].splitlines()
    assert context[0].startswith("_outcome: ")
    assert context[1] == "_12 criterion columns did not fit this row; they are in the run's summary_"


def test_render_slack_report_says_when_a_dimension_of_its_own_did_not_fit_a_row(tmp_path: Path) -> None:
    """A dimension scored with no criteria is a single column, so counting only criteria would drop
    it without a word -- and a table nothing says it is missing a dimension reads as the whole of
    what the trial was scored on."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "todo-app",
                [make_check_score("harness_quality", "check-{}".format(index), 0.8) for index in range(25)],
                dimension_scores=[make_dimension_score("harness_quality", 0.8), make_dimension_score("gates", 1.0)],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    (table,) = read_scoring_tables(thread)
    assert "gates" not in table[0]
    context = read_scoring_contexts(thread)[0].splitlines()
    # Eight of `harness_quality`'s checks did not fit, and `gates`, whose one column is its own score.
    assert context[1] == "_9 criterion columns did not fit this row; they are in the run's summary_"


def test_render_slack_report_clamps_a_heading_as_long_as_the_text_it_was_authored_from(
    tmp_path: Path,
) -> None:
    """A criterion name comes out of a case's own expectations and a harness config name out of the
    matrix, and neither is held to a length. Slack refuses a table cell over its cap together with
    the whole message behind it, so a heading is cut like every other cell."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    long_config = "harness-{}".format("c" * 200)
    long_criterion = "criterion-{}".format("n" * 200)
    write_matrix(
        matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", long_config, CellDecision.RUN)]
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, long_config),
        [make_graded_trial("todo-app", [make_judge_score("quality", long_criterion, 0.8)])],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(thread.message)
    assert_within_slack_limits(reply)
    clamped_config = long_config[: SLACK_TABLE_CELL_CHARACTER_LIMIT - 3] + "..."
    assert read_grid(thread.message)[0] == ("case", clamped_config)
    (table,) = read_scoring_tables(thread)
    assert table[0][-1] == long_criterion[: SLACK_TABLE_CELL_CHARACTER_LIMIT - 3] + "..."


def test_render_slack_report_keeps_the_scoring_reply_inside_slacks_row_cap(tmp_path: Path) -> None:
    """Slack refuses a table of more than a hundred rows, header included, and a suite of many cases
    times many steps reaches that on its own. The rows that do not fit go, and the line above the
    table says how many -- a table that quietly lost rows reads as every trial the run graded."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial("case-{}".format(index), [make_judge_score("quality", "conciseness", 0.8)])
            for index in range(120)
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    (table,) = read_scoring_tables(thread)
    assert len(table) == SLACK_TABLE_ROW_LIMIT
    assert read_scoring_contexts(thread)[0].splitlines()[-1] == (
        "_showing 99 of 120 scored rows; the rest are in the run's summary_"
    )


def test_render_slack_report_spends_each_scoring_tables_row_cap_on_its_own_rows(tmp_path: Path) -> None:
    """The cap is Slack's per table, so a table that held the rows of both would lose rows carrying
    scores to rows of dashes. Each table draws only what its own two dimensions scored, so a suite
    of more graded trials than one table holds still reports every one of them."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "quality-{}".format(index),
                [make_judge_score("quality", "conciseness", 0.8)],
                dimension_scores=[make_dimension_score("quality", 0.8)],
            )
            for index in range(60)
        ]
        + [
            make_graded_trial(
                "gates-{}".format(index),
                [make_check_score("gates", "not_timed_out", 1.0)],
                dimension_scores=[make_dimension_score("gates", 1.0)],
            )
            for index in range(60)
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    quality_table, gates_table = read_scoring_tables(thread)
    # 120 rows across the two, which one shared cap would have cut to 99.
    assert [len(table) - 1 for table in (quality_table, gates_table)] == [60, 60]
    assert [row[1] for row in quality_table[1:]] == ["quality-{}".format(index) for index in range(60)]
    assert [row[1] for row in gates_table[1:]] == ["gates-{}".format(index) for index in range(60)]
    # Nothing was dropped, so neither table says it was.
    assert [context.splitlines()[-1].startswith("_showing ") for context in read_scoring_contexts(thread)] == [
        False,
        False,
    ]


def test_render_slack_report_keeps_the_scoring_tables_inside_slacks_character_budget(tmp_path: Path) -> None:
    """Slack caps a message at 10000 characters across the cells of all its tables and refuses the
    whole message past it, so a suite with more graded trials than fit loses rows rather than blocks.

    Whole rows, or the scores that survived would line up under the wrong headings, and from the
    longer table, so the two tables shrink together rather than one of them disappearing.
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
                [
                    make_judge_score("quality", "conciseness", 0.8),
                    make_check_score("gates", "not_timed_out", 1.0),
                ],
                dimension_scores=[make_dimension_score("quality", 0.8), make_dimension_score("gates", 1.0)],
            )
            for index in range(60)
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    tables = read_scoring_tables(thread)
    assert count_table_characters(reply) <= SLACK_TABLE_CHARACTER_LIMIT
    # Rows went from both tables, every row that survived is whole, and no single cell ran away with
    # the budget either.
    assert [0 < len(table) - 1 < 60 for table in tables] == [True, True]
    assert {len(row) for table in tables for row in table} == {4}
    assert [row[1] for table in tables for row in table[1:] if len(row[1]) > SLACK_TABLE_CELL_CHARACTER_LIMIT] == []
    # Each table announces the rows it kept, so a reader comparing the two knows which trials each
    # of them is missing.
    assert [context.splitlines()[-1] for context in read_scoring_contexts(thread)] == [
        "_showing {} of 60 scored rows; the rest are in the run's summary_".format(len(table) - 1) for table in tables
    ]


def test_render_slack_report_gives_up_the_columns_of_the_rows_the_budget_dropped(tmp_path: Path) -> None:
    """A table's columns are collected from the rows it had before the character budget took any off
    the end, so a criterion only a dropped row was scored on would keep a column of dashes -- which
    reads as a trial that scored bottom on it rather than as one nobody scored on it, and spends a
    header and one of the row's cells on saying nothing."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "case-{}-{}".format(index, "x" * 300),
                [make_judge_score("quality", "conciseness", 0.8)],
                dimension_scores=[make_dimension_score("quality", 0.8)],
            )
            for index in range(95)
        ]
        # Last in case order, so it is the first row the budget drops, and the only row scored on a
        # criterion of its own.
        + [
            make_graded_trial(
                "zzz-rare",
                [make_judge_score("quality", "rarely_scored", 0.4)],
                dimension_scores=[make_dimension_score("quality", 0.4)],
            )
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    assert count_table_characters(reply) <= SLACK_TABLE_CHARACTER_LIMIT
    (table,) = read_scoring_tables(thread)
    assert "zzz-rare" not in [row[1] for row in table[1:]]
    assert table[0] == ("config", "case", "quality", "conciseness")


def test_render_slack_report_gives_up_a_dimension_none_of_the_rows_it_kept_scored(tmp_path: Path) -> None:
    """A table draws the rows of either of its two dimensions, so the budget can take every row that
    scored one of them and leave the other's standing. The dimension goes with them, legend and all:
    a block whose every column would be a dash says nothing, and a legend naming a dimension the
    table no longer holds sends the reader looking for columns that are not there.
    """
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "outcome-{:03d}-{}".format(index, "x" * 300),
                [make_judge_score("outcome", "works", 0.8)],
                dimension_scores=[make_dimension_score("outcome", 0.8)],
            )
            for index in range(90)
        ]
        # Last in case order, so the budget drops all three before it reaches an `outcome` row.
        + [
            make_graded_trial(
                "zzz-quality-{}".format(index),
                [make_judge_score("quality", "conciseness", 0.4)],
                dimension_scores=[make_dimension_score("quality", 0.4)],
            )
            for index in range(3)
        ],
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    reply = read_scoring_reply(thread)
    assert reply is not None
    assert_within_slack_limits(reply)
    (table,) = read_scoring_tables(thread)
    assert table[0] == ("config", "case", "outcome", "works")
    assert [row for row in table[1:] if row[1].startswith("zzz-quality")] == []
    assert 0 < len(table) - 1 < 90
    assert read_scoring_contexts(thread)[0].splitlines()[0] == (
        "_outcome: each dimension's own score, then the criteria under it; every score 0.00-1.00_"
    )


def test_render_slack_report_posts_no_scoring_reply_for_a_suite_that_graded_nothing(tmp_path: Path) -> None:
    """A pair the run skipped whole graded no trial, so there is nothing behind its grid to reply
    with -- and an empty reply reads as a measurement that went missing."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path, [make_pair("released", PairDecision.SKIP)], [make_cell("released", "default", CellDecision.SKIP)]
    )

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(thread.message) == "minds-evals: released -- skipped (already green)"
    assert thread.replies == ()


def test_format_budgeted_block_cuts_one_enormous_line_inside_the_budget() -> None:
    """The only line the budget cuts mid-word, because a block of one line still has to say
    something about it -- and the truncation notice comes out of the budget rather than on top of
    it, so what a section is handed is never over."""
    block = format_budgeted_block(["x" * (DETAIL_BUDGET * 2)])

    assert len(block) == DETAIL_BUDGET
    assert block.endswith("\n... (truncated; see the run summary)")


def test_render_slack_report_keeps_the_grid_and_its_failures_inside_slacks_character_budget(
    tmp_path: Path,
) -> None:
    """Slack caps a message at 10000 characters across the cells of all its tables and refuses the
    whole message past it. The top message's own two tables are bounded by the matrix, but a case id
    is not, so each cell is clamped and the failures table takes whatever the grid left it."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(matrix_path, [make_pair("main", PairDecision.RUN)], [make_cell("main", "default", CellDecision.RUN)])
    write_passing_oracle(summaries_dir, tmp_path)
    write_model_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        [
            make_graded_trial(
                "case-{}-{}".format(index, "x" * 300),
                [make_judge_score("quality", "conciseness", 0.8)],
                is_gates_passed=False,
            )
            for index in range(60)
        ],
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    assert count_table_characters(message) <= SLACK_TABLE_CHARACTER_LIMIT
    # The enormous case ids are cut rather than allowed to spend the whole budget, and the failures
    # table says how many rows it had to give up.
    grid = read_grid(message)
    assert [row[0] for row in grid[1:] if len(row[0]) > SLACK_TABLE_CELL_CHARACTER_LIMIT] == []
    assert grid[1][0].endswith("...")
    assert 0 < len(read_failed_trials(message)) - 1 < 60
    assert [
        element["text"]
        for block in read_blocks(message, "context")
        for element in block["elements"]
        if element["text"].startswith("_showing ")
    ] == [
        "_showing {} of 60 failing rows; the rest are in the run's summary_".format(
            len(read_failed_trials(message)) - 1
        )
    ]


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
        check_job_directory(oracle_job_dir, FIXTURE_PRICE_MAP),
        None,
        oracle_summary_path(summaries_dir, "main", CONFIG_SLUG),
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

    assert_within_slack_limits(message)
    details = read_details(message)
    assert len(details) <= DETAIL_BUDGET
    lines = details.splitlines()
    assert lines[0] == "*default* -- not evaluated (the oracle pass did not pass)"
    assert lines[-1] == "... (truncated; see the run summary)"
    assert 0 < len(lines) - 2 < 12
    assert all(line.startswith("*oracle* `todo-app-") for line in lines[1:-1])


def test_as_slack_payload_posts_as_the_run_and_carries_the_whole_report_as_text(tmp_path: Path) -> None:
    """Neither transport carries an identity of its own, and Slack shows `text` wherever the blocks
    cannot be rendered -- including the retry the poster makes when a block type is refused -- so the
    text has to be the whole report rather than a caption for it."""
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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())
    payload = as_slack_payload(message)

    assert payload["username"] == SLACK_USERNAME
    assert payload["icon_emoji"] == GREEN_ICON_EMOJI
    assert payload["text"] == message.text
    # Nothing failed, so no failures table stands between the grid and the details, and the scores
    # behind the grid are a reply rather than a block of this message.
    assert [block["type"] for block in payload["blocks"]] == [
        "header",
        "section",
        "table",
        "context",
        "section",
        "context",
    ]
    # The grid keeps its words rather than its emoji here: Slack renders no emoji inside a fence.
    assert message.text.splitlines() == [
        ":white_check_mark: *minds-evals: main x eval-config-small -- passed*",
        ":white_check_mark: {} -- oracle passed".format(PAIR_LABEL),
        "passed in 41m20s, _trigger=_ `schedule`",
        "```",
        "case      default        haiku",
        "greeting  ok 0.75 9m05s  -",
        "todo-app  ok 0.75 9m05s  -",
        "```",
        "*details*",
        "*haiku* -- skipped (already green)",
        "<{url}|run logs> | <{url}#artifacts|artifacts>".format(url=RUN_URL),
    ]


def test_as_slack_thread_payload_carries_the_message_and_its_replies(tmp_path: Path) -> None:
    """The file `ci-report` writes is what the posting command reads back, so a thread is one object
    with its replies inside it: a flat list of payloads would leave nothing saying which message a
    reply belongs under, and each reply is a report of nothing on its own."""
    matrix_path = tmp_path / "matrix.json"
    summaries_dir = tmp_path / "summaries"
    write_matrix(
        matrix_path,
        [make_pair("main", PairDecision.RUN)],
        [make_cell("main", "default", CellDecision.RUN)],
        diagnosed_pairs=["main"],
        diagnosed_harnesses=["codex"],
    )
    write_passing_oracle(summaries_dir, tmp_path)
    write_summary(
        live_summary_path(summaries_dir, "main", CONFIG_SLUG, "default"),
        tmp_path / "main-default-live",
        harness_config=DEFAULT_HARNESS_CONFIG,
    )
    write_diagnostic_summary(
        diagnose_fixture_summary_path(summaries_dir, "main"),
        [
            make_diagnostic_trial(
                DiagnosticVerdict.KNOWN,
                case_id="fixture",
                known_facts=[make_fact_outcome("agent.worker_launched", FactStatus.KNOWN)],
            )
        ],
    )
    write_diagnostic_summary(diagnose_behaviour_summary_path(summaries_dir, "main", "codex"), [])

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())
    payload = as_slack_thread_payload(thread)

    # The keys the posting command reads the file back through, and nothing else: a payload of any
    # other shape is refused as a bad `--payloads` file rather than posted.
    assert set(payload) == {"message", "replies"}
    assert payload["message"]["text"] == thread.message.text
    assert payload["message"]["blocks"] == list(thread.message.blocks)
    assert [reply["icon_emoji"] for reply in payload["replies"]] == [GREEN_ICON_EMOJI, GREEN_ICON_EMOJI]
    assert payload["replies"][1]["text"].startswith("*diagnostics detail*")


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

    messages = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(
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

    messages = render_messages(matrix_path, summaries_dir, make_context())
    (undecided,) = render_messages(None, summaries_dir, make_context(resolve_result="failure"))

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context(evaluate_result="failure"))

    assert ":warning:" in read_sections(message)[0]
    assert as_slack_payload(message)["icon_emoji"] == GREEN_ICON_EMOJI


def test_parse_run_check_reads_back_the_summary_check_run_wrote(tmp_path: Path) -> None:
    """`check-run` dumps a model whose verdict fields are computed, and computed fields are extras on
    the way back in, so the dump does not validate as-is. The trial carries a spend record, which is
    the deepest level of the summary: a computed field on one of those would make every summary of a
    trial that recorded money read as one that could not be read."""
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", harness_config=HAIKU_HARNESS_CONFIG, usage=trial_usage_payload())
    run_check = check_job_directory(job_dir, FIXTURE_PRICE_MAP)
    summary_path = tmp_path / "live-summary.json"
    write_run_check_reports(run_check, None, summary_path)
    assert '"is_passed"' in summary_path.read_text()

    parsed = parse_run_check(summary_path.read_text())

    assert parsed == run_check
    assert parsed.is_passed is True
    assert parsed.trials[0].requested_model == "haiku"
    assert [entry.spender for entry in parsed.trials[0].spend] == list(Spender)
    assert parsed.pricing == run_check.pricing


def test_parse_run_check_reads_a_summary_written_before_the_cost_fields_existed(tmp_path: Path) -> None:
    """A summary is read back by whatever `ci-report` is current, which is not always the `check-run`
    that wrote it, and a field the writer never heard of must read as a trial that recorded nothing
    for it rather than as a summary that could not be read -- the one thing the report has no way to
    tell a reader apart from a pass that genuinely went missing.
    """
    job_dir = tmp_path / "nightly-run"
    write_trial_dir(job_dir, "todo-app__aaaaaaa", harness_config=HAIKU_HARNESS_CONFIG)
    payload = json.loads(check_job_directory(job_dir, FIXTURE_PRICE_MAP).model_dump_json())
    payload.pop("pricing")
    for trial in payload["trials"]:
        for field_name in ("step_count", "completed_step_count", "spend"):
            trial.pop(field_name)

    parsed = parse_run_check(json.dumps(payload))

    assert parsed.pricing.source == ""
    (trial_check,) = parsed.trials
    assert (trial_check.step_count, trial_check.completed_step_count, trial_check.spend) == (0, 0, ())


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
    assert _nested_model_types(RunCheck) == {TrialCheck, PricingSource}

    deeper_models = _nested_model_types(TrialCheck)

    assert deeper_models == {CriterionScore, DimensionScore, SpenderCost}
    # Every level the stripping does not reach, which is every model under the summary but the two it
    # names: a computed field on any of them is validated back as one the model did not declare.
    unstripped_models = {PricingSource, *deeper_models}
    assert [model for model in unstripped_models if model.model_computed_fields] == []


def test_format_duration_reads_as_a_wall_clock() -> None:
    assert format_duration(None) == "an unknown time"
    assert format_duration(41) == "41s"
    assert format_duration(61) == "1m01s"
    assert format_duration(2480) == "41m20s"


def test_ci_report_writes_one_thread_per_suite_and_exits_zero_without_a_matrix(tmp_path: Path) -> None:
    """The notification is the whole report of a nightly, so a run broken enough to have decided
    nothing still gets a message rather than a failing job. The posting command reads the file as it
    stands, one thread at a time, so it is an array of threads whatever the run decided."""
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
    assert payloads[0]["message"]["blocks"][0]["text"]["text"] == "minds-evals -- broken"
    assert "the run broke before deciding what to evaluate" in payloads[0]["message"]["text"]
    # Nothing was graded and no pair has diagnostics, so the thread is a message on its own.
    assert payloads[0]["replies"] == []


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


def test_the_notify_job_posts_every_thread_the_report_writes() -> None:
    """The whole file goes to one command, which posts each thread and each reply under it: a step
    that posted the payloads itself would have no message id to thread a reply under, and the scores
    behind every grid live in those replies."""
    workflow_text = read_scheduled_workflow_text()

    assert "minds-evals post-slack-report \\\n            --payloads /tmp/slack-payloads.json" in workflow_text
    assert '--channel "$SLACK_CHANNEL_ID"' in workflow_text
    # Posting is guarded rather than trusted: the toolchain steps above it may have been skipped, and
    # a notification must never turn the run red.
    assert "::warning::minds-evals post-slack-report failed" in workflow_text


def test_the_notify_job_fetches_both_slack_credentials_and_names_the_channel_in_the_open() -> None:
    """The bot token is what the command prefers and the webhook is its fallback, so the job fetches
    both. The channel id is not a secret: it names a channel and grants nothing, and where the report
    lands is worth reading off the workflow."""
    workflow_text = read_scheduled_workflow_text()

    assert "mngr/ci/{}".format(SLACK_BOT_TOKEN_ENV_VAR) in workflow_text
    assert "mngr/ci/{}".format(SLACK_WEBHOOK_ENV_VAR) in workflow_text
    assert "SLACK_CHANNEL_ID: C0BUFUVU0T0" in workflow_text


def test_the_notify_job_writes_each_threads_message_and_its_replies_into_the_step_summary() -> None:
    """The step summary is the whole report wherever Slack is not reachable, so it carries the
    replies as well as the messages -- and the rendering-failure notice it writes in place of a
    report is a thread like any other, or the command would refuse the file."""
    workflow_text = read_scheduled_workflow_text()

    assert "jq -r '.message.text'" in workflow_text
    assert "jq -r '.replies[]? | .text + \"\\n\"'" in workflow_text
    assert '\'[{message: {username: "Evals", icon_emoji: ":brainless:", text: $text}, replies: []}]\'' in workflow_text


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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(thread.message) == "minds-evals: main x eval-config-small -- passed"
    assert thread.message.icon_emoji == GREEN_ICON_EMOJI
    assert "diagnostics not followed: behaviour codex" in read_sections(thread.message)[0]
    # The opening line names the job; the facts behind it are a reply, where a reader goes only once
    # that line has told them to.
    reply = read_diagnostics_reply(thread)
    assert reply is not None
    assert reply.text.splitlines()[0] == "*diagnostics detail*"
    assert (
        "*diagnose behaviour codex* `behaviour`: not followed: agent.worker_launched [work] "
        "(1 dependent fact(s) not asserted)"
    ) in reply.text
    assert "*diagnose fixture* `fixture`: known: workers.captured (#873)" in reply.text
    assert "known" not in read_details(thread.message)


def test_render_slack_report_reads_a_diagnose_job_that_left_no_summary_as_broken(tmp_path: Path) -> None:
    """The job gates nothing, so the details block is the only place its absence is said out loud."""
    summaries_dir = tmp_path / "summaries"
    matrix_path = write_green_pair_with_diagnostics(
        tmp_path,
        summaries_dir,
        fixture_trials=[make_diagnostic_trial(DiagnosticVerdict.PASSED, case_id="fixture", harness="claude")],
        behaviour_trials=None,
    )

    (message,) = render_messages(matrix_path, summaries_dir, make_context(diagnose_behaviour_result="failure"))

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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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

    (message,) = render_messages(
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

    (message,) = render_messages(
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

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_header(thread.message) == "minds-evals: main x eval-config-small -- passed"
    reply = read_diagnostics_reply(thread)
    assert reply is not None
    assert reply.text.splitlines()[1:] == [
        "*haiku* live invariants missed: prep.stage_reached (3 of 3 trials);"
        " steps.boundary_markers_match_case (1 of 3 trials)"
    ]
    assert read_details(thread.message) == ""
    assert read_failed_trials(thread.message) == ()


def test_render_slack_report_says_nothing_of_a_cell_whose_trials_held_every_invariant(tmp_path: Path) -> None:
    """The line exists to name a miss, so a cell that missed none gets no reply carrying one -- and
    reads as clean rather than as one more line to skip past."""
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

    (thread,) = render_slack_report(matrix_path, summaries_dir, make_context())

    assert read_diagnostics_reply(thread) is None
    assert read_details(thread.message) == ""


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

    (message,) = render_messages(matrix_path, summaries_dir, make_context())

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
