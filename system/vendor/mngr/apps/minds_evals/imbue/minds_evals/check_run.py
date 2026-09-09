"""Read a finished harbor job directory and decide whether the run passed.

A scheduled run has to answer one question in one exit code, and the artifacts that answer it are
spread across four files per trial: harbor's own ``result.json`` (did the trial run at all), the
driver's ``agent/state.json`` (did the conversation reach its end, or run out of time -- harbor
grades a timed-out trial as an ordinary result, so nothing else tells the two apart),
``verifier/reward-details.json`` (did the structural gates hold, what did the judges say) and
``agent/verification/manifest.json`` (was anything left unmeasured).

The gate is deliberately narrow. A trial is charged for not running and for failing a structural
gate; it is never charged for a judge score, which is statistical and drifts between runs. An
`error` in the evidence manifest fails the run because it is the harness that broke, not the
workspace -- the same `failed` versus `error` split the collector and the verifier already rest on.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from harbor.models.trial.result import TrialResult
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import JudgeScore
from imbue.minds_evals.data_types import RunCheck
from imbue.minds_evals.data_types import TrialCheck
from imbue.minds_evals.errors import JobReadError

# `agent/` is harbor's own trial layout (the driver's logs_dir is <trial dir>/agent), so it is
# spelled here rather than imported from anywhere.
_AGENT_DIRNAME: Final[str] = "agent"

_TRIAL_RESULT_FILENAME: Final[str] = "result.json"
_REWARD_DETAILS_PATH: Final[str] = "verifier/reward-details.json"
# Spelled out rather than taken from driver.STATE_FILENAME: importing the driver would pull
# harbor's agent base into the CLI's graph for one word, and this path drifting is loud anyway --
# a trial whose state.json is not found is charged for never writing one.
_AGENT_STATE_PATH: Final[str] = "{}/state.json".format(_AGENT_DIRNAME)
# Composed from the collector's own constants, the way generate.py composes it. Drift here would
# be SILENT: an unfound manifest is the "a trial that never reached a workspace writes none" case,
# which is deliberately quiet, so every trial would report nothing unmeasured -- the one verdict
# this gate exists to deny.
_EVIDENCE_MANIFEST_PATH: Final[str] = "{}/{}/{}".format(
    _AGENT_DIRNAME, evidence_collection.VERIFICATION_DIRNAME, evidence_collection.MANIFEST_FILENAME
)

# The dimension whose criteria zero the reward when any of them fails: the transcript parses, the
# agent engaged, every turn completed, the run did not time out.
_GATES_DIMENSION: Final[str] = "gates"
# rewardkit tags each reward it emits with how it was produced: "llm" and "agent" for its two judge
# kinds, "programmatic" for the .py criteria, whose pass/fail guards are gated elsewhere. Both judge
# kinds are collected, so a case that grows an agent judge does not quietly stop being reported.
_JUDGE_REWARD_KINDS: Final[frozenset[str]] = frozenset({"llm", "agent"})

# What an errored evidence entry carrying no id of its own is called in the report. Never empty:
# both readers join these ids and render an empty join as "none", so an empty id would print a trial
# that failed for unmeasured evidence as one with none -- and being falsy is worse still, since a
# lone empty id would let the trial pass.
_UNNAMED_ENTRY_ID: Final[str] = "(unnamed entry)"

# What the driver writes into state.json when the conversation ran to the end.
_FINISHED_TEST_STATE: Final[str] = "finished"


def _load_json_object(path: Path) -> dict[str, Any]:
    """Raises JobReadError if the file is absent, is not decodable, or is not a JSON object."""
    try:
        # Decoded explicitly rather than via read_text() so that a file truncated mid-write, ending
        # on a partial multi-byte sequence, lands in this handler instead of raising a bare
        # UnicodeDecodeError -- which is a ValueError, not an OSError.
        raw_text = path.read_bytes().decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise JobReadError("cannot read {}: {}".format(path, exc)) from exc
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise JobReadError("{} is not valid JSON: {}".format(path, exc)) from exc
    if not isinstance(parsed, dict):
        raise JobReadError("{} is a {}, not a JSON object".format(path, type(parsed).__name__))
    return parsed


def _load_optional_json_object(path: Path) -> dict[str, Any] | None:
    """The file's contents, or None when it was never written. An unreadable file still raises:
    absent and corrupt are different claims and only the first one is expected."""
    if not path.is_file():
        return None
    return _load_json_object(path)


# _reward_dicts, _criteria and is_gates_dimension_passed below are mirrored by _reward_dicts,
# _criteria and _gates_all_passed in templates/tests/verifier/finalize.py, which decides the same gate
# verdict inside the verifier container. They cannot be shared: that file runs on stdlib and
# rewardkit alone, with no imbue package. Keep the two in step. The deliberate difference is
# _criterion_value below, which finalize.py has no counterpart for -- this side must always reach a
# verdict, where finalize.py is entitled to raise on a malformed file.
@pure
def _reward_dicts(dimension: Any) -> list[dict[str, Any]]:
    """The per-reward detail dicts for one dimension of reward-details.json.

    rewardkit emits a single dict when a dimension directory yields one reward and a list when it
    yields several (a judge .toml alongside programmatic .py criteria), so both shapes occur.
    """
    if isinstance(dimension, dict):
        return [dimension]
    if isinstance(dimension, list):
        return [entry for entry in dimension if isinstance(entry, dict)]
    return []


@pure
def _criteria(reward_dict: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_criteria = reward_dict.get("criteria")
    if not isinstance(raw_criteria, list):
        return []
    return [entry for entry in raw_criteria if isinstance(entry, dict)]


@pure
def _criterion_value(criterion: Mapping[str, Any]) -> float:
    """A criterion's normalized 0-1 score, or 0.0 when it carries none this can read.

    reward-details.json is read without a schema, so an unreadable value has to mean something. Zero
    is the safe reading for both callers: it fails a gate, which is the same verdict as a gate that
    was never scored, and it reports a judge criterion at the bottom of its scale rather than hiding
    it. A boolean is excluded before the numeric check because `isinstance(True, int)` holds.
    """
    value = criterion.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


@pure
def is_gates_dimension_passed(reward_details: Mapping[str, Any] | None) -> bool:
    """Whether every structural gate criterion scored above zero.

    A dimension with no criteria at all counts as not passed: it means the verifier never scored the
    gates, which is not the same claim as the gates holding.
    """
    if reward_details is None:
        return False
    reward_dicts = _reward_dicts(reward_details.get(_GATES_DIMENSION))
    is_any_criterion_seen = False
    for reward_dict in reward_dicts:
        for criterion in _criteria(reward_dict):
            is_any_criterion_seen = True
            if _criterion_value(criterion) <= 0.0:
                return False
    return is_any_criterion_seen


def collect_judge_scores(reward_details: Mapping[str, Any] | None) -> tuple[JudgeScore, ...]:
    """Every likert criterion the judges scored, across every dimension. Reported, never gated.

    A criterion whose likert answer cannot be read is the one thing that cannot be reported, since
    there is no number to report; it is dropped and logged. Silence would be worse than the gap: the
    report is the only place a judge score exists, so an empty judge column reads as a case with no
    judges rather than as one whose answers went unread.
    """
    if reward_details is None:
        return ()
    scores: list[JudgeScore] = []
    for dimension_name, dimension in sorted(reward_details.items()):
        for reward_dict in _reward_dicts(dimension):
            if reward_dict.get("kind") not in _JUDGE_REWARD_KINDS:
                continue
            for criterion in _criteria(reward_dict):
                raw_score = criterion.get("raw")
                if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
                    logger.warning(
                        "Dropping the judge criterion {}.{}: its likert answer {!r} is not a number",
                        dimension_name,
                        criterion.get("name"),
                        raw_score,
                    )
                    continue
                scores.append(
                    JudgeScore(
                        dimension=dimension_name,
                        criterion=str(criterion.get("name") or ""),
                        normalized_score=_criterion_value(criterion),
                        raw_score=float(raw_score),
                    )
                )
    return tuple(scores)


def _collect_error_entry_ids(manifest: Mapping[str, Any] | None, manifest_path: Path) -> tuple[str, ...]:
    """The evidence entries the harness could not measure.

    A missing manifest yields nothing rather than an error: a trial that never reached a workspace
    writes none, and its structural gates already say the run went wrong. A manifest that is there
    but carries no readable entries yields nothing too -- this file is read without a schema, because
    a job directory can have been produced by an older driver, and refusing it would make an older
    trial unreadable rather than merely less informative -- but it says so, because the silent
    reading of it is "nothing went unmeasured", which is the one verdict this gate exists to deny.
    """
    if manifest is None:
        return ()
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        logger.warning(
            "{} carries no readable 'entries' list, so nothing can be said about unmeasured evidence",
            manifest_path,
        )
        return ()
    return tuple(
        str(entry.get("entry_id") or _UNNAMED_ENTRY_ID)
        for entry in raw_entries
        if isinstance(entry, dict) and entry.get("status") == CheckStatus.ERROR.value
    )


@pure
def _describe_incompletion(result: TrialResult | None, state: Mapping[str, Any] | None) -> str:
    """Why the trial did not run to the end, or empty when it did.

    Harbor records infrastructure failures as an exception and everything else as a graded trial, so
    a conversation that ran out of time is a normal result here and only the driver's own state file
    tells it apart from a conversation that finished badly.
    """
    if result is None:
        return "harbor wrote no result.json (the trial never finished)"
    if result.exception_info is not None:
        return "{}: {}".format(
            result.exception_info.exception_type, result.exception_info.exception_message.strip()[:200]
        )
    for step_result in result.step_results or ():
        if step_result.exception_info is not None:
            return "step {} raised {}: {}".format(
                step_result.step_name,
                step_result.exception_info.exception_type,
                step_result.exception_info.exception_message.strip()[:200],
            )
    if state is None:
        return "the driver wrote no state.json (the trial never got past setup)"
    if state.get("test_state") != _FINISHED_TEST_STATE:
        return "the conversation ended in state {!r}".format(state.get("test_state"))
    return ""


@pure
def _trial_reward(result: TrialResult | None) -> float | None:
    if result is None or result.verifier_result is None or result.verifier_result.rewards is None:
        return None
    reward = result.verifier_result.rewards.get("reward")
    return None if reward is None else float(reward)


def _load_trial_result(result_path: Path) -> TrialResult | None:
    """Harbor's own record of the trial, or None when it never wrote one.

    Raises JobReadError for a file that is there but is not a TrialResult -- truncated by the crash
    the check is diagnosing, or written by a harbor whose schema moved. That is a job that cannot be
    read, which is a different claim from a run that failed.
    """
    if not result_path.is_file():
        return None
    try:
        return TrialResult.model_validate(_load_json_object(result_path))
    except ValidationError as exc:
        raise JobReadError("{} is not a harbor trial result: {}".format(result_path, exc)) from exc


def read_trial_state(trial_dir: Path) -> dict[str, Any] | None:
    """The driver's own state record for one trial, or None when it never wrote one.

    Read without a schema on purpose: state.json is written by whichever driver version produced the
    trial, and it grows keys over time. Raises JobReadError for a file that is there but unreadable.
    """
    return _load_optional_json_object(trial_dir / _AGENT_STATE_PATH)


def _read_trial_check(trial_dir: Path) -> TrialCheck:
    """Everything the run gate needs from one trial directory, tolerating each artifact's absence."""
    result = _load_trial_result(trial_dir / _TRIAL_RESULT_FILENAME)
    state = read_trial_state(trial_dir)
    reward_details = _load_optional_json_object(trial_dir / _REWARD_DETAILS_PATH)
    manifest_path = trial_dir / _EVIDENCE_MANIFEST_PATH
    manifest = _load_optional_json_object(manifest_path)
    incompletion_reason = _describe_incompletion(result, state)
    state_values = state or {}
    return TrialCheck(
        trial_name=trial_dir.name,
        case_id=str(state_values.get("case_name") or ""),
        is_completed=not incompletion_reason,
        incompletion_reason=incompletion_reason,
        is_gates_passed=is_gates_dimension_passed(reward_details),
        error_entry_ids=_collect_error_entry_ids(manifest, manifest_path),
        reward=_trial_reward(result),
        judge_scores=collect_judge_scores(reward_details),
        modal_environment_name=str(state_values.get("modal_environment_name") or ""),
        mngr_sha=str(state_values.get("mngr_sha") or ""),
        dwt_sha=str(state_values.get("dwt_sha") or ""),
    )


def list_trial_dirs(job_dir: Path) -> list[Path]:
    """Every trial directory in a job directory, in name order.

    Harbor keeps only files at the job root, so a subdirectory is a trial -- including one that never
    wrote a result, which is exactly the case a pass/fail gate must not skip over. The exception is a
    dot-directory: harbor caches a regrade's downloaded source under `<job dir>/.sources/<uuid>`, and
    reading that as a trial would fail a run over an artifact of having regraded it.
    """
    return sorted(entry for entry in job_dir.iterdir() if entry.is_dir() and not entry.name.startswith("."))


def check_job_directory(job_dir: Path) -> RunCheck:
    """Read every trial in a finished job directory and decide whether the run passed.

    Raises JobReadError if the directory holds no trials at all -- an empty job says the run never
    started, which must not be reported as a run that passed.
    """
    if not job_dir.is_dir():
        raise JobReadError("{} is not a job directory".format(job_dir))
    trial_dirs = list_trial_dirs(job_dir)
    if not trial_dirs:
        raise JobReadError("{} holds no trial directories".format(job_dir))
    trials = tuple(_read_trial_check(trial_dir) for trial_dir in trial_dirs)
    return RunCheck(job_name=job_dir.name, trials=trials)


@pure
def _format_judge_scores(judge_scores: Sequence[JudgeScore]) -> str:
    if not judge_scores:
        return "-"
    return ", ".join("{} {:.1f}".format(score.criterion, score.raw_score) for score in judge_scores)


@pure
def _format_marker(is_ok: bool) -> str:
    return "pass" if is_ok else "FAIL"


@pure
def _as_table_cell(text: str) -> str:
    """Free text in a markdown cell. A pipe or a newline in it would end the cell, and every cell
    here carries free text: exception messages carry anything, and case ids, trial names, entry ids
    and judge criterion names are authored strings held to no vocabulary."""
    return text.replace("|", "\\|").replace("\n", " ")


@pure
def render_summary_markdown(run_check: RunCheck) -> str:
    """The run as a GitHub step-summary table: one row per trial, one verdict line above it."""
    header_lines = [
        "## minds-evals: {} -- {}".format(run_check.job_name, _format_marker(run_check.is_passed)),
        "",
        "| trial | case | completed | gates | errored evidence | reward | judge scores | modal env | mngr | dwt |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    trial_lines = [
        "| {} | {} | {} | {} | {} | {} | {} | `{}` | `{}` | `{}` |".format(
            _as_table_cell(trial.trial_name),
            _as_table_cell(trial.case_id) or "-",
            _format_marker(True) if trial.is_completed else _as_table_cell(trial.incompletion_reason),
            _format_marker(trial.is_gates_passed),
            _as_table_cell(", ".join(trial.error_entry_ids)) or "none",
            "-" if trial.reward is None else "{:.4f}".format(trial.reward),
            _as_table_cell(_format_judge_scores(trial.judge_scores)),
            _as_table_cell(trial.modal_environment_name) or "-",
            _as_table_cell(trial.mngr_sha[:12]) or "-",
            _as_table_cell(trial.dwt_sha[:12]) or "-",
        )
        for trial in run_check.trials
    ]
    return "\n".join([*header_lines, *trial_lines, ""])


def write_run_check_reports(run_check: RunCheck, summary_md_path: Path | None, summary_json_path: Path | None) -> None:
    for path, content in (
        (summary_md_path, render_summary_markdown(run_check)),
        (summary_json_path, run_check.model_dump_json(indent=2) + "\n"),
    ):
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        logger.info("Wrote {}", path)
