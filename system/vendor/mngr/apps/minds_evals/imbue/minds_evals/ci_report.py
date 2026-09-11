"""Render the Slack report of a scheduled run: a message per pair, holding a grid of case by config,
the trials that failed, and a collapsed table of every judge score behind them.

Only the blocks an incoming webhook actually accepts are used. A webhook refuses `data_table`
outright -- a minimal one is answered with 400 invalid_blocks -- so the sorting and paging it would
bring are not available here, and `container` is what keeps the long table out of the way instead.
`markdown` is refused as well. Both would need an app posting with a bot token rather than a
webhook, so do not reach for either without changing how the notify job posts.

A scheduled run evaluates arms -- a frozen (mngr, dwt) pair times a named harness config -- and this
is everything anyone reads about it. Each pair gets a message of its own, because the pairs answer
different questions ("is what we are about to ship healthy?" and "is what users are running
healthy?") and a reader acts on one of them at a time.

Every message carries a plain mrkdwn `text` beside its blocks. Slack shows that wherever the blocks
cannot be rendered, the workflow re-posts it on its own if Slack refuses a block, and it is what the
run writes to its step summary -- so it has to read as a whole report rather than as a caption.

The report has two inputs, and both are read as things that may not be there: the decided matrix,
which a run that broke early never wrote, and the per-pass summary files, which a job that died
before grading never uploaded.

Nothing here raises on a bad input file. This report is the whole notification of a run, so an
unreadable file is reported as unreadable, in wording that never reads as a pass that got nowhere:
"the summary could not be read" and "the job failed before grading" are different failures, and
reporting the first as the second sends the reader to the wrong place.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import BaseModel
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import CellDecision
from imbue.minds_evals.data_types import CiMatrix
from imbue.minds_evals.data_types import CiReportContext
from imbue.minds_evals.data_types import DecidedPair
from imbue.minds_evals.data_types import FrozenPair
from imbue.minds_evals.data_types import MatrixCell
from imbue.minds_evals.data_types import PairDecision
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.reporting import SHORT_SHA_LENGTH

# The pattern the notify job downloads every summary artifact under, and the stems of the files
# inside them. The download merges the artifacts into one flat directory, so a summary file's own
# name is what identifies the pass: the oracle runs per pair and the live pass per cell. These names
# are composed here from the matrix rather than discovered on disk, so they must stay equal to the
# ones the scheduled workflow's check steps write.
SUMMARY_ARTIFACT_PREFIX: Final[str] = "minds-evals-summary-"
ORACLE_SUMMARY_STEM: Final[str] = "oracle-summary"
LIVE_SUMMARY_STEM: Final[str] = "live-summary"

# Who the run posts as. The webhook carries no identity of its own, so the message names itself, and
# its avatar is the verdict at a glance in the channel list: the green one only where every arm the
# message reports came out green.
SLACK_USERNAME: Final[str] = "Evals"
GREEN_ICON_EMOJI: Final[str] = ":big_brain:"
UNGREEN_ICON_EMOJI: Final[str] = ":brainless:"

# A Slack section's text is capped at 3000 characters and the whole block is refused past it, so the
# details block is budgeted below that with room for its heading.
DETAIL_BUDGET: Final[int] = 2900
TRUNCATION_NOTICE: Final[str] = "... (truncated; see the run summary)"
DETAILS_HEADING: Final[str] = "*details*"

# The emoji the message speaks in, as Slack's own names for them. A table cell carries the bare name
# in an `emoji` element and mrkdwn carries the `:name:` shortcode, so the shortcodes are derived from
# the names rather than written out a second time and left to drift from them.
PASS_EMOJI_NAME: Final[str] = "white_check_mark"
FAIL_EMOJI_NAME: Final[str] = "x"
WARNING_EMOJI_NAME: Final[str] = "warning"
NOT_EVALUATED_EMOJI_NAME: Final[str] = "heavy_minus_sign"
UNKNOWN_EMOJI_NAME: Final[str] = "grey_question"

# The absolute scale the summary table colours a reward on, as the bands the squares stand for.
# Read as "up to": a reward lands in the first band whose ceiling it does not reach.
REWARD_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (0.25, "large_red_square"),
    (0.50, "large_orange_square"),
    (0.75, "large_yellow_square"),
)
TOP_BAND_EMOJI_NAME: Final[str] = "large_green_square"

# What a failing trial's reward carries after it, and what a passing one carries in its place. A
# plain glyph rather than an emoji, so that the square is the only colour in the row: the emoji
# would be red, which is the scale's own bottom band and would read as a second, contradicting
# verdict.
#
# The reward column is right-aligned and every reward is the same width, so a mark on one cell and
# nothing on another steps the numbers out of line -- hence the spacer. Neither may be built out of
# ordinary spaces: Slack trims trailing whitespace inside a cell, which collapses the spacer to
# nothing and leaves exactly the misalignment it is there to prevent. So the gap before the mark is
# a no-break space, and the spacer is that plus a figure space, which is as wide as a digit.
FAILED_MARK: Final[str] = "\u00a0\u2717"
PASSED_SPACER: Final[str] = "\u00a0\u2007"

PASS_EMOJI: Final[str] = ":{}:".format(PASS_EMOJI_NAME)
FAIL_EMOJI: Final[str] = ":{}:".format(FAIL_EMOJI_NAME)
WARNING_EMOJI: Final[str] = ":{}:".format(WARNING_EMOJI_NAME)

# The grid's first column, and what a cell of it says where there is no trial to report: a pass that
# was never run reads as a dash, and one whose story cannot be told as a question mark. These are the
# words the plain-text fallback prints; the blocks lead with the matching emoji instead, because
# Slack renders no emoji inside the code fence the fallback's grid lives in.
GRID_CASE_HEADING: Final[str] = "case"
NOT_EVALUATED_MARK: Final[str] = "-"
UNKNOWN_MARK: Final[str] = "?"
PASS_MARK: Final[str] = "ok"
FAIL_MARK: Final[str] = "FAIL"

# The judge table's own columns, ahead of one column per criterion, and what a cell of it says where
# there is no number: a trial that was never graded, or a criterion the judge did not score on it.
JUDGE_REWARD_HEADING: Final[str] = "reward"
MISSING_SCORE_MARK: Final[str] = "-"

# The judge table's other columns. It carries every graded trial of the pair rather than one pass's,
# so it has to name the arm each row came from, and the state column is what makes the table stand
# on its own when it is expanded away from the rest of the message.
JUDGE_CONFIG_HEADING: Final[str] = "config"
JUDGE_STATE_HEADING: Final[str] = "state"

# The failures table: the arm, the case, and what stopped it. It is drawn only when something
# failed -- a heading with an empty table under it reads as a missing measurement rather than as a
# green run.
FAILED_TRIALS_HEADING: Final[str] = "*failed trials*"
FAILED_TRIALS_NOTE_HEADING: Final[str] = "note"

# The collapsible the judge table lives in. Slack caps a container's child blocks and refuses the
# whole message past that, so nothing that grows with the matrix may become a child of its own.
JUDGE_CONTAINER_TITLE: Final[str] = "judge scores"
JUDGE_CONTAINER_SUBTITLE: Final[str] = "every config and case, criterion by criterion"
MAX_CONTAINER_CHILD_BLOCKS: Final[int] = 10

# Slack refuses a table whose row holds more than this many cells, and it refuses the whole message
# with it, so a pass that scored more criteria than fit loses its overflow columns rather than its
# table.
MAX_TABLE_CELLS_PER_ROW: Final[int] = 20

# Slack caps a message at this many characters across the cells of all its tables, and refuses the
# whole message past it. The grid and the failures table are bounded by the matrix and are drawn
# whole; the judge table is bounded by cases times configs times criteria, so it is what gets cut to
# what is left. No cell may run away with the budget either: a case id and an incompletion reason
# are both unbounded, so each cell is clamped before any of this is counted.
MAX_TABLE_CHARACTERS: Final[int] = 10000
MAX_TABLE_CELL_CHARACTERS: Final[int] = 120
TABLE_ELLIPSIS: Final[str] = "..."
# What is left of a judge table row once its non-criterion columns have taken their cells.
MAX_JUDGE_CRITERIA: Final[int] = MAX_TABLE_CELLS_PER_ROW - 4

# What the oracle column is called. The oracle pass boots no workspace, so it is independent of the
# harness config and is never one of them; `ci-matrix` refuses `oracle` as a config name for this.
ORACLE_LABEL: Final[str] = "oracle"

# What GitHub reports for a job that is fine: succeeded, or never needed to run. An empty result is
# a job the run never reached at all, which the pairs and cells already account for.
HEALTHY_JOB_RESULTS: Final[frozenset[str]] = frozenset({"success", "skipped", ""})


class ArmVerdict(LowerCaseStrEnum):
    """How one arm of a run came out, as the header that names it reads.

    BROKEN is the arm whose story cannot be told -- no summary, or an unreadable one -- as against
    FAILED, which is an arm that was measured and fell short. NOT_EVALUATED is an arm nothing was
    ever attempted on, because a ref did not resolve or because the pair's oracle gated it off.
    `combine_verdicts` is where their order is decided.
    """

    PASSED = auto()
    FAILED = auto()
    SKIPPED = auto()
    NOT_EVALUATED = auto()
    BROKEN = auto()


class UnreadSummary(FrozenModel):
    """A pass that left no graded run behind, and which of the two ways it did that.

    A summary that was never written means the pass did not get as far as grading; one that cannot
    be read means it did and left something broken behind. The report says which, because they send
    the reader to different places.
    """

    is_summary_absent: bool = Field(description="Whether the summary file was never written at all")


# What one pass's summary artifact yielded. The graded run stands for itself, so a reading that
# carries one cannot also claim an absence.
SummaryReading = RunCheck | UnreadSummary


class PassReport(FrozenModel):
    """One pass of a pair -- its oracle, or one of its cells -- as the message presents it.

    A pass is a column of the grid and, when there is something to say about it beyond its column,
    a line of the details block.
    """

    label: str = Field(description="The column heading: the cell's harness config, or 'oracle'")
    verdict: ArmVerdict = Field(description="How this pass came out")
    detail: str = Field(description="What the details block says about the pass; empty when it was graded")
    trials: tuple[TrialCheck, ...] = Field(description="The trials it graded; empty when it graded none")


class PairReport(FrozenModel):
    """Everything one pair's message says: the pair, how it came out, and the passes behind it."""

    pair: DecidedPair = Field(description="The pair, with the refs it froze to")
    verdict: ArmVerdict = Field(description="How the pair and all of its cells came out together")
    summary_text: str = Field(description="Why the pair reads as it does; empty when the verdict says it all")
    oracle: PassReport | None = Field(description="The pair's oracle pass; None when the run attempted none")
    cells: tuple[PassReport, ...] = Field(description="The pair's live passes, in matrix order; empty when none ran")


class GridMark(FrozenModel):
    """One cell of the grid, in the two spellings the message needs it in.

    The blocks lead with the emoji and the fallback with the word, because a code fence renders no
    emoji; carrying both here is what keeps the two readings of a cell from disagreeing.
    """

    emoji_name: str = Field(description="The Slack emoji the table cell leads with: the reward's band")
    word: str = Field(description="What the fallback prints where the blocks show the emoji")
    reward_text: str = Field(description="The trial's reward, spaced ready to follow; empty when it has none")
    verdict_mark: str = Field(description="The failure mark, or the spacer that keeps the rewards in line")


class GridRow(FrozenModel):
    """One row of the grid: the case it is about, and what each column made of it."""

    case_key: str = Field(description="The case the row reports on")
    marks: tuple[GridMark, ...] = Field(description="One mark per column, in column order")


class Grid(FrozenModel):
    """The grid of case by column, or nothing at all when no column graded a single case."""

    headings: tuple[str, ...] = Field(description="The header row: the case column, then each column's label")
    rows: tuple[GridRow, ...] = Field(description="One row per case; empty when there is no grid to draw")


class JudgeCriterion(FrozenModel):
    """One column of a judge table: which dimension scored the criterion, and the criterion itself.

    The dimension is part of a column's identity rather than decoration. Two dimensions may score
    criteria of the same name, and the dimension is also what says whether a score is about the
    product or about the harness that drove it.
    """

    dimension: str = Field(description="The rewardkit dimension the criterion was scored under")
    criterion: str = Field(description="The judge criterion's name, e.g. 'conciseness'")


class JudgeTable(FrozenModel):
    """Every graded trial of a pair in one table: which arm ran it, what it earned, how the judges
    scored it, and what became of it.

    One table rather than one per harness config, so a criterion can be compared straight down its
    own column, and so the message does not grow two blocks per config. Each row leads with its
    config, which is why the block builder styles the first column rather than a named field.
    """

    criteria: tuple[JudgeCriterion, ...] = Field(
        description="The judge table's criterion columns, in first-seen order, capped to what a row fits"
    )
    rows: tuple[tuple[str, ...], ...] = Field(
        description="One row per graded trial: its config, case, reward, each score, then its state"
    )
    total_criteria: int = Field(description="How many criteria were scored in all, before the cap")
    dropped_rows: int = Field(description="How many graded trials the message's table budget left no room for")


class FailedTrial(FrozenModel):
    """One trial that did not pass, as the failures table lists it."""

    config: str = Field(description="The harness config whose pass ran the trial")
    case_key: str = Field(description="The case it ran")
    note: str = Field(description="What stopped it")


class SlackMessage(FrozenModel):
    """One posted message: the Block Kit blocks, and the plain mrkdwn that stands in for them."""

    text: str = Field(description="The whole report as mrkdwn, readable on its own")
    blocks: tuple[dict[str, Any], ...] = Field(description="The message's Block Kit blocks, in order")
    icon_emoji: str = Field(description="The shortcode of the avatar the message posts under")


# The grid of a pair that graded nothing. Nothing is drawn for it, and nothing is said about it: the
# pair's own section already says why there is no grid.
EMPTY_GRID: Final[Grid] = Grid(headings=(), rows=())

# The judge table of a pair that graded nothing.
EMPTY_JUDGE_TABLE: Final[JudgeTable] = JudgeTable(criteria=(), rows=(), total_criteria=0, dropped_rows=0)


# The stand-in for a pass that has no summary of its own to read.
ABSENT_SUMMARY: Final[UnreadSummary] = UnreadSummary(is_summary_absent=True)


@pure
def without_computed_fields(payload: Any, model_type: type[BaseModel]) -> Any:
    """A serialized model's keys with the model's computed fields dropped.

    `model_dump_json` writes computed fields and `FrozenModel` forbids extra keys, so a model
    carrying any does not validate back from its own dump. A payload that is not an object at all
    passes through untouched, so that pydantic is what reports it rather than an attribute error
    here.
    """
    if not isinstance(payload, Mapping):
        return payload
    return {key: value for key, value in payload.items() if key not in model_type.model_computed_fields}


@pure
def parse_run_check(summary_text: str) -> RunCheck:
    """check-run's JSON summary, back into the model it was dumped from.

    Raises json.JSONDecodeError for text that is not JSON and ValidationError for JSON that is not a
    run summary; every caller turns both into "could not be read".
    """
    payload = json.loads(summary_text)
    raw_trials = payload.get("trials") if isinstance(payload, Mapping) else None
    if not isinstance(raw_trials, list):
        return RunCheck.model_validate(payload)
    trials = [without_computed_fields(trial, TrialCheck) for trial in raw_trials]
    return RunCheck.model_validate({**without_computed_fields(payload, RunCheck), "trials": trials})


@pure
def oracle_summary_path(summaries_dir: Path, pair_name: str) -> Path:
    """Where the pair's oracle pass uploaded its summary."""
    return summaries_dir / "{}-{}.json".format(ORACLE_SUMMARY_STEM, pair_name)


@pure
def live_summary_path(summaries_dir: Path, pair_name: str, harness_config: str) -> Path:
    """Where the cell's live pass uploaded its summary."""
    return summaries_dir / "{}-{}-{}.json".format(LIVE_SUMMARY_STEM, pair_name, harness_config)


def read_summary(summary_path: Path) -> SummaryReading:
    """One pass's summary artifact, tolerating both its absence and its corruption.

    Only the file is tolerated. OSError is a file this job could not read, and every way its
    content can be wrong -- malformed JSON, a decoding failure, a payload that is not a run summary
    -- raises a ValueError, pydantic's ValidationError included. A bug in the parsing itself still
    raises, because reporting one as "the summary could not be read" would send the reader to the
    run's artifacts to look for a problem that is in this module.
    """
    if not summary_path.is_file():
        return UnreadSummary(is_summary_absent=True)
    try:
        return parse_run_check(summary_path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read the run summary at {}: {}", summary_path, exc)
        return UnreadSummary(is_summary_absent=False)


def read_ci_matrix(matrix_path: Path | None) -> CiMatrix | None:
    """What the run decided to evaluate, or None when nothing says.

    A matrix that was never written and one that cannot be read are the same claim here, unlike the
    per-pass summaries: either way the run never published a decision, and there is no arm to
    attribute the failure to.

    Narrow for the same reason `read_summary` is: the file is what is being tolerated, not this
    module's own reading of it.
    """
    if matrix_path is None or not matrix_path.is_file():
        return None
    try:
        return CiMatrix.model_validate(without_computed_fields(json.loads(matrix_path.read_text()), CiMatrix))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read the decided matrix at {}: {}", matrix_path, exc)
        return None


@pure
def format_duration(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "an unknown time"
    if duration_seconds < 60:
        return "{}s".format(duration_seconds)
    return "{}m{:02d}s".format(duration_seconds // 60, duration_seconds % 60)


@pure
def format_ref_label(ref: str, sha: str) -> str:
    """One half of a pair's label: the ref it was asked for and the SHA it froze to.

    A ref that is already a full SHA says nothing the short SHA does not, so it is not repeated; the
    freeze step echoes such a ref back unchanged, which is why equality is what tells them apart --
    a branch or tag name is held to no length, and one that happens to be 40 characters long is
    still a name worth printing. A pair that never resolved has a ref but no SHA, and the label has
    to say so rather than render an empty pair of backticks.
    """
    short_sha = sha[:SHORT_SHA_LENGTH]
    if not short_sha:
        return "`{}` (`unresolved`)".format(ref) if ref else "`unresolved`"
    if not ref or ref == sha:
        return "`{}`".format(short_sha)
    return "`{}` (`{}`)".format(ref, short_sha)


@pure
def format_pair_label(pair: FrozenPair) -> str:
    return "[_mngr_ {} | _dwt_ {}]".format(
        format_ref_label(pair.mngr_ref, pair.mngr_sha), format_ref_label(pair.dwt_ref, pair.dwt_sha)
    )


@pure
def is_verdict_green(verdict: ArmVerdict) -> bool:
    """Whether the verdict is one nobody has to read further about.

    A pair whose every cell was skipped as already green is green: it was verified, just not
    tonight. Everything else -- measured shortfall, broken pass, arm nothing was attempted on -- is
    something a reader has to act on.
    """
    match verdict:
        case ArmVerdict.PASSED | ArmVerdict.SKIPPED:
            return True
        case ArmVerdict.FAILED | ArmVerdict.BROKEN | ArmVerdict.NOT_EVALUATED:
            return False
        case _ as unreachable:
            assert_never(unreachable)


@pure
def format_verdict_emoji(verdict: ArmVerdict) -> str:
    return PASS_EMOJI if is_verdict_green(verdict) else FAIL_EMOJI


@pure
def format_verdict_icon(verdict: ArmVerdict) -> str:
    """The avatar the message posts under. Read off the same verdict the header spells out, so the
    icon in the channel list and the words in the message can never say different things."""
    return GREEN_ICON_EMOJI if is_verdict_green(verdict) else UNGREEN_ICON_EMOJI


@pure
def format_verdict_word(verdict: ArmVerdict) -> str:
    """The verdict as the message spells it out."""
    match verdict:
        case ArmVerdict.PASSED:
            return "passed"
        case ArmVerdict.FAILED:
            return "failed"
        case ArmVerdict.SKIPPED:
            return "skipped (already green)"
        case ArmVerdict.NOT_EVALUATED:
            return "not evaluated"
        case ArmVerdict.BROKEN:
            return "broken"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def combine_verdicts(verdicts: Sequence[ArmVerdict]) -> ArmVerdict:
    """How a pair reads once its cells are in: the worst thing that happened to any of its arms.

    A measured shortfall outranks an arm whose story cannot be told, because it is the one a reader
    can act on. The all-skipped case is what a pair of only-green cells would read as; a pair the
    run skipped whole never reaches here, because its decision already says so and it has no arms.
    """
    for candidate in (ArmVerdict.FAILED, ArmVerdict.BROKEN, ArmVerdict.NOT_EVALUATED):
        if candidate in verdicts:
            return candidate
    if verdicts and all(verdict is ArmVerdict.SKIPPED for verdict in verdicts):
        return ArmVerdict.SKIPPED
    return ArmVerdict.PASSED


@pure
def format_trial_state(trial: TrialCheck) -> str:
    """What happened to one trial, in the order the reasons matter.

    The model comes before the measurements: a trial that answered on another model than its arm
    asked for measured the wrong thing, so its gates and its evidence say nothing about the arm.
    """
    if not trial.is_completed:
        return "did not complete ({})".format(trial.incompletion_reason or "unknown")
    elif trial.wrong_model_reason:
        return trial.wrong_model_reason
    elif trial.error_entry_ids:
        return "unmeasured evidence: {}".format(", ".join(trial.error_entry_ids))
    elif not trial.is_gates_passed:
        return "gates failed"
    else:
        return "ok"


@pure
def is_model_unconfirmed(trial: TrialCheck) -> bool:
    """Whether a passing trial leaves the model its arm asked for unconfirmed.

    `null` is silence rather than evidence of a wrong model -- no transcript was captured, or the
    catalog id has no known reported name -- so the trial still passes, and the reader has to be
    told, or a green arm reads as measured on the model it names.
    """
    return trial.is_passed and bool(trial.requested_model) and trial.is_model_confirmed is None


@pure
def format_case_key(trial: TrialCheck) -> str:
    """Which grid row a trial belongs to. A trial that never reached the point of recording its case
    still has a name of its own, and a row of its own is better than being folded into another."""
    return trial.case_id or trial.trial_name or UNKNOWN_MARK


@pure
def describe_unread_summary(reading: UnreadSummary, pass_name: str) -> str:
    """Why a pass has no verdict: it never got to grading, or what it wrote cannot be read."""
    if reading.is_summary_absent:
        return "broken (no {} summary; the job failed before grading)".format(pass_name)
    return "broken (the {} summary could not be read)".format(pass_name)


@pure
def describe_oracle(reading: SummaryReading) -> str:
    """What the pair's section line says about its oracle pass."""
    if isinstance(reading, UnreadSummary):
        return describe_unread_summary(reading, ORACLE_LABEL)
    return "oracle passed" if reading.is_passed else "oracle failed"


@pure
def render_oracle_pass(reading: SummaryReading) -> PassReport:
    """The pair's oracle pass as a pass of its own.

    It carries no detail line: its status is already on the pair's section line, and repeating it
    under the cells would read as a fourth cell. Its trials are still listed there like any others,
    which is what a failed oracle's message is mostly made of.
    """
    if isinstance(reading, UnreadSummary):
        return PassReport(label=ORACLE_LABEL, verdict=ArmVerdict.BROKEN, detail="", trials=())
    verdict = ArmVerdict.PASSED if reading.is_passed else ArmVerdict.FAILED
    return PassReport(label=ORACLE_LABEL, verdict=verdict, detail="", trials=reading.trials)


@pure
def render_running_cell_pass(cell: MatrixCell, live: SummaryReading, is_oracle_passed: bool) -> PassReport:
    """One cell the run meant to evaluate.

    A cell whose pair's oracle did not pass never started, so it is reported as not evaluated rather
    than as a missing summary -- but only when it wrote no summary at all. One that wrote a summary
    which cannot be read got further than the oracle's story allows, and saying otherwise would name
    a pass that never ran.
    """
    if isinstance(live, UnreadSummary):
        if live.is_summary_absent and not is_oracle_passed:
            return PassReport(
                label=cell.harness_config,
                verdict=ArmVerdict.NOT_EVALUATED,
                detail="not evaluated (the oracle pass did not pass)",
                trials=(),
            )
        return PassReport(
            label=cell.harness_config,
            verdict=ArmVerdict.BROKEN,
            detail=describe_unread_summary(live, "live"),
            trials=(),
        )
    verdict = ArmVerdict.PASSED if live.is_passed else ArmVerdict.FAILED
    return PassReport(label=cell.harness_config, verdict=verdict, detail="", trials=live.trials)


@pure
def render_cell_pass(cell: MatrixCell, live: SummaryReading, is_oracle_passed: bool) -> PassReport:
    """One cell: the harness config it ran the pair on, and how that live pass came out."""
    match cell.decision:
        case CellDecision.SKIP:
            return PassReport(
                label=cell.harness_config,
                verdict=ArmVerdict.SKIPPED,
                detail="skipped (already green)",
                trials=(),
            )
        case CellDecision.RUN:
            return render_running_cell_pass(cell, live, is_oracle_passed)
        case _ as unreachable:
            assert_never(unreachable)


def read_cell_passes(
    pair: DecidedPair, matrix: CiMatrix, summaries_dir: Path, oracle: PassReport
) -> tuple[PassReport, ...]:
    """Every cell of one pair, in matrix order."""
    is_oracle_passed = oracle.verdict is ArmVerdict.PASSED
    passes: list[PassReport] = []
    for cell in matrix.cells:
        if cell.pair != pair.pair:
            continue
        live = (
            ABSENT_SUMMARY
            if cell.decision is CellDecision.SKIP
            else read_summary(live_summary_path(summaries_dir, pair.pair, cell.harness_config))
        )
        passes.append(render_cell_pass(cell, live, is_oracle_passed))
    return tuple(passes)


def read_running_pair_report(
    pair: DecidedPair, matrix: CiMatrix, summaries_dir: Path, context: CiReportContext
) -> PairReport:
    """A pair the run evaluated: its oracle pass is what the pair itself is judged on, and its cells
    are what the grid is made of."""
    reading = read_summary(oracle_summary_path(summaries_dir, pair.pair))
    oracle = render_oracle_pass(reading)
    # A run that stops after the oracle passes has cells in its matrix that never ran; putting a
    # verdict on them would claim a live pass nobody paid for.
    cells = () if context.is_live_pass_skipped else read_cell_passes(pair, matrix, summaries_dir, oracle)
    return PairReport(
        pair=pair,
        verdict=combine_verdicts((oracle.verdict, *(cell.verdict for cell in cells))),
        summary_text=describe_oracle(reading),
        oracle=oracle,
        cells=cells,
    )


def read_pair_report(pair: DecidedPair, matrix: CiMatrix, summaries_dir: Path, context: CiReportContext) -> PairReport:
    """One pair's whole message, read out of the matrix and whatever summaries reached the run."""
    match pair.decision:
        case PairDecision.UNRESOLVED:
            # Reported rather than aborting the run, so a missing release tag never costs the other
            # pairs.
            return PairReport(
                pair=pair,
                verdict=ArmVerdict.NOT_EVALUATED,
                summary_text="a ref did not resolve",
                oracle=None,
                cells=(),
            )
        case PairDecision.SKIP:
            # The emoji answers "is this pair good?", which a skip does not change.
            return PairReport(pair=pair, verdict=ArmVerdict.SKIPPED, summary_text="", oracle=None, cells=())
        case PairDecision.RUN:
            return read_running_pair_report(pair, matrix, summaries_dir, context)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def grid_columns(report: PairReport) -> tuple[PassReport, ...]:
    """The columns of a pair's grid: its cells, or its oracle pass when it has no cells.

    An oracle-only run pays for no cell, so the oracle's own trials are the only grid there is.
    """
    if report.cells:
        return report.cells
    return () if report.oracle is None else (report.oracle,)


@pure
def detail_passes(report: PairReport) -> tuple[PassReport, ...]:
    """Every pass whose trials and status the details block draws on, oracle first."""
    return report.cells if report.oracle is None else (report.oracle, *report.cells)


@pure
def judge_passes(report: PairReport) -> tuple[PassReport, ...]:
    """Every grid column that graded something, which is what a judge table can be made of.

    A skipped, not-evaluated or broken cell graded nothing and gets no section of its own; the
    details block is where the reader is told what became of it.
    """
    return tuple(column for column in grid_columns(report) if column.trials)


@pure
def find_case_trial(column: PassReport, case_key: str) -> TrialCheck | None:
    """The column's trial for one case. A pass runs each case once, so the first match is the one."""
    for trial in column.trials:
        if format_case_key(trial) == case_key:
            return trial
    return None


@pure
def band_emoji_name(reward: float) -> str:
    """Which square a reward wears: the first band whose ceiling it does not reach."""
    for ceiling, emoji_name in REWARD_BANDS:
        if reward < ceiling:
            return emoji_name
    return TOP_BAND_EMOJI_NAME


@pure
def render_grid_mark(column: PassReport, case_key: str) -> GridMark:
    """One cell of the grid: what the pass made of that case, or why it says nothing about it.

    The square says where the reward sits on 0..1 and the mark beside it says whether the trial
    passed, so the colour means one thing throughout and the verdict never competes with it.

    A pass that was never attempted -- skipped because it is already green, or gated off by a failed
    oracle -- reads as a dash. Anything else without a trial is a question mark: a broken summary,
    or a graded pass that has no trial for a case another column ran.
    """
    trial = find_case_trial(column, case_key)
    if trial is None:
        if column.verdict in (ArmVerdict.SKIPPED, ArmVerdict.NOT_EVALUATED):
            return GridMark(
                emoji_name=NOT_EVALUATED_EMOJI_NAME, word=NOT_EVALUATED_MARK, reward_text="", verdict_mark=""
            )
        return GridMark(emoji_name=UNKNOWN_EMOJI_NAME, word=UNKNOWN_MARK, reward_text="", verdict_mark="")
    # A trial that was never graded has no reward to place on the scale, so it keeps the neutral
    # mark rather than being coloured as though it had scored bottom.
    emoji_name = UNKNOWN_EMOJI_NAME if trial.reward is None else band_emoji_name(trial.reward)
    reward_text = "" if trial.reward is None else " {:.2f}".format(trial.reward)
    if trial.is_passed:
        return GridMark(emoji_name=emoji_name, word=PASS_MARK, reward_text=reward_text, verdict_mark=PASSED_SPACER)
    return GridMark(emoji_name=emoji_name, word=FAIL_MARK, reward_text=reward_text, verdict_mark=FAILED_MARK)


@pure
def format_mark_text(mark: GridMark) -> str:
    """One grid cell as the fallback prints it: the word the emoji stands for, and the reward."""
    return "{}{}".format(mark.word, mark.reward_text)


@pure
def collect_case_keys(columns: Sequence[PassReport]) -> tuple[str, ...]:
    """The grid's rows: every case any column graded, in the order the columns first mention them."""
    case_keys: list[str] = []
    for column in columns:
        for trial in column.trials:
            case_key = format_case_key(trial)
            if case_key not in case_keys:
                case_keys.append(case_key)
    return tuple(case_keys)


@pure
def render_grid(columns: Sequence[PassReport]) -> Grid:
    """The whole grid, or the empty one when no column graded a single case.

    Slack caps a table at 100 rows and 20 columns; a run wide or long enough to exceed either has
    its blocks refused, and the text fallback the workflow posts instead carries the same grid.
    """
    case_keys = collect_case_keys(columns)
    if not case_keys:
        return EMPTY_GRID
    return Grid(
        headings=(GRID_CASE_HEADING, *(column.label for column in columns)),
        rows=tuple(
            GridRow(case_key=case_key, marks=tuple(render_grid_mark(column, case_key) for column in columns))
            for case_key in case_keys
        ),
    )


@pure
def format_grid_text_rows(grid: Grid) -> tuple[tuple[str, ...], ...]:
    """The grid as rows of plain text, for the fallback that has no table block to render it with."""
    return (
        grid.headings,
        *((row.case_key, *(format_mark_text(mark) for mark in row.marks)) for row in grid.rows),
    )


@pure
def format_grid_table_cells(grid: Grid) -> tuple[tuple[str, ...], ...]:
    """The grid's cells as the table block spells them, for counting against Slack's budget.

    Not `format_grid_text_rows`, which is the fallback's spelling: that one prints the verdict as a
    word and leaves the case key uncut, so counting it would charge the budget for characters no
    table cell holds.
    """
    return (
        grid.headings,
        *(
            (
                clamp_cell_text(row.case_key),
                *("{}{}".format(mark.reward_text, mark.verdict_mark) for mark in row.marks),
            )
            for row in grid.rows
        ),
    )


@pure
def format_trial_note(trial: TrialCheck, note: str) -> str:
    """One line about one trial: which case it ran, and what is being said about it."""
    return "`{}`: {}".format(format_case_key(trial), note)


@pure
def format_unconfirmed_model_note(trial: TrialCheck) -> str:
    return "model {} unconfirmed".format(trial.requested_model)


@pure
def format_status_lines(columns: Sequence[PassReport]) -> tuple[str, ...]:
    """One line per pass that has no grid column to speak for it."""
    return tuple("*{}* -- {}".format(column.label, column.detail) for column in columns if column.detail)


@pure
def format_failure_lines(columns: Sequence[PassReport]) -> tuple[str, ...]:
    """One line per failing trial, naming what stopped it. The grid says only that it failed."""
    return tuple(
        "*{}* {}".format(column.label, format_trial_note(trial, format_trial_state(trial)))
        for column in columns
        for trial in column.trials
        if not trial.is_passed
    )


@pure
def format_unconfirmed_model_lines(columns: Sequence[PassReport]) -> tuple[str, ...]:
    """One line per passing trial whose arm's model nothing confirmed."""
    return tuple(
        "*{}* {}".format(column.label, format_trial_note(trial, format_unconfirmed_model_note(trial)))
        for column in columns
        for trial in column.trials
        if is_model_unconfirmed(trial)
    )


@pure
def format_detail_lines(report: PairReport) -> tuple[str, ...]:
    """Everything neither the grid nor a pass's own section says, grouped so a reader can stop after
    the first group.

    A graded pass's failures are in the failures table, so what is left here is the passes with no
    column of their own: the cells that graded nothing, and above all a failed oracle, whose trials
    are what its pair's message is mostly made of.

    The unconfirmed models are every pass's, graded or not. Nothing else in the message says a
    passing arm was never confirmed to have answered on the model it names, and a green arm that
    measured the wrong model is the one thing here a reader must not miss.
    """
    columns = detail_passes(report)
    graded = judge_passes(report)
    uncovered = tuple(column for column in columns if column not in graded)
    return (
        *format_status_lines(columns),
        *format_failure_lines(uncovered),
        *format_unconfirmed_model_lines(columns),
    )


@pure
def format_budgeted_block(lines: Sequence[str]) -> str:
    """The detail lines as one budgeted block, or nothing when there are none.

    The cut lands on a line boundary. A raw character cut ends mid-word or inside a `:emoji:` token,
    which Slack then renders as literal text beside the truncation notice. The first line is kept
    whatever it says, so a block of one enormous line still says something about it, and is the only
    line the budget cuts mid-word.

    The truncation notice and the newline before it come out of the budget rather than sitting on
    top of it, so what is returned is never longer than DETAIL_BUDGET.
    """
    if not lines:
        return ""
    detail = "\n".join(lines)
    if len(detail) <= DETAIL_BUDGET:
        return detail
    kept_budget = DETAIL_BUDGET - len(TRUNCATION_NOTICE) - 1
    kept = [lines[0][:kept_budget]]
    length = len(kept[0])
    for line in lines[1:]:
        if length + len(line) + 1 > kept_budget:
            break
        kept.append(line)
        length += len(line) + 1
    return "\n".join([*kept, TRUNCATION_NOTICE])


@pure
def format_fixed_width_grid(rows: Sequence[Sequence[str]]) -> str:
    """The grid as text, for the fallback that has no table block to render it with."""
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows
    )


@pure
def collect_judge_criteria(trials: Sequence[TrialCheck]) -> tuple[JudgeCriterion, ...]:
    """Every criterion a pass's judges scored, in the order its trials first mention them.

    The union across the trials rather than the first trial's own, because a case can be scored on
    criteria another case is not: the dataset's cases carry their own expectations.
    """
    criteria: list[JudgeCriterion] = []
    for trial in trials:
        for score in trial.judge_scores:
            criterion = JudgeCriterion(dimension=score.dimension, criterion=score.criterion)
            if criterion not in criteria:
                criteria.append(criterion)
    return tuple(criteria)


@pure
def format_criterion_heading(criterion: JudgeCriterion) -> str:
    return "{}: {}".format(criterion.dimension, criterion.criterion)


@pure
def clamp_cell_text(text: str) -> str:
    """One table cell's text, cut to what a single cell may run to."""
    if len(text) <= MAX_TABLE_CELL_CHARACTERS:
        return text
    return text[: MAX_TABLE_CELL_CHARACTERS - len(TABLE_ELLIPSIS)] + TABLE_ELLIPSIS


@pure
def count_table_characters(rows: Sequence[Sequence[str]]) -> int:
    return sum(len(cell) for row in rows for cell in row)


@pure
def format_criterion_column_heading(criterion: JudgeCriterion, criteria: Sequence[JudgeCriterion]) -> str:
    """One criterion's column heading.

    Bare, because the dimensions are stated once in the legend above the table and qualifying every
    heading makes the columns far wider than the numbers under them. A criterion name that two
    dimensions both scored is the exception: those columns carry their dimension, or the table has
    two columns of one name and no way to tell which score belongs to which.
    """
    if sum(1 for other in criteria if other.criterion == criterion.criterion) > 1:
        return format_criterion_heading(criterion)
    return criterion.criterion


@pure
def format_judge_headings(criteria: Sequence[JudgeCriterion]) -> tuple[str, ...]:
    """The judge table's header row."""
    return (
        JUDGE_CONFIG_HEADING,
        GRID_CASE_HEADING,
        JUDGE_REWARD_HEADING,
        *(format_criterion_column_heading(criterion, criteria) for criterion in criteria),
        JUDGE_STATE_HEADING,
    )


@pure
def format_criterion_score(trial: TrialCheck, criterion: JudgeCriterion) -> str:
    """One criterion's own likert answer on one trial, as the judge gave it.

    A dash rather than a zero where the trial has no such criterion: the columns are the union across
    the pass's trials, and a case that was never scored on a criterion is not a case that scored
    bottom on it.
    """
    for score in trial.judge_scores:
        if score.dimension == criterion.dimension and score.criterion == criterion.criterion:
            return "{:.0f}".format(score.raw_score)
    return MISSING_SCORE_MARK


@pure
def format_judge_row(config: str, trial: TrialCheck, criteria: Sequence[JudgeCriterion]) -> tuple[str, ...]:
    """One trial's row of the judge table.

    The reward carries no band and no failure mark. The summary table above already says both, and
    a second set of verdict glyphs is noise in a table read for its numbers.
    """
    reward = MISSING_SCORE_MARK if trial.reward is None else "{:.2f}".format(trial.reward)
    return (
        clamp_cell_text(config),
        clamp_cell_text(format_case_key(trial)),
        reward,
        *(format_criterion_score(trial, criterion) for criterion in criteria),
        clamp_cell_text(format_trial_state(trial)),
    )


@pure
def format_criteria_overflow_line(table: JudgeTable) -> str:
    """What the table says out loud when it lost columns to Slack's cap on a table row.

    Silently dropping them would leave a table that reads as the whole of what the judges scored.
    """
    if table.total_criteria <= len(table.criteria):
        return ""
    return "_showing {} of {} judge criteria; the rest are in the run's summary_".format(
        len(table.criteria), table.total_criteria
    )


@pure
def render_judge_table(columns: Sequence[PassReport]) -> JudgeTable:
    """Every graded trial of a pair, in one table.

    The criteria are the union across every pass rather than one pass's own, because the arms are
    read against each other here: a criterion only one config was scored on still gets a column,
    and the configs that were not scored on it say so rather than showing a zero.
    """
    criteria = collect_judge_criteria(tuple(trial for column in columns for trial in column.trials))
    kept_criteria = criteria[:MAX_JUDGE_CRITERIA]
    return JudgeTable(
        criteria=kept_criteria,
        rows=tuple(
            format_judge_row(column.label, trial, kept_criteria) for column in columns for trial in column.trials
        ),
        total_criteria=len(criteria),
        dropped_rows=0,
    )


@pure
def budget_judge_table(table: JudgeTable, spent: int) -> JudgeTable:
    """The judge table cut to the table budget the rest of the message left it.

    Whole rows, so that what survives is still a table: a row cut in half would line its scores up
    under the wrong headings. The header row is counted first, because a table of headings alone is
    worth nothing and must not be what the budget buys.
    """
    headings = format_judge_headings(table.criteria)
    remaining = MAX_TABLE_CHARACTERS - spent - count_table_characters((headings,))
    kept: list[tuple[str, ...]] = []
    for row in table.rows:
        cost = count_table_characters((row,))
        if cost > remaining:
            break
        remaining -= cost
        kept.append(row)
    return JudgeTable(
        criteria=table.criteria,
        rows=tuple(kept),
        total_criteria=table.total_criteria,
        dropped_rows=len(table.rows) - len(kept),
    )


@pure
def format_dropped_rows_line(table: JudgeTable) -> str:
    """What the table says out loud when the message's character budget cost it rows.

    A budget that leaves room for no row at all leaves no table to say it in either, so the wording
    stands on its own: `build_message` puts this line where the container would have gone.
    """
    if not table.dropped_rows:
        return ""
    if not table.rows:
        return "_the judge scores did not fit this message; they are in the run's summary_"
    return "_showing {} of {} graded trials; the rest are in the run's summary_".format(
        len(table.rows), len(table.rows) + table.dropped_rows
    )


@pure
def render_failed_trials(columns: Sequence[PassReport]) -> tuple[FailedTrial, ...]:
    """Every graded trial that did not pass, in column order. The summary table says only that a
    trial failed; this is where a reader learns what stopped it."""
    return tuple(
        FailedTrial(
            config=clamp_cell_text(column.label),
            case_key=clamp_cell_text(format_case_key(trial)),
            note=clamp_cell_text(format_trial_state(trial)),
        )
        for column in columns
        for trial in column.trials
        if not trial.is_passed
    )


@pure
def format_criteria_legend(criteria: Sequence[JudgeCriterion]) -> str:
    """Which dimension scored which criteria, said once above the table rather than in every
    heading."""
    dimensions: list[str] = []
    for criterion in criteria:
        if criterion.dimension not in dimensions:
            dimensions.append(criterion.dimension)
    return "_criteria by dimension:_ {}".format(
        "; ".join(
            "{}: {}".format(
                dimension,
                ", ".join(criterion.criterion for criterion in criteria if criterion.dimension == dimension),
            )
            for dimension in dimensions
        )
    )


@pure
def format_judge_legend_lines(table: JudgeTable) -> tuple[str, ...]:
    """What is said above the judge table: which dimension scored which criteria, and then whatever
    the table had to give up to Slack's caps."""
    return tuple(
        line
        for line in (
            format_criteria_legend(table.criteria),
            format_criteria_overflow_line(table),
            format_dropped_rows_line(table),
        )
        if line
    )


@pure
def format_judge_fallback(table: JudgeTable) -> tuple[str, ...]:
    """The judge table as the fallback carries it: the legend, then a fixed-width fence."""
    if not table.rows:
        return (format_dropped_rows_line(table),) if table.dropped_rows else ()
    return (
        *format_judge_legend_lines(table),
        "```\n{}\n```".format(format_fixed_width_grid((format_judge_headings(table.criteria), *table.rows))),
    )


@pure
def format_failed_trials_rows(failures: Sequence[FailedTrial]) -> tuple[tuple[str, ...], ...]:
    """The failures table as rows of plain text, header row first; empty when nothing failed."""
    if not failures:
        return ()
    return (
        (JUDGE_CONFIG_HEADING, GRID_CASE_HEADING, FAILED_TRIALS_NOTE_HEADING),
        *((failure.config, failure.case_key, failure.note) for failure in failures),
    )


@pure
def format_failed_trials_fallback(failures: Sequence[FailedTrial]) -> tuple[str, ...]:
    """The failures table as the fallback carries it, under the same heading the blocks give it."""
    rows = format_failed_trials_rows(failures)
    if not rows:
        return ()
    return (FAILED_TRIALS_HEADING, "```\n{}\n```".format(format_fixed_width_grid(rows)))


@pure
def format_reward_scale_note() -> str:
    """The legend for the summary table's squares.

    Derived from the bands themselves, so that the words under the table cannot drift from what its
    cells actually do.
    """
    bands = [":{}: `<{:.2f}`".format(emoji_name, ceiling) for ceiling, emoji_name in REWARD_BANDS]
    bands.append(":{}: `>={:.2f}`".format(TOP_BAND_EMOJI_NAME, REWARD_BANDS[-1][0]))
    return "_reward_ {}  --  *{}* = the trial failed its gates".format(" ".join(bands), FAILED_MARK.strip())


@pure
def describe_unhealthy_jobs(context: CiReportContext) -> tuple[str, ...]:
    """Every job of the run that ended outside success, as `<job>=<result>`.

    The trials passing is not the whole story: the passes also delete the run's Modal environments
    and upload its artifacts, both unconditionally, and either failing turns a job red while every
    summary still says the eval itself was fine.
    """
    results = (
        ("resolve", context.resolve_result),
        ("oracle", context.oracle_result),
        ("evaluate", context.evaluate_result),
    )
    return tuple("{}={}".format(name, result) for name, result in results if result not in HEALTHY_JOB_RESULTS)


@pure
def format_job_note(context: CiReportContext, is_only_the_job_unhealthy: bool) -> str:
    """The warning for a run whose jobs went red with nothing to blame it on.

    A failing arm is what turns a job red in the ordinary case, so an unhealthy job on its own says
    nothing, and pointing at cleanup and uploads on every red run would train the reader to ignore
    it.
    """
    if not is_only_the_job_unhealthy:
        return ""
    return " _every arm passed, but the job did not ({}; check cleanup and uploads)_".format(
        ", ".join(describe_unhealthy_jobs(context))
    )


@pure
def format_pair_section(
    report: PairReport, context: CiReportContext, emoji: str, is_only_the_job_unhealthy: bool
) -> str:
    """The pair's own two lines: which commits it froze to and what became of them, then the run."""
    label_line = "{} {}".format(emoji, format_pair_label(report.pair))
    if report.summary_text:
        label_line = "{} -- {}".format(label_line, report.summary_text)
    run_line = "{} in {}, _trigger=_ `{}`{}{}".format(
        format_verdict_word(report.verdict),
        format_duration(context.duration_seconds),
        context.trigger,
        " _(oracle only)_" if context.is_live_pass_skipped else "",
        format_job_note(context, is_only_the_job_unhealthy),
    )
    return "{}\n{}".format(label_line, run_line)


@pure
def format_links_line(run_url: str) -> str:
    return "<{}|run logs> | <{}#artifacts|artifacts>".format(run_url, run_url)


@pure
def build_header_block(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text}}


@pure
def build_section_block(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


@pure
def build_context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


@pure
def build_raw_text_cell(text: str) -> dict[str, Any]:
    return {"type": "raw_text", "text": text}


@pure
def build_bold_cell(text: str) -> dict[str, Any]:
    """A table cell in bold. Harness configs are named this way wherever they appear in a table, so
    that the arm a row or column belongs to is what the eye lands on first."""
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text, "style": {"bold": True}}],
            }
        ],
    }


@pure
def build_mark_cell(mark: GridMark) -> dict[str, Any]:
    """One grid cell as a rich-text cell: the reward's band as a square, the reward, then the mark.

    The word the fallback prints is not repeated here, because the mark is already the verdict, and
    a cell with no reward to show carries the square alone rather than an empty text element.
    """
    elements: list[dict[str, Any]] = [{"type": "emoji", "name": mark.emoji_name}]
    if mark.reward_text:
        elements.append({"type": "text", "text": mark.reward_text})
    if mark.verdict_mark:
        elements.append({"type": "text", "text": mark.verdict_mark, "style": {"bold": True}})
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}


@pure
def build_container_block(title: str, subtitle: str, child_blocks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """A collapsed group of blocks."""
    assert len(child_blocks) <= MAX_CONTAINER_CHILD_BLOCKS, len(child_blocks)
    return {
        "type": "container",
        "title": {"type": "plain_text", "text": title},
        "subtitle": {"type": "plain_text", "text": subtitle},
        "is_collapsible": True,
        "default_collapsed": True,
        "child_blocks": list(child_blocks),
    }


@pure
def build_grid_block(grid: Grid) -> dict[str, Any]:
    """The grid as Slack's table block: the cases down the side, the configs across the top, and
    what each arm made of each case in between.

    The rewards are right-aligned so that they read as a column of numbers rather than as text that
    happens to be numeric.
    """
    return {
        "type": "table",
        "column_settings": [{"align": "left"}, *({"align": "right"} for _ in grid.headings[1:])],
        "rows": [
            [build_raw_text_cell(grid.headings[0]), *(build_bold_cell(heading) for heading in grid.headings[1:])],
            *(
                [build_raw_text_cell(clamp_cell_text(row.case_key)), *(build_mark_cell(mark) for mark in row.marks)]
                for row in grid.rows
            ),
        ],
    }


@pure
def build_judge_table_block(table: JudgeTable) -> dict[str, Any]:
    """The judge table as Slack's table block.

    The state column wraps and everything between the case and it is right-aligned, so the scores
    line up under their headings however long the states beside them run.
    """
    return {
        "type": "table",
        "column_settings": [
            {"align": "left"},
            {"align": "left"},
            *({"align": "right"} for _ in range(1 + len(table.criteria))),
            {"align": "left", "is_wrapped": True},
        ],
        "rows": [
            [build_raw_text_cell(heading) for heading in format_judge_headings(table.criteria)],
            *([build_bold_cell(row[0]), *(build_raw_text_cell(cell) for cell in row[1:])] for row in table.rows),
        ],
    }


@pure
def build_failed_trials_block(failures: Sequence[FailedTrial]) -> dict[str, Any]:
    """The failures as Slack's table block: the arm, the case, and what stopped it."""
    return {
        "type": "table",
        "column_settings": [{"align": "left"}, {"align": "left"}, {"align": "left", "is_wrapped": True}],
        "rows": [
            [
                build_raw_text_cell(heading)
                for heading in (JUDGE_CONFIG_HEADING, GRID_CASE_HEADING, FAILED_TRIALS_NOTE_HEADING)
            ],
            *(
                [
                    build_bold_cell(failure.config),
                    build_raw_text_cell(failure.case_key),
                    build_raw_text_cell(failure.note),
                ]
                for failure in failures
            ),
        ],
    }


@pure
def build_judge_blocks(table: JudgeTable) -> tuple[dict[str, Any], ...]:
    """What the message shows for the pair's judge scores.

    Three outcomes, and the last is not an oversight: a table with rows is folded into a container,
    a table the message's character budget emptied says so where that container would have gone, and
    a pair that graded nothing has no scores to show and draws neither.
    """
    if table.rows:
        return (
            build_container_block(
                JUDGE_CONTAINER_TITLE,
                JUDGE_CONTAINER_SUBTITLE,
                (
                    build_context_block("\n".join(format_judge_legend_lines(table))),
                    build_judge_table_block(table),
                ),
            ),
        )
    elif table.dropped_rows:
        return (build_context_block(format_dropped_rows_line(table)),)
    else:
        return ()


@pure
def build_message(
    header_text: str,
    emoji: str,
    icon_emoji: str,
    pair_section: str,
    grid: Grid,
    judge_table: JudgeTable,
    failures: Sequence[FailedTrial],
    details_section: str,
    links: str,
) -> SlackMessage:
    """One message from its parts, as blocks and as the mrkdwn that stands in for them.

    The order is the order a reader asks the questions in: which pair, how did each case do on each
    arm, what went wrong, and only then every number behind it. The last of those is collapsed,
    because a green run is read for its first two blocks alone.
    """
    blocks: list[dict[str, Any]] = [build_header_block(header_text), build_section_block(pair_section)]
    text_parts = ["{} *{}*".format(emoji, header_text), pair_section]
    if grid.rows:
        blocks.append(build_grid_block(grid))
        blocks.append(build_context_block(format_reward_scale_note()))
        text_parts.append("```\n{}\n```".format(format_fixed_width_grid(format_grid_text_rows(grid))))
    if failures:
        blocks.append(build_section_block(FAILED_TRIALS_HEADING))
        blocks.append(build_failed_trials_block(failures))
    text_parts.extend(format_failed_trials_fallback(failures))
    blocks.extend(build_judge_blocks(judge_table))
    text_parts.extend(format_judge_fallback(judge_table))
    if details_section:
        blocks.append(build_section_block(details_section))
        text_parts.append(details_section)
    blocks.append(build_context_block(links))
    text_parts.append(links)
    return SlackMessage(text="\n".join(text_parts), blocks=tuple(blocks), icon_emoji=icon_emoji)


@pure
def render_pair_message(report: PairReport, context: CiReportContext, is_only_the_job_unhealthy: bool) -> SlackMessage:
    """One pair's whole message."""
    emoji = WARNING_EMOJI if is_only_the_job_unhealthy else format_verdict_emoji(report.verdict)
    details = format_budgeted_block(format_detail_lines(report))
    graded = judge_passes(report)
    grid = render_grid(grid_columns(report))
    failures = render_failed_trials(graded)
    spent = count_table_characters(format_grid_table_cells(grid)) + count_table_characters(
        format_failed_trials_rows(failures)
    )
    return build_message(
        "minds-evals: {} -- {}".format(report.pair.pair, format_verdict_word(report.verdict)),
        emoji,
        format_verdict_icon(report.verdict),
        format_pair_section(report, context, emoji, is_only_the_job_unhealthy),
        grid,
        budget_judge_table(render_judge_table(graded), spent),
        failures,
        "{}\n{}".format(DETAILS_HEADING, details) if details else "",
        format_links_line(context.run_url),
    )


@pure
def render_undecided_message(context: CiReportContext) -> SlackMessage:
    """What a run that never published a decision gets instead of pairs: the job results, which are
    the only thing left that says where it broke."""
    section = (
        "{} no pairs were resolved (resolve={}, oracle={}, evaluate={})"
        " -- the run broke before deciding what to evaluate".format(
            FAIL_EMOJI, context.resolve_result, context.oracle_result, context.evaluate_result
        )
    )
    return build_message(
        "minds-evals -- broken",
        FAIL_EMOJI,
        UNGREEN_ICON_EMOJI,
        section,
        EMPTY_GRID,
        EMPTY_JUDGE_TABLE,
        (),
        "",
        format_links_line(context.run_url),
    )


@pure
def as_slack_payload(message: SlackMessage) -> dict[str, Any]:
    """One message as the webhook takes it. The webhook carries no identity, so every post names
    itself, and `text` is both the notification line and what Slack falls back to."""
    return {
        "username": SLACK_USERNAME,
        "icon_emoji": message.icon_emoji,
        "text": message.text,
        "blocks": list(message.blocks),
    }


def render_slack_report(
    matrix_path: Path | None, summaries_dir: Path, context: CiReportContext
) -> tuple[SlackMessage, ...]:
    """Every message a scheduled run posts: one per pair, or one saying it decided nothing."""
    matrix = read_ci_matrix(matrix_path)
    if matrix is None or not matrix.pairs:
        return (render_undecided_message(context),)
    reports = tuple(read_pair_report(pair, matrix, summaries_dir, context) for pair in matrix.pairs)
    # Read across the whole run, not per pair: a red job that one pair's failure already explains is
    # not news in the other pair's message.
    is_any_arm_bad = any(
        report.verdict in (ArmVerdict.FAILED, ArmVerdict.BROKEN, ArmVerdict.NOT_EVALUATED) for report in reports
    )
    is_only_the_job_unhealthy = bool(describe_unhealthy_jobs(context)) and not is_any_arm_bad
    return tuple(render_pair_message(report, context, is_only_the_job_unhealthy) for report in reports)
