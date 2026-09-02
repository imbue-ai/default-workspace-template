"""Generate a harbor task dataset from a minds eval config (adapter pattern): reads the existing
eval-config JSON schema unchanged and emits one harbor task directory per persona case.

Each task directory carries a byte-identical environment/ (the adapted box Dockerfile, entrypoint,
and a staged shallow clone of mngr-internal at the resolved SHA), so Modal's image-layer cache
builds the box image once per mngr SHA. Per-case data lives only in instruction.md, tests/case.json,
and solution/solve.sh -- never in environment/, or the cache key diverges.
"""

import json
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

import click
from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import trajectory
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DEFAULT_AVG_WORD_COUNT_BASELINE
from imbue.minds_evals.data_types import DEFAULT_DWT_BRANCH
from imbue.minds_evals.data_types import DEFAULT_DWT_REPO
from imbue.minds_evals.data_types import DEFAULT_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import DEFAULT_VERIFICATION_TIMEOUT_SECONDS
from imbue.minds_evals.data_types import EvalConfig
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import PersonaCase
from imbue.minds_evals.data_types import PromptEntry
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.data_types import UsageSource
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
def _normalize_prompts(raw_prompts: object, case_id: str) -> tuple[PromptEntry, ...]:
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
    # The opening ask is what commissions the work, and it is the one entry every reader of the
    # dataset (and the oracle) can take verbatim. Both non-literal forms are refused here rather
    # than only the sentinel: a goal entry could state its own opening ask, but allowing it would
    # make the first message of a case non-deterministic.
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
        prompts = _normalize_prompts(raw_case.get("prompts"), case_id)
        raw_expectations = raw_case.get("expectations")
        cases.append(
            PersonaCase(
                case_id=case_id,
                persona=str(raw_case.get("persona", "")).strip(),
                prompts=prompts,
                expectations=parse_expectations(raw_expectations, case_id) if raw_expectations is not None else None,
            )
        )
    case_ids = [case.case_id for case in cases]
    duplicate_ids = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    if duplicate_ids:
        # Two cases with the same id collide on one task directory, so reject up front.
        raise EvalConfigError("duplicate case id(s): {}".format(", ".join(duplicate_ids)))
    return tuple(cases)


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
    return EvalConfig(
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
def render_task_toml(template_text: str, case_config: CaseConfig) -> str:
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
        },
    )


@pure
def render_prompt_entry_prose(entry: PromptEntry) -> str:
    """One prompts entry as a human reads it in instruction.md.

    A goal entry is rendered as what it is -- a stretch of conversation with a budget -- rather than
    as a message, so a reader never mistakes the goal text for something sent verbatim.
    """
    if isinstance(entry, GoalEntry):
        return "(goal, up to {} exchange(s)) Keep talking until satisfied: {}".format(entry.max_exchanges, entry.goal)
    return "`{}`".format(entry) if entry == DECIDE_SENTINEL else entry


@pure
def render_instruction(template_text: str, case_config: CaseConfig) -> str:
    prompts_prose = "\n".join(
        "{}. {}".format(index + 1, render_prompt_entry_prose(entry)) for index, entry in enumerate(case_config.prompts)
    )
    return _substitute_template(
        template_text,
        {
            "case_id": case_config.case_id,
            "persona_prose": case_config.persona or "(none)",
            "prompts_prose": prompts_prose,
            "case_config_json": json.dumps(case_config.model_dump(), indent=2),
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


def write_task_dir(task_dir: Path, case_config: CaseConfig, mngr_source: Path) -> None:
    """Write one complete harbor task directory for a case."""
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(render_task_toml(_read_template("task.toml"), case_config))
    (task_dir / "instruction.md").write_text(render_instruction(_read_template("instruction.md"), case_config))

    # environment/: identical across all tasks in the dataset (Modal layer-cache
    # builds the box image once per mngr SHA). Per-case data must never land here.
    # The clone is staged WITHOUT .git: Modal's build-context upload drops .git
    # anyway, so nothing in the box may depend on it; the exact SHA travels as a
    # plain file instead (COPYed to /work/mngr_sha, read by the driver).
    environment_dir = task_dir / "environment"
    _copy_template_tree("environment", environment_dir)
    shutil.copytree(mngr_source, environment_dir / "mngr", symlinks=True, ignore=shutil.ignore_patterns(".git"))
    (environment_dir / "mngr_sha").write_text(case_config.mngr_sha + "\n")

    # tests/: the separate verifier's build context (rewardkit criteria + case data).
    tests_dir = task_dir / "tests"
    _copy_template_tree("tests", tests_dir)
    (tests_dir / "case.json").write_text(json.dumps(case_config.model_dump(mode="json"), indent=2))

    # tests/outcome/ is a scoring dimension, so it must exist ONLY for cases that declare
    # expectations -- rewardkit would otherwise emit a partial score for a case with nothing to
    # score. It lives outside templates/tests/ precisely so it is opted into rather than deleted.
    if case_config.expectations is not None:
        outcome_dir = tests_dir / "outcome"
        _copy_template_tree("outcome", outcome_dir)
        (outcome_dir / "judge.toml").write_text(render_outcome_judge_toml(_read_template("outcome/judge.toml")))

    # solution/: the oracle's canned near-perfect run.
    solution_dir = task_dir / "solution"
    solution_dir.mkdir()
    solve_path = solution_dir / "solve.sh"
    solve_path.write_text(render_solve_script(_read_template("solution/solve.sh"), case_config))
    solve_path.chmod(0o755)


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


def generate_dataset(config_path: Path, output_dir: Path, mngr_repo: str) -> list[Path]:
    """Generate one harbor task directory per persona case; returns the task directories."""
    config = load_eval_config(config_path)
    # Warned before any network work, since the config alone decides it: an author with a
    # mis-sized budget should not first wait through two ls-remotes and a clone.
    for case in config.cases:
        _warn_if_timeout_is_implausible(case, config.timeout_seconds)
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
            write_task_dir(task_dir, case_config, mngr_source)
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
