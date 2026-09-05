"""Generate a harbor task dataset from a minds eval config (adapter pattern): reads the existing
eval-config JSON schema unchanged and emits one harbor task directory per persona case.

Each task directory carries a byte-identical environment/ (the adapted box Dockerfile, entrypoint,
and a staged shallow clone of mngr-internal at the resolved SHA), so Modal's image-layer cache
builds the box image once per mngr SHA. Per-case data lives only in instruction.md, tests/case.json,
and solution/solve.sh -- never in environment/, or the cache key diverges.
"""

import json
import re
import shutil
import string
import subprocess
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from typing import Final
from typing import assert_never

import click
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import trajectory
from imbue.minds_evals.data_types import BOX_STEP_FILES_DIR
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CaseStep
from imbue.minds_evals.data_types import ComposedRewardFloor
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DEFAULT_AVG_WORD_COUNT_BASELINE
from imbue.minds_evals.data_types import DEFAULT_DWT_BRANCH
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.data_types import DEFAULT_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import DEFAULT_VERIFICATION_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import EvalConfig
from imbue.minds_evals.data_types import Expectations
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import PerDimensionRewardFloors
from imbue.minds_evals.data_types import PersonaCase
from imbue.minds_evals.data_types import PromptEntry
from imbue.minds_evals.data_types import REWARD_STRATEGY_KEY
from imbue.minds_evals.data_types import RewardDimension
from imbue.minds_evals.data_types import RewardFloor
from imbue.minds_evals.data_types import RewardStrategy
from imbue.minds_evals.data_types import STEP_NAME_PATTERN
from imbue.minds_evals.data_types import StepBoxFile
from imbue.minds_evals.data_types import StepFile
from imbue.minds_evals.data_types import StepMinReward
from imbue.minds_evals.data_types import StepPosition
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.data_types import UPLOAD_ID_PATTERN
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.data_types import WORKSPACE_UPLOADS_DIR
from imbue.minds_evals.data_types import entry_exchange_budget
from imbue.minds_evals.errors import EvalConfigError
from imbue.minds_evals.errors import GitSourceError
from imbue.minds_evals.expectations import expand_expectations
from imbue.minds_evals.expectations import parse_expectations
from imbue.minds_evals.usage import summarize_workspace_usage

MNGR_REPO: Final[str] = "https://github.com/imbue-ai/mngr-internal.git"

_TEMPLATES = resources.files("imbue.minds_evals") / "templates"

# Canned oracle replies: short, plain-language, and self-directed, so the LLM
# judges score them near the top of every dimension.
_ORACLE_OPENING_REPLY: Final[str] = (
    "On it. I'll set everything up and let you know the moment it's ready for you to try."
)
_ORACLE_MIDDLE_REPLY: Final[str] = (
    "It's ready -- open the preview to try it out. I'll keep polishing while you take a look."
)
_ORACLE_FINAL_REPLY: Final[str] = (
    "All done. Everything you asked for is in place and working. Tell me if you'd like anything adjusted."
)
_ORACLE_DECIDE_MESSAGE: Final[str] = "Sounds good."
# How the oracle's trajectory names its author, and the fixed timestamp that keeps solve.sh byte-stable
# across generations (the state it writes is pinned to the epoch the same way).
_ORACLE_DRIVER_NAME: Final[str] = "minds-evals-oracle"
_ORACLE_DRIVER_VERSION: Final[str] = "0.1.0"
_ORACLE_TIMESTAMP: Final[str] = "1970-01-01T00:00:00+00:00"

# The driver's own deadline is the case's timeout_seconds; harbor's agent
# timeout gets this much grace on top so that after the driver hits its deadline
# and returns, its finally-block cleanup (a final snapshot pull plus the
# workspace destroy sweep and its retry) still runs before harbor cancels
# run(). The nested sandboxes' own timeout is the backstop if cleanup is cut off.
AGENT_TIMEOUT_GRACE_SECONDS: Final[float] = 300.0

# What the outcome judge's rewardkit weight must be for it to carry half the outcome dimension.
# rewardkit aggregates a dimension in two levels: all of a directory's .py criteria are averaged
# into ONE programmatic reward of weight 1.0, and each judge toml is a second reward carrying its
# own weight -- so an even split is weight 1.0 regardless of how many programmatic criteria a case
# declares. (Contrast the quality dimension's weight of 3.0, which buys equal weight PER CRITERION
# across its three judge criteria and one programmatic guard.)
OUTCOME_JUDGE_WEIGHT: Final[float] = 1.0

# What the outcome judge reads: the case's ground truth (rendered at grade time), the evidence index,
# the rendered conversation -- which is there so a deliverable the client visibly steered away from
# the scripted expectations is graded against the evolved ask -- and the flattened UI-flow evidence.
#
# The last three entries are produced by grade-time pre-steps and always exist, empty or not. That is
# deliberate: rewardkit renders a listed path it cannot find as a literal "[not found]" block, so a
# conditional artifact would put noise in the prompt of every flow-less trial (every oracle run, and
# every case that declares no flows). An empty listed DIRECTORY, by contrast, renders nothing at all
# -- which is why the digest states the screenshot count rather than leaving the judge to infer it.
OUTCOME_JUDGE_FILES: Final[tuple[str, ...]] = (
    "/logs/agent/expectations.md",
    "/logs/agent/{}/{}".format(evidence_collection.VERIFICATION_DIRNAME, evidence_collection.MANIFEST_FILENAME),
    "/logs/agent/judge_transcript.txt",
    "/logs/agent/judge_flows_digest.txt",
    "/logs/agent/judge_screenshots",
)


@pure
def derive_case_id(raw_case: Mapping[str, Any], index: int) -> str:
    """A case's stable id: its explicit 'id', else a positional 'case-N' (same derivation the old
    harness used, so ids stay comparable across harnesses)."""
    return str(raw_case.get("id") or "case-{}".format(index + 1))


@pure
def _describe_validation_error(exc: ValidationError) -> str:
    """A pydantic error report flattened to one line of `field: message` pairs, for a config author
    who is reading a CLI error rather than a stack trace."""
    return "; ".join(
        "{}: {}".format(".".join(str(part) for part in error["loc"]) or "entry", error["msg"])
        for error in exc.errors()
    )


@pure
def parse_goal_entry(raw_entry: Mapping[str, Any], case_id: str, index: int) -> GoalEntry:
    """One `{goal, max_exchanges}` prompts entry, as the model that the driver re-validates at trial
    time defines it: unknown keys refused, the budget defaulted and bounded.

    Only the surrounding context is added here -- which case and which prompt -- so a config author
    is told where the bad entry is. The budget bound matters at generation time because a budget is
    a cost commitment: each exchange is a full agent turn in a real workspace, so an implausible
    budget must fail generation rather than surface as a trial that runs for hours.
    """
    raw_goal = raw_entry.get("goal")
    # Type-checked rather than coerced with str(): a JSON number, bool, list, or object would become
    # the literal text the client is told to hold out for ("123", "['a', 'b']"), so the authoring
    # mistake would steer a real conversation instead of failing generation.
    if not isinstance(raw_goal, str) or not raw_goal.strip():
        raise EvalConfigError(
            "case {!r} prompt {}: a goal entry needs a non-empty 'goal' string".format(case_id, index + 1)
        )
    try:
        return GoalEntry.model_validate({**dict(raw_entry), "goal": raw_goal.strip()})
    except ValidationError as exc:
        raise EvalConfigError(
            "case {!r} prompt {}: {}".format(case_id, index + 1, _describe_validation_error(exc))
        ) from exc


@pure
def _normalize_prompts(raw_prompts: object, case_id: str, is_opening_ask_required: bool) -> tuple[PromptEntry, ...]:
    if not isinstance(raw_prompts, list) or not raw_prompts:
        raise EvalConfigError("case {!r} must have a non-empty 'prompts' list".format(case_id))
    prompts: list[PromptEntry] = []
    for index, raw_prompt in enumerate(raw_prompts):
        if isinstance(raw_prompt, dict):
            prompts.append(parse_goal_entry({str(key): value for key, value in raw_prompt.items()}, case_id, index))
        elif isinstance(raw_prompt, str):
            text = raw_prompt.strip()
            if not text:
                raise EvalConfigError("case {!r} prompt {}: a prompt must not be empty".format(case_id, index + 1))
            prompts.append(text)
        else:
            # Not coerced with str(): a JSON null or number would become the literal client message
            # "None" or "123" and be sent to a real agent, so the authoring mistake would surface as
            # a wasted trial rather than as a failed generation.
            raise EvalConfigError(
                "case {!r} prompt {}: a prompt must be a message string or a goal object, not {}".format(
                    case_id, index + 1, type(raw_prompt).__name__
                )
            )
    if not is_opening_ask_required:
        return tuple(prompts)
    # The opening ask is what commissions the work, and it is the one entry every reader of the
    # dataset (and the oracle) can take verbatim. Both non-literal forms are refused here rather
    # than only the sentinel: a goal entry could state its own opening ask, but allowing it would
    # make the first message of a case non-deterministic. Only the case's FIRST list is held to
    # this: a later step opens mid-conversation, where there is a transcript to decide from.
    first = prompts[0]
    if isinstance(first, GoalEntry):
        raise EvalConfigError(
            "case {!r}: the first prompt must be a literal message, not a goal entry".format(case_id)
        )
    if first == DECIDE_SENTINEL:
        raise EvalConfigError(
            "case {!r}: the first prompt cannot be {} (nothing to decide from yet)".format(case_id, DECIDE_SENTINEL)
        )
    return tuple(prompts)


_STEP_KEYS: Final[frozenset[str]] = frozenset({"name", "prompts", "files", "expectations", "min_reward"})
_STEP_FILE_KEYS: Final[frozenset[str]] = frozenset({"source", "upload_id"})
_STEP_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(STEP_NAME_PATTERN)
_UPLOAD_ID_PATTERN: Final[re.Pattern[str]] = re.compile(UPLOAD_ID_PATTERN)
_REWARD_DIMENSIONS: Final[tuple[str, ...]] = tuple(dimension.value for dimension in RewardDimension)


@pure
def parse_step_file(raw_entry: object, case_id: str, step_name: str, index: int) -> StepFile:
    """One `{source, upload_id}` entry of a step's `files`, as the eval author wrote it."""
    what = "files[{}]".format(index)
    if not isinstance(raw_entry, dict):
        raise EvalConfigError("case {!r} step {!r}: {} must be an object".format(case_id, step_name, what))
    raw_file: dict[str, Any] = {str(key): value for key, value in raw_entry.items()}
    unknown_keys = sorted(set(raw_file) - _STEP_FILE_KEYS)
    if unknown_keys:
        raise EvalConfigError(
            "case {!r} step {!r}: {} has unknown key(s): {}".format(case_id, step_name, what, ", ".join(unknown_keys))
        )
    source = str(raw_file.get("source") or "").strip()
    if not source:
        raise EvalConfigError("case {!r} step {!r}: {} needs a 'source'".format(case_id, step_name, what))
    source_parts = PurePosixPath(source)
    if source_parts.is_absolute() or ".." in source_parts.parts:
        # The source is copied into every task directory that uses it, so it has to be reachable
        # from the eval config wherever the config is checked out.
        raise EvalConfigError(
            "case {!r} step {!r}: {}.source {!r} must be a relative path inside the eval config's "
            "own directory".format(case_id, step_name, what, source)
        )
    upload_id = str(raw_file.get("upload_id") or "").strip()
    if not _UPLOAD_ID_PATTERN.match(upload_id):
        raise EvalConfigError(
            "case {!r} step {!r}: {}.upload_id {!r} must match {} (it names a directory in the "
            "workspace and in the box)".format(case_id, step_name, what, upload_id, UPLOAD_ID_PATTERN)
        )
    return StepFile(source=source, upload_id=upload_id)


@pure
def parse_step_min_reward(raw_value: object, case_id: str, step_name: str) -> StepMinReward:
    """One step's `min_reward`, in either of the two forms harbor reads it in."""
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        return ComposedRewardFloor(floor=float(raw_value))
    if not isinstance(raw_value, dict):
        raise EvalConfigError(
            "case {!r} step {!r}: 'min_reward' must be a number or an object keyed by reward "
            "dimension, got {!r}".format(case_id, step_name, raw_value)
        )
    floors: list[RewardFloor] = []
    for raw_key, raw_floor in raw_value.items():
        key = str(raw_key)
        if key not in _REWARD_DIMENSIONS:
            raise EvalConfigError(
                "case {!r} step {!r}: 'min_reward' key {!r} is not a reward dimension; expected one of {}".format(
                    case_id, step_name, key, ", ".join(_REWARD_DIMENSIONS)
                )
            )
        if not isinstance(raw_floor, (int, float)) or isinstance(raw_floor, bool):
            raise EvalConfigError(
                "case {!r} step {!r}: 'min_reward.{}' must be a number, got {!r}".format(
                    case_id, step_name, key, raw_floor
                )
            )
        floors.append(RewardFloor(dimension=RewardDimension(key), floor=float(raw_floor)))
    if not floors:
        raise EvalConfigError(
            "case {!r} step {!r}: 'min_reward' is empty, which gates nothing -- leave it out instead".format(
                case_id, step_name
            )
        )
    return PerDimensionRewardFloors(floors=tuple(floors))


@pure
def _reject_ungradeable_reward_floors(
    min_reward: StepMinReward | None, expectations: Expectations | None, case_id: str, step_name: str
) -> None:
    """Refuse a floor on a dimension this step's own verifier will not emit.

    Three of the four dimensions are unconditional: `gates` and `quality` ship in every verifier
    build context and `reward` is what finalize.py composes. `outcome` is the exception -- the
    criteria directory is written only for a step that declares expectations, so that rewardkit
    does not score a step with nothing to score. Harbor reads a threshold on a key the verifier
    never wrote as -inf, so such a floor fails on every run and aborts the trial there regardless
    of what the agent did.
    """
    if expectations is not None or not isinstance(min_reward, PerDimensionRewardFloors):
        return
    if not any(floor.dimension is RewardDimension.OUTCOME for floor in min_reward.floors):
        return
    raise EvalConfigError(
        "case {!r} step {!r}: 'min_reward' gates {!r}, but the step declares no 'expectations', so "
        "its verifier emits no outcome score and the threshold would fail on every run".format(
            case_id, step_name, RewardDimension.OUTCOME.value
        )
    )


@pure
def _parse_step(raw_entry: object, case_id: str, index: int, is_last: bool) -> CaseStep:
    """One entry of a case's `steps`, validated on its own.

    A `min_reward` on the LAST step is rejected: harbor's threshold only ever aborts the steps that
    come after, so one there would be graded and then ignored -- which is why the position is passed
    in rather than being a property of the step.
    """
    if not isinstance(raw_entry, dict):
        raise EvalConfigError("case {!r} step {}: each step must be an object".format(case_id, index + 1))
    raw_step: dict[str, Any] = {str(key): value for key, value in raw_entry.items()}
    unknown_keys = sorted(set(raw_step) - _STEP_KEYS)
    if unknown_keys:
        raise EvalConfigError(
            "case {!r} step {}: unknown key(s): {}".format(case_id, index + 1, ", ".join(unknown_keys))
        )
    name = str(raw_step.get("name") or "").strip()
    if not _STEP_NAME_PATTERN.match(name):
        raise EvalConfigError(
            "case {!r} step {}: name {!r} must match {} (it names a directory and a container session)".format(
                case_id, index + 1, name, STEP_NAME_PATTERN
            )
        )
    raw_min_reward = raw_step.get("min_reward")
    if raw_min_reward is not None and is_last:
        raise EvalConfigError(
            "case {!r} step {!r}: the last step cannot declare a 'min_reward' -- there are no "
            "later steps for it to abort, so harbor would grade it and then ignore it".format(case_id, name)
        )
    # Absent is the only shape that means "no files": `or []` would let every falsy non-list --
    # `"files": {}` in particular -- through as one, and the step would generate cleanly with its
    # prompts quoting an upload path nothing ever staged.
    raw_files = raw_step.get("files")
    raw_files = [] if raw_files is None else raw_files
    if not isinstance(raw_files, list):
        raise EvalConfigError("case {!r} step {!r}: 'files' must be a list".format(case_id, name))
    raw_expectations = raw_step.get("expectations")
    # Named for the step, not the case: a stepped case's prompt errors are counted within one step,
    # so a message saying only the case leaves the author looking through all of them.
    step_label = "{} step {}".format(case_id, name)
    expectations = parse_expectations(raw_expectations, step_label) if raw_expectations is not None else None
    min_reward = parse_step_min_reward(raw_min_reward, case_id, name) if raw_min_reward is not None else None
    _reject_ungradeable_reward_floors(min_reward, expectations, case_id, name)
    return CaseStep(
        name=name,
        prompts=_normalize_prompts(raw_step.get("prompts"), step_label, is_opening_ask_required=index == 0),
        files=tuple(
            parse_step_file(raw_file, case_id, name, file_index) for file_index, raw_file in enumerate(raw_files)
        ),
        expectations=expectations,
        min_reward=min_reward,
    )


@pure
def _normalize_steps(raw_steps: object, case_id: str) -> tuple[CaseStep, ...]:
    """The case's `steps` list: every step parsed, then the rules only the whole list can check."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise EvalConfigError("case {!r} must have a non-empty 'steps' list".format(case_id))
    steps = [
        _parse_step(raw_entry, case_id, index, is_last=index == len(raw_steps) - 1)
        for index, raw_entry in enumerate(raw_steps)
    ]
    step_names = [step.name for step in steps]
    duplicate_names = sorted({name for name in step_names if step_names.count(name) > 1})
    if duplicate_names:
        raise EvalConfigError("case {!r}: duplicate step name(s): {}".format(case_id, ", ".join(duplicate_names)))
    upload_ids = [step_file.upload_id for step in steps for step_file in step.files]
    duplicate_ids = sorted({upload_id for upload_id in upload_ids if upload_ids.count(upload_id) > 1})
    if duplicate_ids:
        # Each id names one directory under the workspace's data/uploads/, so a repeat would have a
        # later step overwrite an upload the client is still referring to by path.
        raise EvalConfigError("case {!r}: duplicate upload_id(s): {}".format(case_id, ", ".join(duplicate_ids)))
    return tuple(steps)


@pure
def _parse_reward_strategy(raw_value: object, case_id: str, is_stepped: bool) -> RewardStrategy:
    """How a stepped case's per-step rewards become the trial's, defaulting to the final step's."""
    if raw_value is None:
        return RewardStrategy.FINAL
    if not is_stepped:
        raise EvalConfigError(
            "case {!r} declares {!r} but no 'steps'; a flat case has one reward and nothing to aggregate".format(
                case_id, REWARD_STRATEGY_KEY
            )
        )
    try:
        return RewardStrategy(str(raw_value).strip().lower())
    except ValueError:
        raise EvalConfigError(
            "case {!r}: unknown {} {!r}; expected one of {}".format(
                case_id, REWARD_STRATEGY_KEY, raw_value, ", ".join(strategy.value for strategy in RewardStrategy)
            )
        ) from None


@pure
def worst_case_exchange_count(prompts: Sequence[PromptEntry]) -> int:
    """The most client messages a case can send: every goal entry spending its whole budget."""
    return sum(entry_exchange_budget(entry) for entry in prompts)


@pure
def _normalize_cases(personas: object) -> tuple[PersonaCase, ...]:
    if not isinstance(personas, list) or not personas:
        raise EvalConfigError("'personas' must be a non-empty list")
    cases: list[PersonaCase] = []
    for index, raw_entry in enumerate(personas):
        if not isinstance(raw_entry, dict):
            raise EvalConfigError("each persona case must be an object")
        raw_case: dict[str, Any] = {str(key): value for key, value in raw_entry.items()}
        case_id = derive_case_id(raw_case, index)
        raw_steps = raw_case.get("steps")
        # A case says its turns one way or the other. Accepting both would leave two answers to
        # "what does this case say next", and nothing to decide between them.
        if raw_steps is not None and raw_case.get("prompts") is not None:
            raise EvalConfigError("case {!r} declares both 'prompts' and 'steps'; pick one".format(case_id))
        steps = _normalize_steps(raw_steps, case_id) if raw_steps is not None else None
        prompts = (
            tuple(entry for step in steps for entry in step.prompts)
            if steps is not None
            else _normalize_prompts(raw_case.get("prompts"), case_id, is_opening_ask_required=True)
        )
        raw_expectations = raw_case.get("expectations")
        if steps is not None and raw_expectations is not None:
            # Every step states its own, so that a reader of a step's instruction sees exactly what
            # that step is graded on rather than one block that applies to none of them in full.
            raise EvalConfigError(
                "case {!r} declares 'steps' and a case-level 'expectations'; a stepped case states "
                "its expectations per step".format(case_id)
            )
        cases.append(
            PersonaCase(
                case_id=case_id,
                persona=str(raw_case.get("persona", "")).strip(),
                prompts=prompts,
                steps=steps,
                expectations=parse_expectations(raw_expectations, case_id) if raw_expectations is not None else None,
                reward_strategy=_parse_reward_strategy(
                    raw_case.get(REWARD_STRATEGY_KEY), case_id, is_stepped=steps is not None
                ),
            )
        )
    case_ids = [case.case_id for case in cases]
    duplicate_ids = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicate_ids:
        # Two cases with the same id collide on one task directory, so reject up front.
        raise EvalConfigError("duplicate case id(s): {}".format(", ".join(duplicate_ids)))
    return tuple(cases)


@pure
def step_file_source_path(config_dir: Path, step_file: StepFile) -> Path:
    """Where one step's upload lives on disk: its `source`, taken relative to the eval config file."""
    return config_dir / step_file.source


def _validate_step_file_sources(config: EvalConfig, config_dir: Path) -> None:
    """Every declared upload must exist beside the eval config.

    Checked at load time rather than when the copy is attempted, so a mistyped path fails before any
    remote is resolved or any task directory is written.
    """
    for case in config.cases:
        for step in case.steps or ():
            for step_file in step.files:
                source = step_file_source_path(config_dir, step_file)
                if not source.exists():
                    raise EvalConfigError(
                        "case {!r} step {!r}: upload {!r} has no source at {}".format(
                            case.case_id, step.name, step_file.upload_id, source
                        )
                    )


def load_eval_config(config_path: Path) -> EvalConfig:
    """Read and validate an eval config json file (the old harness's schema, unchanged)."""
    if not config_path.is_file():
        raise EvalConfigError("no such config file: {}".format(config_path))
    try:
        raw_config = json.loads(config_path.read_text())
    except ValueError as exc:
        raise EvalConfigError("config {} is not valid JSON: {}".format(config_path, exc)) from exc
    if not isinstance(raw_config, dict):
        raise EvalConfigError("config {} must be a JSON object".format(config_path))
    if not raw_config.get("mngr_branch"):
        raise EvalConfigError("eval config is missing required key: 'mngr_branch'")
    config = EvalConfig(
        mngr_branch=str(raw_config["mngr_branch"]),
        dwt_repo=str(raw_config.get("dwt_repo") or DEFAULT_DWT_REPO),
        dwt_branch=str(raw_config.get("dwt_branch") or DEFAULT_DWT_BRANCH),
        timeout_seconds=float(raw_config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        verification_timeout_seconds=float(
            raw_config.get("verification_timeout_seconds") or DEFAULT_VERIFICATION_TIMEOUT_SECONDS
        ),
        avg_word_count_baseline=float(raw_config.get("avg_word_count_baseline") or DEFAULT_AVG_WORD_COUNT_BASELINE),
        cases=_normalize_cases(raw_config.get("personas")),
    )
    _validate_step_file_sources(config, config_path.parent)
    return config


def resolve_remote_tip(repo: str, branch: str) -> str:
    """The branch's current tip SHA on the remote, via plain `git ls-remote` -- git uses your own
    credentials, and a real auth/network failure surfaces as-is. Every pinned input goes through
    here (mngr and the workspace template alike), so the messages name the repo they are about."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo, "refs/heads/{}".format(branch)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise GitSourceError("timed out reaching the remote {} -- check your network/VPN".format(repo)) from None
    if result.returncode != 0:
        # A failed ls-remote (offline, auth, DNS) is NOT a missing branch -- surface the real reason.
        detail = (result.stderr or "").strip() or "git ls-remote failed"
        raise GitSourceError(
            "could not reach the remote {} -- check your network + git auth ({})".format(repo, detail[:200])
        )
    ref = (result.stdout or "").split("\t")[0].strip()
    if not ref:
        raise GitSourceError("branch {!r} not found on the remote {}".format(branch, repo))
    return ref


def _run_git(*args: str) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise GitSourceError("`git {}` failed: {}".format(" ".join(args[:2]), (result.stderr or "").strip()[:300]))


def fetch_mngr_source(repo: str, ref: str, dest: Path) -> None:
    """A FRESH shallow clone of the exact mngr ref into `dest`, via plain git (your own credentials).
    Pulled straight from the remote into a throwaway dir -- independent of any on-device checkout, so
    your local working-tree state never reaches the box."""
    _run_git("init", "-q", str(dest))
    _run_git("-C", str(dest), "fetch", "--depth", "1", repo, ref)
    _run_git("-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


@pure
def build_case_config(config: EvalConfig, case: PersonaCase, mngr_sha: str, dwt_sha: str) -> CaseConfig:
    # The deliverable kind is expanded into its explicit check list exactly once, here, so the
    # collector and the verifier can never disagree about what was being checked.
    return CaseConfig(
        case_id=case.case_id,
        persona=case.persona,
        prompts=case.prompts,
        step=None,
        timeout_seconds=config.timeout_seconds,
        verification_timeout_seconds=config.verification_timeout_seconds,
        mngr_branch=config.mngr_branch,
        mngr_sha=mngr_sha,
        dwt_repo=config.dwt_repo,
        dwt_branch=config.dwt_branch,
        dwt_sha=dwt_sha,
        avg_word_count_baseline=config.avg_word_count_baseline,
        expectations=expand_expectations(case.expectations) if case.expectations is not None else None,
        authored_expectations=case.expectations,
    )


@pure
def _substitute_template(template_text: str, values: dict[str, str]) -> str:
    return string.Template(template_text).substitute(values)


@pure
def step_conversation_timeout_seconds(case_config: CaseConfig, step: CaseStep) -> float:
    """One step's share of the case's conversation budget.

    Split by worst-case exchange count rather than evenly, because a step is as expensive as the
    client messages it can send. Harbor applies the task's agent timeout to EVERY step unless a step
    overrides it, so without a split a two-step case could run for twice its declared budget.
    """
    case_total = worst_case_exchange_count(case_config.prompts)
    if case_total == 0:
        return case_config.timeout_seconds
    return case_config.timeout_seconds * worst_case_exchange_count(step.prompts) / case_total


@pure
def step_agent_timeout_seconds(case_config: CaseConfig, step: CaseStep) -> float:
    """What harbor gives one step's run() call: its conversation share, the evidence-collection
    budget every step spends at its end, and the grace the driver's cleanup needs."""
    return (
        step_conversation_timeout_seconds(case_config, step)
        + case_config.verification_timeout_seconds
        + AGENT_TIMEOUT_GRACE_SECONDS
    )


# What every verifier container gets, task-level and per step alike. Restated on each step so that
# the figure a reader of a step sees is the one that step gets, rather than one inherited from a
# [verifier] block that also configures the task-level verifier a stepped task never runs.
VERIFIER_TIMEOUT_SECONDS: Final[float] = 600.0


@pure
def trial_lifetime_seconds(timeout_seconds: float, verification_timeout_seconds: float, step_count: int) -> float:
    """How long something started on the first step has to live to still serve the last one.

    Not the sum of the steps' conversation shares, which is just the case's `timeout_seconds`:
    between two conversations the trial also spends a step's evidence phase, the grace its cleanup
    needs, and its verifier container. A resource sized against the conversation total alone -- the
    reverse tunnel the workspace reaches the LLM proxy on -- is torn down under a later step.

    Every step is counted, including the last, whose verification the tunnel does not have to
    outlive. The number bounds a resource nothing else will reclaim if the driver dies, so it is
    deliberately generous rather than exact.

    Takes the two budgets rather than a CaseConfig so that it can also be answered from the eval
    config alone, before any remote is resolved -- which is where a case too long for the
    infrastructure has to be reported.
    """
    return timeout_seconds + step_count * (
        verification_timeout_seconds + AGENT_TIMEOUT_GRACE_SECONDS + VERIFIER_TIMEOUT_SECONDS
    )


@pure
def render_min_reward_toml(min_reward: StepMinReward) -> str:
    """One step's reward floor, in whichever of harbor's two forms its author wrote."""
    match min_reward:
        case ComposedRewardFloor():
            return "min_reward = {}".format(min_reward.floor)
        case PerDimensionRewardFloors():
            return "min_reward = {{ {} }}".format(
                ", ".join("{} = {}".format(floor.dimension.value, floor.floor) for floor in min_reward.floors)
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def render_steps_toml(case_config: CaseConfig, steps: Sequence[CaseStep]) -> str:
    """The `[[steps]]` array a stepped case's task.toml carries."""
    blocks: list[str] = []
    for step in steps:
        lines = ["[[steps]]", 'name = "{}"'.format(step.name)]
        if step.min_reward is not None:
            lines.append(render_min_reward_toml(step.min_reward))
        lines += [
            "",
            "[steps.agent]",
            "timeout_sec = {}".format(step_agent_timeout_seconds(case_config, step)),
            "",
            "[steps.verifier]",
            "timeout_sec = {}".format(VERIFIER_TIMEOUT_SECONDS),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


@pure
def render_task_toml(
    template_text: str,
    case_config: CaseConfig,
    steps: Sequence[CaseStep] | None,
    reward_strategy: RewardStrategy,
) -> str:
    return _substitute_template(
        template_text,
        {
            "case_id": case_config.case_id,
            "mngr_branch": case_config.mngr_branch,
            "mngr_sha": case_config.mngr_sha,
            "dwt_repo": case_config.dwt_repo,
            "dwt_branch": case_config.dwt_branch,
            "dwt_sha": case_config.dwt_sha,
            "agent_timeout_sec": str(
                case_config.timeout_seconds + case_config.verification_timeout_seconds + AGENT_TIMEOUT_GRACE_SECONDS
            ),
            "verifier_timeout_sec": str(VERIFIER_TIMEOUT_SECONDS),
            "reward_strategy_toml": (
                'multi_step_reward_strategy = "{}"\n'.format(reward_strategy.value) if steps is not None else ""
            ),
            "steps_toml": render_steps_toml(case_config, steps) if steps is not None else "",
        },
    )


# The directory a step's uploads travel in, both inside the task's workdir/ and, briefly, inside the
# box's working directory. The step setup script moves it by this name, and is given the name rather
# than repeating it, since a script that moves a directory nothing staged fails inside the box.
STEP_FILES_DIRNAME: Final[str] = "step_files"


@pure
def step_files_box_dir(step_name: str) -> str:
    """Where the box holds one step's uploads once its setup script has relocated them."""
    return "{}/{}".format(BOX_STEP_FILES_DIR, step_name)


@pure
def step_box_files(step: CaseStep) -> tuple[StepBoxFile, ...]:
    """Where each of a step's uploads waits in the box, for the driver to copy into the workspace."""
    return tuple(
        StepBoxFile(
            upload_id=step_file.upload_id,
            box_path="{}/{}".format(step_files_box_dir(step.name), step_file.upload_id),
        )
        for step_file in step.files
    )


@pure
def build_step_case_config(case_config: CaseConfig, steps: Sequence[CaseStep], index: int) -> CaseConfig:
    """The case config one step's instruction and verifier carry: that step's turns, that step's
    expectations, its own share of the conversation budget, and where it sits in the task.

    The persona and the pins are the case's, unchanged, because a step is a stretch of one client's
    conversation rather than a case of its own.
    """
    step = steps[index]
    return case_config.model_copy_update(
        to_update(case_config.field_ref().prompts, step.prompts),
        to_update(case_config.field_ref().timeout_seconds, step_conversation_timeout_seconds(case_config, step)),
        to_update(
            case_config.field_ref().expectations,
            expand_expectations(step.expectations) if step.expectations is not None else None,
        ),
        to_update(case_config.field_ref().authored_expectations, step.expectations),
        to_update(
            case_config.field_ref().step,
            StepPosition(
                name=step.name,
                index=index,
                total=len(steps),
                trial_lifetime_seconds=trial_lifetime_seconds(
                    case_config.timeout_seconds, case_config.verification_timeout_seconds, len(steps)
                ),
                entries_before=sum(len(earlier.prompts) for earlier in steps[:index]),
                files=step_box_files(step),
            ),
        ),
    )


@pure
def build_step_oracle_case_config(case_config: CaseConfig, steps: Sequence[CaseStep], index: int) -> CaseConfig:
    """The config one step's oracle script is rendered from: the conversation up to and including
    this step, graded against this step's expectations.

    The oracle fabricates the record a real run would have left at the END of this step, and that
    record is cumulative -- the structural gates hold a step answerable for every entry the trial has
    configured so far, not only its own -- so a task-level oracle replaying the whole case into every
    step would fail each earlier step's turn gate.
    """
    step_config = build_step_case_config(case_config, steps, index)
    return step_config.model_copy_update(
        to_update(
            step_config.field_ref().prompts,
            tuple(entry for earlier in steps[: index + 1] for entry in earlier.prompts),
        )
    )


@pure
def render_prompt_entry_prose(entry: PromptEntry) -> str:
    """One prompts entry as a human reads it in instruction.md.

    A goal entry is rendered as what it is -- a stretch of conversation with a budget -- rather than
    as a message, so a reader never mistakes the goal text for something sent verbatim.
    """
    if isinstance(entry, GoalEntry):
        return "(goal, up to {} exchange(s)) the client keeps the conversation going until it is satisfied that: {}".format(
            entry.max_exchanges, entry.goal
        )
    return "`{}`".format(entry) if entry == DECIDE_SENTINEL else entry


@pure
def _prompts_prose(case_config: CaseConfig) -> str:
    return "\n".join(
        "{}. {}".format(index + 1, render_prompt_entry_prose(entry)) for index, entry in enumerate(case_config.prompts)
    )


@pure
def render_instruction(template_text: str, case_config: CaseConfig) -> str:
    return _substitute_template(
        template_text,
        {
            "case_id": case_config.case_id,
            "persona_prose": case_config.persona or "(none)",
            "prompts_prose": _prompts_prose(case_config),
            "case_config_json": json.dumps(case_config.model_dump(), indent=2),
        },
    )


@pure
def _step_files_prose(step: StepPosition) -> str:
    """What a reader of a step's instruction is told about the uploads it introduces."""
    if not step.files:
        return "This step introduces no files into the workspace."
    placements = "\n".join(
        "- `{}` -> `{}/{}`".format(step_file.box_path, WORKSPACE_UPLOADS_DIR, step_file.upload_id)
        for step_file in step.files
    )
    return (
        "Files the driver copies from the box into the running workspace before this step's first "
        "message, so the client can refer to them by the path they appear at:\n{}".format(placements)
    )


@pure
def _step_expectations_prose(step_case_config: CaseConfig) -> str:
    """What a reader of a step's instruction is told about how the step is graded."""
    expectations = step_case_config.authored_expectations
    if expectations is None:
        return (
            "This step declares no expectations, so it is graded on the structural gates and the conversation alone."
        )
    if expectations.deliverable is None:
        return (
            "This step commissions no deliverable, so nothing is probed or bundled; it is judged "
            "from the conversation, the always-on workspace capture and any UI flows it declares, "
            "against: {}".format(expectations.outcome)
        )
    return "This step is graded against: {}".format(expectations.outcome)


@pure
def render_step_instruction(template_text: str, step_case_config: CaseConfig) -> str:
    """One step's instruction.md. A multi-step task has no top-level instruction.md, so this is the
    only place that step's case config can ride."""
    step = step_case_config.step
    assert step is not None, "a step instruction needs a step position"
    return _substitute_template(
        template_text,
        {
            "case_id": step_case_config.case_id,
            "step_name": step.name,
            "step_number": str(step.index + 1),
            "step_total": str(step.total),
            "persona_prose": step_case_config.persona or "(none)",
            "files_prose": _step_files_prose(step),
            "prompts_prose": _prompts_prose(step_case_config),
            "expectations_prose": _step_expectations_prose(step_case_config),
            "case_config_json": json.dumps(step_case_config.model_dump(), indent=2),
        },
    )


@pure
def render_step_setup_script(template_text: str, step_name: str) -> str:
    """The script harbor runs in the box before a step's agent, to relocate that step's uploads."""
    destination = step_files_box_dir(step_name)
    return _substitute_template(
        template_text,
        {
            "destination": destination,
            "destination_parent": str(PurePosixPath(destination).parent),
            # The name write_step_workdir staged the uploads under, so the two cannot drift into a
            # script that moves a directory nothing wrote.
            "staged_dirname": STEP_FILES_DIRNAME,
        },
    )


@pure
def _oracle_user_message(entry: PromptEntry) -> str:
    """What the oracle sends for one entry.

    A goal entry becomes ONE literal message stating the goal: the oracle fabricates a plausible
    max-reward transcript and does not simulate a client's persistence, so an entry whose real
    client would have pushed several times contributes a single turn here.
    """
    if isinstance(entry, GoalEntry):
        return entry.goal
    return _ORACLE_DECIDE_MESSAGE if entry == DECIDE_SENTINEL else entry


@pure
def _oracle_entry_kind(entry: PromptEntry) -> TurnEntryKind:
    if isinstance(entry, GoalEntry):
        return TurnEntryKind.GOAL
    return TurnEntryKind.PERSONA if entry == DECIDE_SENTINEL else TurnEntryKind.LITERAL


@pure
def oracle_entry_records(case_config: CaseConfig) -> list[dict[str, Any]]:
    """The per-entry outcomes the oracle's state.json carries, one exchange each, so the structural
    gates see a conversation that reconciles with its message count.

    Every key an `EntryRecord` has, including the empty `detail`: the oracle is the reference a real
    trial is compared against, so its records are shaped exactly like the ones the driver writes.
    """
    return [
        {
            "index": index,
            "kind": _oracle_entry_kind(entry).value,
            "exchange_count": 1,
            "outcome": (TurnOutcome.SATISFIED if isinstance(entry, GoalEntry) else TurnOutcome.COMPLETED).value,
            "detail": "",
        }
        for index, entry in enumerate(case_config.prompts)
    ]


@pure
def _oracle_conversation(case_config: CaseConfig) -> list[dict[str, str]]:
    """The oracle's canned exchange in the driver's clean-conversation shape: one client turn per
    entry, each answered by a short, self-directed reply."""
    conversation: list[dict[str, str]] = []
    final_turn_idx = len(case_config.prompts) - 1
    for index, prompt in enumerate(case_config.prompts):
        conversation.append({"role": "user", "text": _oracle_user_message(prompt)})
        if index == 0:
            reply = _ORACLE_OPENING_REPLY
        elif index == final_turn_idx:
            reply = _ORACLE_FINAL_REPLY
        else:
            reply = _ORACLE_MIDDLE_REPLY
        conversation.append({"role": "agent", "text": reply})
    return conversation


@pure
def render_oracle_trajectory_json(case_config: CaseConfig) -> str:
    """The oracle's trajectory.json: the canned conversation in the hand-built ATIF shape the driver
    writes, so `-a oracle` grades through exactly the verifier path a real trial does."""
    oracle_trajectory = trajectory.build_hand_built_trajectory(
        conversation=_oracle_conversation(case_config),
        provenance=TrajectoryProvenance(
            driver_name=_ORACLE_DRIVER_NAME,
            driver_version=_ORACLE_DRIVER_VERSION,
            decider_model="",
            decider_turns=(),
            harbor_session_id=None,
            case_id=case_config.case_id,
            usage_source=UsageSource.TRANSCRIPT,
        ),
        workspace_usage=summarize_workspace_usage(()),
        timestamp=_ORACLE_TIMESTAMP,
        # The oracle replays one canned conversation, with no steps to divide.
        boundaries=(),
    )
    assert oracle_trajectory is not None, "an eval case always has at least one prompt"
    return json.dumps(oracle_trajectory.to_json_dict(), indent=2)


@pure
def render_outcome_judge_toml(template_text: str) -> str:
    """The outcome judge. Rendered rather than copied so the file list and the weight stay owned by
    this module, where they are derived and tested, instead of drifting inside a static template."""
    judge_files = "[\n{}\n]".format(
        "\n".join('    "{}",'.format(path) for path in OUTCOME_JUDGE_FILES),
    )
    return _substitute_template(
        template_text,
        {"judge_files": judge_files, "judge_weight": str(OUTCOME_JUDGE_WEIGHT)},
    )


@pure
def render_oracle_evidence_shell(case_config: CaseConfig) -> str:
    """The shell that writes the oracle's fabricated (all-green) evidence bundle, so `-a oracle`
    exercises artifact transfer, the outcome criteria, the judge, and the reward composition. Empty
    for cases with no expectations, which must keep grading exactly as they did before."""
    if case_config.expectations is None:
        return ""
    evidence_files = evidence_collection.oracle_evidence_files(case_config)
    lines = [
        "",
        "# The oracle boots no workspace, so the evidence bundle is fabricated: every declared",
        "# check recorded as passed, against a plausible registry and service listing.",
        "rm -f /logs/agent/{}/README.txt".format(evidence_collection.VERIFICATION_DIRNAME),
    ]
    # Every parent directory the bundle needs, derived rather than listed: the flow logs nest one
    # level deeper than the HTTP probes, and a new nested artifact must not need a change here.
    bundle_root = PurePosixPath("/logs/agent") / evidence_collection.VERIFICATION_DIRNAME
    directories = sorted({str(bundle_root / PurePosixPath(name).parent) for name in evidence_files})
    lines += ["mkdir -p {}".format(directory) for directory in directories]
    for relative_name, content in sorted(evidence_files.items()):
        heredoc = "MINDS_EVALS_EVIDENCE_{}_EOF".format(_slug_for_heredoc(relative_name))
        lines.append(
            "cat > /logs/agent/{dirname}/{name} << '{marker}'\n{content}\n{marker}".format(
                dirname=evidence_collection.VERIFICATION_DIRNAME, name=relative_name, content=content, marker=heredoc
            )
        )
    return "\n".join(lines) + "\n"


@pure
def _slug_for_heredoc(relative_name: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in relative_name).upper()


@pure
def render_solve_script(template_text: str, case_config: CaseConfig) -> str:
    entry_count = len(case_config.prompts)
    state = {
        "eval_name": "oracle",
        "case_name": case_config.case_id,
        "mngr_sha": case_config.mngr_sha,
        "dwt_sha": case_config.dwt_sha,
        "waits_done": entry_count,
        "num_turns": entry_count,
        "entries": oracle_entry_records(case_config),
        "test_state": "finished",
        "timed_out": False,
        "started_at": _ORACLE_TIMESTAMP,
        "elapsed_seconds": 0.0,
        "timeout_seconds": case_config.timeout_seconds,
    }
    return _substitute_template(
        template_text,
        {
            "trajectory_json": render_oracle_trajectory_json(case_config),
            "state_json": json.dumps(state, indent=2),
            "verification_evidence_sh": render_oracle_evidence_shell(case_config),
        },
    )


def _copy_template_tree(relative_path: str, dest: Path) -> None:
    # Bytecode caches are excluded: running the template scripts (the unit tests import one) leaves
    # them next to the sources in a dev checkout, from where they would otherwise ship into the
    # verifier image alongside the code they were compiled from.
    with resources.as_file(_TEMPLATES / relative_path) as source:
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _read_template(relative_path: str) -> str:
    return (_TEMPLATES / relative_path).read_text()


# Where the verifier's criteria sit inside its build context. They are one COPY of their own so
# that the image layer holding them is shared across every case and every step, and only the tiny
# case.json layer above it differs; see templates/tests/Dockerfile.
VERIFIER_CRITERIA_DIRNAME: Final[str] = "verifier"


def write_verifier_dir(tests_dir: Path, case_config: CaseConfig) -> None:
    """The separate verifier's build context: the rewardkit criteria plus this case's own data."""
    _copy_template_tree("tests", tests_dir)
    (tests_dir / "case.json").write_text(json.dumps(case_config.model_dump(mode="json"), indent=2))
    # The outcome directory is a scoring dimension, so it must exist ONLY when there are
    # expectations -- rewardkit would otherwise emit a partial score for a case with nothing to
    # score. It lives outside templates/tests/ precisely so it is opted into rather than deleted.
    if case_config.expectations is not None:
        outcome_dir = tests_dir / VERIFIER_CRITERIA_DIRNAME / "outcome"
        _copy_template_tree("outcome", outcome_dir)
        (outcome_dir / "judge.toml").write_text(render_outcome_judge_toml(_read_template("outcome/judge.toml")))


def write_solution_dir(solution_dir: Path, case_config: CaseConfig) -> None:
    """The oracle's canned near-perfect run for one instruction."""
    solution_dir.mkdir(parents=True)
    solve_path = solution_dir / "solve.sh"
    solve_path.write_text(render_solve_script(_read_template("solution/solve.sh"), case_config))
    solve_path.chmod(0o755)


def write_step_workdir(workdir: Path, step: CaseStep, config_dir: Path) -> None:
    """The directory harbor merges into the box before this step runs.

    It carries the step's uploads and the script that moves them somewhere the box's working
    directory is not, since that directory is the mngr checkout every workspace is vendored from.
    A step that introduces nothing writes no workdir at all, which harbor treats as nothing to do.
    """
    if not step.files:
        return
    for step_file in step.files:
        source = step_file_source_path(config_dir, step_file)
        destination = workdir / STEP_FILES_DIRNAME / step_file.upload_id
        destination.mkdir(parents=True)
        # A directory source contributes its contents and a file source becomes the one file
        # inside, so an upload id always names a directory in the workspace -- the shape a real
        # Minds upload has.
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination / source.name)
    setup_path = workdir / "setup.sh"
    setup_path.write_text(render_step_setup_script(_read_template("step_setup.sh"), step.name))
    setup_path.chmod(0o755)


def write_steps_dir(task_dir: Path, case_config: CaseConfig, steps: Sequence[CaseStep], config_dir: Path) -> None:
    """Write `steps/<name>/` for every step of a stepped case.

    Every step carries the whole task in miniature: its own instruction.md, since a multi-step task
    has no top-level one and that is the only channel a harbor agent has to the case config; its own
    tests/, a complete copy of the standard verifier, since harbor REPLACES the build context with a
    step's tests rather than overlaying it; and its own solution/, since harbor prefers a step's
    oracle over the task's and the record each step must fabricate is a different one.
    """
    step_instruction_template = _read_template("step_instruction.md")
    for index, step in enumerate(steps):
        step_dir = task_dir / "steps" / step.name
        step_dir.mkdir(parents=True)
        step_case_config = build_step_case_config(case_config, steps, index)
        (step_dir / "instruction.md").write_text(render_step_instruction(step_instruction_template, step_case_config))
        write_verifier_dir(step_dir / "tests", step_case_config)
        write_solution_dir(step_dir / "solution", build_step_oracle_case_config(case_config, steps, index))
        write_step_workdir(step_dir / "workdir", step, config_dir)


def write_task_dir(
    task_dir: Path, case_config: CaseConfig, case: PersonaCase, config_dir: Path, mngr_source: Path
) -> None:
    """Write one complete harbor task directory for a case."""
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        render_task_toml(_read_template("task.toml"), case_config, case.steps, case.reward_strategy)
    )
    # A multi-step task must NOT have a top-level instruction.md, tests/ or solution/: harbor reads
    # each step's own and would leave the top-level ones unread.
    if case.steps is None:
        (task_dir / "instruction.md").write_text(render_instruction(_read_template("instruction.md"), case_config))
        write_verifier_dir(task_dir / "tests", case_config)
        write_solution_dir(task_dir / "solution", case_config)
    else:
        write_steps_dir(task_dir, case_config, case.steps, config_dir)

    # environment/: identical across all tasks in the dataset (Modal layer-cache
    # builds the box image once per mngr SHA). Per-case data must never land here.
    # The clone is staged WITHOUT .git: Modal's build-context upload drops .git
    # anyway, so nothing in the box may depend on it; the exact SHA travels as a
    # plain file instead (COPYed to /work/mngr_sha, read by the driver).
    environment_dir = task_dir / "environment"
    _copy_template_tree("environment", environment_dir)
    shutil.copytree(mngr_source, environment_dir / "mngr", symlinks=True, ignore=shutil.ignore_patterns(".git"))
    (environment_dir / "mngr_sha").write_text(case_config.mngr_sha + "\n")


# A rough floor for one client message plus the agent turn it draws, from observed runs. Used only
# to warn that a case's worst case cannot fit its budget, never to reject one: how long a turn takes
# is a property of the agent under test, not of the config.
TYPICAL_EXCHANGE_SECONDS: Final[float] = 180.0


@pure
def is_exchange_budget_implausible(prompts: Sequence[PromptEntry], timeout_seconds: float) -> bool:
    """Whether a case's worst-case exchange count cannot plausibly fit the given wall-clock budget."""
    return worst_case_exchange_count(prompts) * TYPICAL_EXCHANGE_SECONDS > timeout_seconds


def _warn_if_timeout_is_implausible(case: PersonaCase, timeout_seconds: float) -> None:
    """Warn when a case's worst-case exchange count cannot plausibly fit its configured budget.

    Authors set `timeout_seconds` themselves, and a goal entry multiplies what a case can spend, so
    a budget sized for the old one-message-per-entry semantics silently becomes a timed-out trial.
    """
    if not is_exchange_budget_implausible(case.prompts, timeout_seconds):
        return
    worst_case_count = worst_case_exchange_count(case.prompts)
    logger.warning(
        "Case {} can send up to {} client message(s), which needs roughly {:.0f}s at {:.0f}s per "
        "exchange, but timeout_seconds is {:.0f}; trials that use the whole budget will time out",
        case.case_id,
        worst_case_count,
        worst_case_count * TYPICAL_EXCHANGE_SECONDS,
        TYPICAL_EXCHANGE_SECONDS,
        timeout_seconds,
    )


@pure
def is_trial_longer_than_the_workspace(
    timeout_seconds: float, verification_timeout_seconds: float, step_count: int
) -> bool:
    """Whether a stepped case's worst case outlasts the one workspace its steps share.

    That workspace is created with the eval overlay, whose sandbox lifetime is a hard ceiling
    nothing in the case config can raise, so a case over it loses the workspace mid-trial.
    """
    return (
        trial_lifetime_seconds(timeout_seconds, verification_timeout_seconds, step_count)
        > minds_bridge.EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS
    )


def _warn_if_trial_outlives_the_workspace(case: PersonaCase, config: EvalConfig) -> None:
    """Warn when a stepped case's worst case cannot fit the workspace it runs in.

    A trial that loses its workspace is recorded as a harness failure rather than as anything about
    the agent, so the shape is worth naming before the dataset is built.

    A warning rather than a rejection, for the same reason `_warn_if_timeout_is_implausible` is one:
    the budgets are worst cases, and how long a trial actually takes is a property of the agent
    under test.
    """
    if case.steps is None:
        return
    step_count = len(case.steps)
    if not is_trial_longer_than_the_workspace(config.timeout_seconds, config.verification_timeout_seconds, step_count):
        return
    logger.warning(
        "Case {} spends up to {:.0f}s across its {} step(s), but the one workspace they share is "
        "capped at {:.0f}s by the {} template; a trial that uses its whole budget loses the "
        "workspace mid-trial. Lower timeout_seconds or verification_timeout_seconds, or drop a step",
        case.case_id,
        trial_lifetime_seconds(config.timeout_seconds, config.verification_timeout_seconds, step_count),
        step_count,
        minds_bridge.EVAL_WORKSPACE_SANDBOX_TIMEOUT_SECONDS,
        minds_bridge.EVAL_WORKSPACE_TEMPLATE,
    )


def _warn_about_step_shapes(case: PersonaCase) -> None:
    """Warn about the two step shapes that generate cleanly and are almost always a mistake."""
    steps = case.steps or ()
    for index, step in enumerate(steps):
        if index == len(steps) - 1:
            continue
        if step.min_reward is None:
            logger.warning(
                "Case {} step {} declares no min_reward, so nothing can abort the trial when it "
                "fails; harbor then runs the next step against a workspace that has already given up",
                case.case_id,
                step.name,
            )
        flow_count = len(step.expectations.ui_flows) if step.expectations is not None else 0
        if flow_count:
            logger.warning(
                "Case {} step {} declares {} UI flow(s), and a flow is not read-only by "
                "construction: whatever it changes stays changed for every later step, where the "
                "agent and the goal-holding client will both see it",
                case.case_id,
                step.name,
                flow_count,
            )


def generate_dataset(config_path: Path, output_dir: Path, mngr_repo: str) -> list[Path]:
    """Generate one harbor task directory per persona case; returns the task directories."""
    config = load_eval_config(config_path)
    # Warned before any network work, since the config alone decides it: an author with a
    # mis-sized budget or an odd step shape should not first wait through two ls-remotes and a
    # clone.
    for case in config.cases:
        _warn_if_timeout_is_implausible(case, config.timeout_seconds)
        _warn_if_trial_outlives_the_workspace(case, config)
        _warn_about_step_shapes(case)
    mngr_sha = resolve_remote_tip(mngr_repo, config.mngr_branch)
    logger.info("Resolved mngr {}@{}", config.mngr_branch, mngr_sha[:12])
    # The workspace template is pinned the same way as mngr: the dataset records the
    # exact SHA and the box clones that, so the same dataset builds the same
    # workspaces however long after generation it is run.
    dwt_sha = resolve_remote_tip(config.dwt_repo, config.dwt_branch)
    logger.info("Resolved dwt {}@{}", config.dwt_branch, dwt_sha[:12])

    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvalConfigError(
            "output dir {} already exists and is not empty -- delete it or pick a new one".format(output_dir)
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    task_dirs: list[Path] = []
    # One shallow clone, copied into every task's environment/ so all copies are
    # byte-identical (local disk pays for the copies; Modal dedupes the upload).
    with tempfile.TemporaryDirectory(prefix="minds-evals-mngr-src-") as staging_dir:
        mngr_source = Path(staging_dir) / "mngr"
        logger.info("Fetching mngr source at {} (shallow clone)", mngr_sha[:12])
        fetch_mngr_source(mngr_repo, mngr_sha, mngr_source)
        for case in config.cases:
            case_config = build_case_config(config, case, mngr_sha, dwt_sha)
            task_dir = output_dir / case.case_id
            logger.info("Writing task {}", task_dir)
            write_task_dir(task_dir, case_config, case, config_path.parent, mngr_source)
            task_dirs.append(task_dir)
    return task_dirs


@click.group()
def main() -> None:
    """Generate harbor task datasets for the Minds persona evals."""
    setup_logging(level="INFO")


@main.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help=(
        "Eval config json: {mngr_branch, dwt_repo?, dwt_branch?, timeout_seconds?, "
        "verification_timeout_seconds?, avg_word_count_baseline?, personas:[...]}"
    ),
)
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Dataset directory to create (one harbor task subdirectory per persona case)",
)
@click.option(
    "--mngr-repo",
    default=MNGR_REPO,
    show_default=True,
    help="The mngr remote the box source is fetched from",
)
def generate(config_path: Path, output_dir: Path, mngr_repo: str) -> None:
    """Generate one harbor task per persona case from an eval config."""
    task_dirs = generate_dataset(config_path=config_path, output_dir=output_dir, mngr_repo=mngr_repo)
    logger.info("Generated {} task(s) in {}", len(task_dirs), output_dir)
    logger.info(
        "Run them from the monorepo root with: uv run --project apps/minds_evals harbor run "
        "-p {} -a imbue.minds_evals.driver:MindsPersonaDriver -e modal -y",
        output_dir,
    )


if __name__ == "__main__":
    main()
