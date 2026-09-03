"""MindsPersonaDriver: a host-side harbor agent that owns the whole persona conversation loop.

The harbor environment is the Minds box; the driver starts the backend with per-trial env, creates
one nested Modal workspace through the production Minds API, drives the scripted multi-turn
conversation against the workspace's system_interface (bridged through ``mngr exec``), snapshots the
workspace after turns, and keeps the ATIF ``trajectory.json`` (what the verifier grades) and
``state.json`` current in the box so even a timed-out trial leaves a gradeable partial record.
"""

import asyncio
import json
import re
import shlex
import time
import uuid
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory
from loguru import logger
from modal.exception import Error as ModalError
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import decider
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import forward_instance
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import proxy_config
from imbue.minds_evals import trajectory as trajectory_building
from imbue.minds_evals import ui_flows
from imbue.minds_evals import usage as usage_accounting
from imbue.minds_evals.data_types import CapturedFile
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import DeciderTurn
from imbue.minds_evals.data_types import EntryRecord
from imbue.minds_evals.data_types import EvidenceManifest
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import PromptEntry
from imbue.minds_evals.data_types import TrajectoryProvenance
from imbue.minds_evals.data_types import TrajectorySource
from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.data_types import TranscriptCapture
from imbue.minds_evals.data_types import TurnEntryKind
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.data_types import UsageSource
from imbue.minds_evals.data_types import WorkerCapture
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.data_types import entry_exchange_budget
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import InstructionParseError
from imbue.minds_evals.errors import TrajectoryDocumentError

# The ATIF trajectory the verifier grades and `harbor view` renders: the driver's hand-built turn
# summary after every turn, so a trial that dies mid-way still leaves a gradeable record, replaced
# by the workspace agent's own document once the evidence phase has captured it (see
# specs/minds-evals-atif-transcripts/spec.md).
TRAJECTORY_FILENAME: Final[str] = "trajectory.json"
STATE_FILENAME: Final[str] = "state.json"
# Token and cost accounting, written host-side beside the trajectory (the verifier does not grade
# it, so unlike the trajectory and state files it is not mirrored into the box).
USAGE_FILENAME: Final[str] = "usage.json"
MINDS_ENV: Final[str] = "staging"

# Electron plus the backend need several minutes on first boot; the agent-level
# override_setup_timeout_sec in the run recipe must cover this.
BACKEND_BOOT_TIMEOUT_SECONDS: Final[float] = 600.0

# The box-local port an in-box LLM proxy would listen on, reverse-forwarded to the same port inside
# the workspace so Claude Code can reach it as a loopback address.
PROXY_PORT: Final[int] = 4000
# Long enough to cover the probe's own fetch without leaving a tunnel open for the whole trial; the
# probe tears itself down when it elapses, so a failure cannot leak a long-lived process.
PROXY_PROBE_HOLD_SECONDS: Final[float] = 180.0
PROXY_PROBE_READY_TIMEOUT_SECONDS: Final[float] = 120.0
PROXY_PROBE_TOKEN: Final[str] = "minds-evals-reverse-tunnel-ok"
# litellm imports its callbacks and starts a uvicorn worker; a cold box takes appreciably longer
# than the ~3s it takes locally.
PROXY_BOOT_TIMEOUT_SECONDS: Final[float] = 180.0
PROXY_TUNNEL_READY_TIMEOUT_SECONDS: Final[float] = 120.0
PROXY_TUNNEL_GRACE_SECONDS: Final[float] = 600.0
# The proxy's per-request metering record, written host-side beside usage.json.
PROXY_USAGE_FILENAME: Final[str] = "usage_proxy.jsonl"

# Each bridge poll is a Modal exec round trip; a run of consecutive failures
# means the bridge is broken, not just a transient blip. Log every few and give
# up (marking the trial) once this many pile up, rather than silently burning
# the whole case budget on a wedged bridge.
_MAX_CONSECUTIVE_FETCH_FAILURES: Final[int] = 20
_FETCH_FAILURE_LOG_INTERVAL: Final[int] = 5

_FENCED_JSON_PATTERN: Final[re.Pattern[str]] = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# What the vendored mngr copy leaves out (ported from the old harness's launch
# module): VCS state and reinstallable build/dependency artifacts.
_VENDOR_EXCLUDES: Final[tuple[str, ...]] = (
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "*.egg-info",
    ".coverage",
)


@pure
def build_eval_base_clone_command(dwt_repo: str, dwt_branch: str, dwt_sha: str, eval_base_dir: str) -> str:
    """The in-box command that materializes the workspace template at the SHA the dataset pinned."""
    # `git clone` can only be pointed at a ref name, so the pin is applied by a
    # second step, and that step must land on a real local branch rather than a
    # detached HEAD: the per-case clone of this directory -- and mngr's clone of
    # that one -- takes its checkout from this HEAD, and the workspace is created
    # with an empty branch field, meaning "whatever HEAD is". Naming the branch
    # after the configured dwt branch keeps the workspace on the branch name it
    # would have had without the pin. --no-checkout avoids populating the large
    # template worktree twice.
    return "rm -rf {base} && git clone --no-checkout {repo} {base} && git -C {base} checkout -B {branch} {sha}".format(
        base=shlex.quote(eval_base_dir),
        repo=shlex.quote(dwt_repo),
        branch=shlex.quote(dwt_branch),
        sha=shlex.quote(dwt_sha),
    )


class SnapshotMode(UpperCaseStrEnum):
    """When the driver snapshots the workspace home tree into the trial artifacts."""

    PER_TURN = auto()
    FINAL = auto()
    OFF = auto()


# Where the pinned workspace-template clone lives in the box: one per trial, shared by every
# per-case clone taken from it.
_EVAL_BASE_DIR: Final[str] = "/work/eval-base"
# Where the per-case clones taken from it live; the workspace is created from its case's clone.
_CLONES_DIR: Final[str] = "/work/clones"
# The type the launch-task skill creates workers as, for a worker the listing no longer shows.
_DEFAULT_WORKER_AGENT_TYPE: Final[str] = "claude"


@pure
def _case_clone_dir(case_id: str) -> str:
    """The unquoted box path of one case's workspace-template clone: what clone prep populates and
    what the workspace is then created from, so both must derive it the same way."""
    return "{}/{}".format(_CLONES_DIR, case_id)


@pure
def build_case_clone_command(clone_dir: str) -> str:
    """Take one case's own clone from the shared eval-base clone, replacing any earlier attempt."""
    return "mkdir -p {clones} && rm -rf {clone} && git clone {base} {clone}".format(
        clones=shlex.quote(_CLONES_DIR),
        clone=shlex.quote(clone_dir),
        base=shlex.quote(_EVAL_BASE_DIR),
    )


@pure
def build_vendor_mngr_command(clone_dir: str) -> str:
    """Overwrite the case clone's vendored mngr with the box's own tree, so the workspace runs the
    mngr under test rather than whatever the template pinned."""
    return (
        "mkdir -p {clone}/system/vendor/mngr && rsync -a --delete {excludes} {mngr} {clone}/system/vendor/mngr/"
    ).format(
        clone=shlex.quote(clone_dir),
        excludes=" ".join("--exclude='{}'".format(pattern) for pattern in _VENDOR_EXCLUDES),
        # The trailing slash is rsync's "contents of", not "the directory itself", and is load-bearing.
        mngr=shlex.quote("{}/".format(minds_bridge.BOX_MNGR_DIR)),
    )


@pure
def build_clone_probe_command(clone_dir: str, eval_base_dir: str) -> str:
    """Where the agent's own history starts and what the base was built from, in one box exec.

    Both shas ride one round trip because both are needed to keep the captured deliverable
    replayable: the bundle is based on the clone's HEAD, and regenerating that base from the dwt tip
    is what proves it reproduces.
    """
    return (
        "printf '{base_marker}\\n'; git -C {clone} rev-parse HEAD; "
        "printf '{dwt_marker}\\n'; git -C {base} rev-parse HEAD"
    ).format(
        clone=shlex.quote(clone_dir),
        base=shlex.quote(eval_base_dir),
        base_marker=evidence_collection.section_marker("base_sha"),
        dwt_marker=evidence_collection.section_marker("dwt_tip_sha"),
    )


@pure
def _agent_kwarg_text(raw_value: object) -> str:
    """What the operator typed after `--ak key=`, whatever Python type it arrived as.

    Harbor JSON-parses every `--ak key=value` before the driver sees it, so the type depends on the
    spelling: `key=true` arrives as a bool, `key=1` as an int, `key=null` as None, and only
    `key=yes` stays a string. Every parser below goes through here rather than assuming the str the
    CLI syntax suggests -- `--ak proxy=1` is a spelling they advertise, and it does not arrive as one.

    Whitespace is stripped but case is preserved, so a parser that cares about case can still see it.
    """
    return "" if raw_value is None else str(raw_value).strip()


# The spellings every boolean `--ak` flag on this driver reads the same way, so `--ak proxy=on`
# cannot come to mean the opposite of `--ak proxy_probe=on`. Shared rather than repeated because a
# docstring is not enough to hold two parsers in step.
_TRUE_FLAG_SPELLINGS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE_FLAG_SPELLINGS: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


@pure
def parse_agent_flag(raw_value: object, name: str) -> bool:
    """A boolean agent kwarg, however harbor delivered it.

    Every unrecognised spelling is rejected rather than read as False. A flag that silently means
    "off" whenever it cannot be understood turns a typo into a trial that ran the other arm and
    reported the one that was asked for, which is worse than a run that refuses to start.
    """
    text = _agent_kwarg_text(raw_value).lower()
    if text in _TRUE_FLAG_SPELLINGS:
        return True
    if text in _FALSE_FLAG_SPELLINGS:
        return False
    raise AgentKwargError(
        "{} {!r} is not a boolean; expected one of true/false/yes/no/on/off/1/0".format(name, raw_value)
    )


@pure
def parse_snapshot_mode(raw_value: object) -> SnapshotMode:
    """The snapshot cadence, rejecting a spelling this driver cannot honour.

    `SnapshotMode(...)` raises a bare ValueError naming the enum, which reads as an internal fault
    rather than a bad kwarg; this names the option and what it accepts.
    """
    text = _agent_kwarg_text(raw_value)
    try:
        return SnapshotMode(text.replace("-", "_").upper())
    except ValueError:
        raise AgentKwargError(
            "snapshot_mode {!r} is not a snapshot cadence; expected one of {}".format(
                raw_value, ", ".join(mode.value.lower().replace("_", "-") for mode in SnapshotMode)
            )
        ) from None


class SnapshotPoint(UpperCaseStrEnum):
    """A place in the conversation loop where a snapshot could be taken.

    The two are distinguished because which exchange ends an entry is not known until the entry's
    source is asked again: taking the `final` cadence's one snapshot per exchange, to keep the last,
    would pull a whole tarball per exchange of a goal entry.
    """

    AFTER_EXCHANGE = auto()
    AFTER_FINAL_ENTRY = auto()


@pure
def is_snapshot_wanted(snapshot_mode: SnapshotMode, point: SnapshotPoint) -> bool:
    match snapshot_mode:
        case SnapshotMode.PER_TURN:
            return point == SnapshotPoint.AFTER_EXCHANGE
        case SnapshotMode.FINAL:
            return point == SnapshotPoint.AFTER_FINAL_ENTRY
        case SnapshotMode.OFF:
            return False
        case _ as unreachable:
            assert_never(unreachable)


@pure
def parse_case_config(instruction: str) -> CaseConfig:
    """Pull the case config out of the instruction's fenced JSON block (custom harbor agents do not
    receive the task directory, so the instruction carries the machine-readable case)."""
    blocks = _FENCED_JSON_PATTERN.findall(instruction)
    if not blocks:
        raise InstructionParseError("no fenced json block in the task instruction")
    try:
        raw_case = json.loads(blocks[-1])
    except ValueError as exc:
        raise InstructionParseError("instruction json block is not valid JSON: {}".format(exc)) from exc
    return CaseConfig.model_validate(raw_case)


@pure
def sanitize_user_id(text: str) -> str:
    """A trial name -> a Modal user_id fragment (lowercase alnum + dashes, bounded length); Modal env
    names (minds-<env>-<user_id>) are restrictive."""
    slug = "".join(character if character.isalnum() else "-" for character in text.lower())
    return re.sub(r"-+", "-", slug).strip("-")


@pure
def derive_user_id(trial_name: str, salt: str) -> str:
    """The per-trial Modal user id: the sanitized trial name plus a fresh per-run salt, so re-runs or
    resumes can never collide even if a trial name repeats."""
    base = sanitize_user_id(trial_name)[:31].rstrip("-") or "trial"
    return "{}-{}".format(base, salt)


class Say(FrozenModel):
    """The client's next message for this exchange."""

    text: str = Field(description="What the simulated client says")


class Done(FrozenModel):
    """The entry is over; nothing more will be said for it."""

    reason: TurnOutcome = Field(description="Why the entry stopped")
    detail: str = Field(default="", description="The source's own words for why it stopped, if it gave any")


# What one exchange of an entry asks of its source. The loop performs a Say and ends the entry on a
# Done; a source never touches the environment, so this union is the whole contract.
TurnAction = Say | Done


class TurnSource(MutableModel, ABC):
    """Produces the simulated user's messages for one prompts entry, one exchange at a time."""

    results: list[DeciderResult] = Field(
        default_factory=list, description="One entry per decider-model call this source made"
    )

    @property
    @abstractmethod
    def kind(self) -> TurnEntryKind:
        """Which kind of entry this source implements, as recorded in state.json."""

    @property
    @abstractmethod
    def exhaustion_end(self) -> Done:
        """How the entry ended when its exchange budget stopped this source, reason and detail both.

        A fixed-script source has exactly one message and is COMPLETED once it has been sent; only a
        goal-holding client can actually be cut off mid-conversation. Asking the source for one more
        action just to learn which it was would cost a real model call per exhausted entry.

        It is the same `Done` the source would have returned had it been asked again, so an ending
        the budget pre-empts is recorded exactly as one the source reported itself.
        """

    @abstractmethod
    def next_action(self, case: CaseConfig, transcript: Transcript) -> TurnAction:
        """The client's next message, or the reason this entry is over."""


class SingleMessageTurnSource(TurnSource, ABC):
    """A source whose entry is exactly one message: it says its piece once, then the entry is over.

    Holds the say-once rule in one place so that "a string prompts entry is one turn, and a spent
    budget of one means COMPLETED rather than a client that was cut off" cannot drift between the
    sources that share it.
    """

    is_message_said: bool = Field(default=False, description="Whether this entry's one message has been said")

    @property
    def exhaustion_end(self) -> Done:
        return Done(reason=TurnOutcome.COMPLETED)

    @abstractmethod
    def _next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        """This entry's one message."""

    def next_action(self, case: CaseConfig, transcript: Transcript) -> TurnAction:
        if self.is_message_said:
            return Done(reason=TurnOutcome.COMPLETED)
        message = self._next_message(case, transcript)
        self.is_message_said = True
        return Say(text=message)


class LiteralTurnSource(SingleMessageTurnSource):
    """Deterministic: says the config's literal prompt string verbatim, once."""

    prompt: str = Field(frozen=True, description="The literal message sent for this turn")

    @property
    def kind(self) -> TurnEntryKind:
        return TurnEntryKind.LITERAL

    def _next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        return self.prompt


class PersonaLLMTurnSource(SingleMessageTurnSource):
    """Non-deterministic: renders the persona plus the transcript so far into the role-play prompt,
    calls the decider model once, and falls back to the literal "Sounds good." on any error."""

    model: str = Field(frozen=True, description="The decider model")
    api_key: SecretStr = Field(frozen=True, description="Anthropic API key for the decider")

    @property
    def kind(self) -> TurnEntryKind:
        return TurnEntryKind.PERSONA

    def _next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        result = decider.decide_next_message(
            persona=case.persona,
            transcript=transcript,
            model=self.model,
            api_key=self.api_key.get_secret_value(),
        )
        self.results.append(result)
        return result.message


# What a goal entry's record says when the client's own model call degraded: the literal fallback
# line went out in place of a real message, and the entry ended there.
FALLBACK_ENTRY_DETAIL: Final[str] = "The goal client's model call failed; the fallback line was sent instead."


class GoalTurnSource(TurnSource):
    """A goal-holding client: one forced-tool call per exchange either says the next thing or
    declares the goal met.

    It judges satisfaction from the conversation alone, exactly as a real non-technical client does:
    it never reaches into the workspace, and out-of-band verification stays the ground truth for
    whether the goal was actually achieved.
    """

    goal: str = Field(frozen=True, description="What this entry's client is holding out for")
    model: str = Field(frozen=True, description="The decider model")
    api_key: SecretStr = Field(frozen=True, description="Anthropic API key for the decider")
    is_fallback_said: bool = Field(default=False, description="Whether the degraded fallback line has been sent")

    @property
    def kind(self) -> TurnEntryKind:
        return TurnEntryKind.GOAL

    @property
    def exhaustion_end(self) -> Done:
        # The fallback line is sent as a message, so an entry whose LAST allowed exchange was the
        # degraded one is stopped by the budget before this source can report FALLBACK itself. That
        # is a harness outage, not an agent that failed the client, and must be recorded as one --
        # otherwise an entry with a budget of 1 could never be recorded as a fallback at all.
        if self.is_fallback_said:
            return Done(reason=TurnOutcome.FALLBACK, detail=FALLBACK_ENTRY_DETAIL)
        return Done(reason=TurnOutcome.BUDGET_EXHAUSTED)

    def next_action(self, case: CaseConfig, transcript: Transcript) -> TurnAction:
        # A degraded call already cost the trial a full agent turn on a line that carries no goal.
        # Ending the entry there keeps a flaky API from spending the whole budget on pleasantries.
        if self.is_fallback_said:
            return Done(reason=TurnOutcome.FALLBACK, detail=FALLBACK_ENTRY_DETAIL)
        decision = decider.decide_goal_action(
            persona=case.persona,
            goal=self.goal,
            transcript=transcript,
            model=self.model,
            api_key=self.api_key.get_secret_value(),
        )
        self.results.append(decision.call)
        if decision.call.is_fallback:
            self.is_fallback_said = True
            return Say(text=decision.call.message)
        if decision.is_satisfied:
            logger.info("The client is satisfied: {}", decision.satisfaction_reason)
            return Done(reason=TurnOutcome.SATISFIED, detail=decision.satisfaction_reason)
        return Say(text=decision.call.message)


@pure
def resolve_turn_sources(case: CaseConfig, decider_model: str, api_key: str) -> list[TurnSource]:
    """Map each prompts entry to its own turn source: a literal string -> LiteralTurnSource, the
    DECIDE_FROM_PERSONA sentinel -> PersonaLLMTurnSource, a goal object -> GoalTurnSource.

    One source per entry rather than a shared one: a source carries per-entry state (whether it has
    said its piece, which goal it holds), and the driver -- not the sources -- accumulates the
    decider usage across the whole conversation.
    """
    sources: list[TurnSource] = []
    for entry in case.prompts:
        if isinstance(entry, GoalEntry):
            sources.append(GoalTurnSource(goal=entry.goal, model=decider_model, api_key=SecretStr(api_key)))
        elif entry == DECIDE_SENTINEL:
            sources.append(PersonaLLMTurnSource(model=decider_model, api_key=SecretStr(api_key)))
        else:
            sources.append(LiteralTurnSource(prompt=entry))
    return sources


@pure
def _agent_reply_text(event: Mapping[str, Any]) -> str:
    """One raw event's agent-facing reply text, or "" when it is not an agent turn.

    Reads both common-transcript vintages: the ATIF-shaped ``step`` record with ``source: "agent"``
    (whose text is ``message``) that mngr's emitters write, and the legacy ``assistant_message``
    record the workspace system_interface still produces."""
    if event.get("type") == "step" and event.get("source") == "agent":
        return str(event.get("message") or "").strip()
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "").strip()
    return ""


@pure
def _new_agent_reply_texts(events: list[dict[str, Any]], baseline_event_count: int) -> list[str]:
    """The non-empty agent reply texts at or after ``baseline_event_count`` (the event count captured
    just before the turn was sent). Anchoring on the send-time index -- rather than "after the last
    user turn" -- avoids being fooled by framework-injected user messages (the /welcome skill
    body, queued prompts, is_meta events) that can land after the agent's reply."""
    return [text for event in events[baseline_event_count:] if (text := _agent_reply_text(event))]


@pure
def _words_per_agent_turn(conversation: list[dict[str, str]]) -> list[int]:
    """Word count of each agent turn -- one entry per client turn, over the turn's merged reply
    (several agent messages joined). Contrast ``average_words_per_message``, which counts each agent
    message on its own."""
    return [len(entry["text"].split()) for entry in conversation if entry["role"] == "agent" and entry["text"]]


@pure
def _conversation_events(conversation: list[dict[str, str]]) -> list[dict[str, str]]:
    """The clean conversation rendered back into the workspace event schema (user_message.content /
    assistant_message.text), which is the shape the decider's prompt renders."""
    events: list[dict[str, str]] = []
    for entry in conversation:
        if entry["role"] == "user":
            events.append({"type": "user_message", "content": entry["text"]})
        else:
            events.append({"type": "assistant_message", "text": entry["text"]})
    return events


# The fixed identity and timestamp the eval-case commit is made with. A commit hash is a function of
# its tree, parent, author, committer, AND dates, so a wall-clock date would give every trial a
# different base sha for an identical tree -- and the captured deliverable.bundle, which is based on
# that commit, could then never be unbundled onto a regenerated clone. Pinning the dates makes the
# base a pure function of the dwt SHA, the mngr SHA, and the vendor exclude list, which is exactly
# what makes the retroactive fresh-environment replay the bundle exists for possible.
EVAL_CASE_COMMIT_DATE: Final[str] = "1970-01-01T00:00:00 +0000"
EVAL_CASE_COMMIT_EMAIL: Final[str] = "eval@minds"
EVAL_CASE_COMMIT_NAME: Final[str] = "minds-eval"


@pure
def build_eval_case_commit_command(clone_dir: str, commit_message: str) -> str:
    """The eval-case commit, made reproducibly: fixed identity and fixed author/committer dates."""
    return (
        "cd {clone} && git add -A && "
        "GIT_AUTHOR_DATE={date} GIT_COMMITTER_DATE={date} "
        "git -c user.email={email} -c user.name={name} commit -q -m {message}"
    ).format(
        clone=shlex.quote(clone_dir),
        date=shlex.quote(EVAL_CASE_COMMIT_DATE),
        email=shlex.quote(EVAL_CASE_COMMIT_EMAIL),
        name=shlex.quote(EVAL_CASE_COMMIT_NAME),
        message=shlex.quote(commit_message),
    )


@pure
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pure
def _box_trial_file_path(filename: str) -> str:
    """Where one trial file is mirrored in the box, under the task's declared artifact directory."""
    return "{}/{}".format(minds_bridge.BOX_LOGS_DIR, filename)


@pure
def _usage_source(resolved_usage: usage_accounting.ResolvedWorkspaceUsage) -> UsageSource:
    return UsageSource.PROXY if resolved_usage.is_from_proxy else UsageSource.TRANSCRIPT


@pure
def _trajectory_json(trajectory: Trajectory) -> str:
    return json.dumps(trajectory.to_json_dict(), indent=2)


def _read_worker_streams(captures: Sequence[WorkerCapture]) -> dict[str, list[dict[str, Any]]]:
    """Each captured worker stream's records by worker name, for the usage account; a stream that
    cannot be read is left out, which the completeness figure then reflects."""
    records_by_name: dict[str, list[dict[str, Any]]] = {}
    for capture in captures:
        if capture.stream.host_path is None:
            continue
        try:
            content = capture.stream.host_path.read_text()
        except OSError as exc:
            logger.warning("Could not read worker {}'s captured stream: {}", capture.launch.name, exc)
            continue
        records_by_name[capture.launch.name] = trajectory_building.parse_transcript_jsonl(content)
    return records_by_name


@pure
def _settled_worker_count(
    captures: Sequence[WorkerCapture], stream_records_by_name: Mapping[str, Sequence[Mapping[str, Any]]]
) -> int:
    """How many captured workers the transcript account holds whole: their stream was read into it and
    they are known to have settled by capture time (stopped in place, or destroyed after finishing).
    A worker still running contributes its spend so far, not its whole spend, so it is summed but not
    counted; one whose state could not be established is treated the same way, and neither is one
    whose stream was never read."""
    return sum(
        1
        for capture in captures
        if capture.launch.name in stream_records_by_name
        and capture.state in (WorkerState.STOPPED, WorkerState.DESTROYED)
    )


def _worker_document_or_none(capture: WorkerCapture) -> dict[str, Any] | None:
    """The worker's document: the one mngr built inside the workspace when it was captured, else one
    built here from its stream (a destroyed worker only leaves its preserved stream), else None."""
    document_path = capture.document.host_path
    if document_path is not None:
        try:
            return trajectory_building.parse_worker_document(document_path.read_text())
        except (OSError, TrajectoryDocumentError) as exc:
            logger.warning(
                "Worker {}'s captured document is unusable; building one from its stream: {}",
                capture.launch.name,
                exc,
            )
    stream_path = capture.stream.host_path
    if stream_path is None or not capture.agent_id:
        logger.warning(
            "Worker {} is not embedded in the trajectory: {}",
            capture.launch.name,
            "its stream was not captured" if stream_path is None else "no agent id was resolved to build it under",
        )
        return None
    try:
        return trajectory_building.build_worker_trajectory_from_stream(
            stream_path.read_text(), capture.agent_id, capture.agent_type or _DEFAULT_WORKER_AGENT_TYPE
        )
    except (OSError, TrajectoryDocumentError) as exc:
        logger.warning("Could not build worker {}'s trajectory from its stream: {}", capture.launch.name, exc)
        return None


def _embedded_workers(
    captures: Sequence[WorkerCapture], host_logs_dir: Path
) -> list[trajectory_building.EmbeddedWorker]:
    """The chat agent's workers ready to embed, each with its own workers already grafted in: deepest
    first, so a worker's document is complete before its lead's is assembled. Each report path is
    named relative to the logs dir, the bundle root the verifier reads."""
    embedded_by_name: dict[str, trajectory_building.EmbeddedWorker] = {}
    for capture in sorted(captures, key=lambda capture: capture.launch.depth, reverse=True):
        document = _worker_document_or_none(capture)
        if document is None:
            continue
        children = [
            embedded_by_name[child.launch.name]
            for child in captures
            if child.launch.lead_name == capture.launch.name and child.launch.name in embedded_by_name
        ]
        report_path = capture.report.host_path
        embedded_by_name[capture.launch.name] = trajectory_building.EmbeddedWorker(
            launch=capture.launch,
            document=trajectory_building.graft_worker_trajectories(document, children),
            state=capture.state,
            report_path=report_path.relative_to(host_logs_dir).as_posix() if report_path is not None else "",
        )
    # A worker's own workers embed inside its document, so a lead that could not be embedded takes
    # them out of the trajectory with it, however sound their documents were -- and its workers'
    # workers too, down the whole chain. Shallowest first, so each lead is placed before its workers.
    reachable_names: set[str] = set()
    for capture in sorted(captures, key=lambda capture: capture.launch.depth):
        if capture.launch.name not in embedded_by_name:
            continue
        if capture.launch.depth == 0 or capture.launch.lead_name in reachable_names:
            reachable_names.add(capture.launch.name)
            continue
        logger.warning(
            "Worker {} is not embedded in the trajectory: its lead {} was not",
            capture.launch.name,
            capture.launch.lead_name,
        )
    return [
        embedded_by_name[capture.launch.name]
        for capture in captures
        if capture.launch.depth == 0 and capture.launch.name in embedded_by_name
    ]


@pure
def _captured_file_metadata(captured: CapturedFile) -> dict[str, Any]:
    """One captured file's outcome as trial metadata: whether it came out and, if not, why."""
    return {
        "is_captured": captured.is_captured,
        "reason": captured.failure_reason,
        "detail": captured.failure_detail,
    }


@pure
def _worker_capture_metadata(
    capture: WorkerCapture, stream_records: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """The capture's outcome as trial metadata: what the worker is, per part whether it came out and,
    if not, why, and the worker's own usage account (None when its stream was not read), so the
    delegated spend can be reconciled per worker against the proxy's figures."""
    return {
        "name": capture.launch.name,
        "agent_type": capture.agent_type,
        "agent_id": capture.agent_id,
        "state": capture.state.value,
        "depth": capture.launch.depth,
        "lead_name": capture.launch.lead_name,
        "launch_tool_call_id": capture.launch.tool_call_id,
        **{
            part: _captured_file_metadata(captured)
            for part, captured in (
                ("document", capture.document),
                ("stream", capture.stream),
                ("report", capture.report),
            )
        },
        "usage": (
            usage_accounting.workspace_usage_metadata(usage_accounting.summarize_workspace_usage(stream_records))
            if stream_records is not None
            else None
        ),
    }


@pure
def _transcript_capture_metadata(capture: TranscriptCapture) -> dict[str, Any]:
    """The capture's outcome as trial metadata: per half, whether it came out and, if not, why."""
    return {
        name: _captured_file_metadata(captured)
        for name, captured in (("stream", capture.stream), ("document", capture.document))
    }


class MindsPersonaDriver(BaseAgent):
    """The host-side persona-driver agent: one Minds box per trial, one nested workspace per case."""

    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        *args: Any,
        snapshot_mode: object = "per-turn",
        modal_config_path: str = "",
        poll_seconds: float = 5.0,
        proxy_probe: object = False,
        proxy: object = False,
        verifier_model: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Flow driving is mechanical, so a cheaper tier may well do; until that is measured the
        # verification agent runs on the decider's model, with this override to measure it.
        self._verifier_model_override = verifier_model.strip()
        # Opt-in check that a box-local port is reachable from inside the workspace, which is what
        # an in-box LLM proxy would depend on. Off by default: it costs an extra bridge round trip.
        self._is_proxy_probe_enabled = parse_agent_flag(proxy_probe, "proxy_probe")
        # Route the workspace's model traffic through a proxy in the box, so every call -- including
        # any the agent delegates -- is metered where the agent cannot reach it.
        self._is_proxy_enabled = parse_agent_flag(proxy, "proxy")
        self._proxy_key: str = ""
        self._proxy_usage_records: tuple[dict[str, Any], ...] = ()
        self._snapshot_mode = parse_snapshot_mode(snapshot_mode)
        self._modal_config_path = modal_config_path
        self._poll_seconds = float(poll_seconds)
        # An 8-hex salt (not the full uuid4 hex): the salted trial name must fit
        # the Modal env name budget (minds-<env>-<user_id>, user_id capped at 40
        # chars like the old harness) while keeping the trial name readable.
        self._salt = uuid.uuid4().hex[:8]
        self._user_id: str = ""
        self._mngr_sha: str = ""
        self._api_port: str = ""
        self._box_env: dict[str, str] | None = None
        self._workspace_agent_id: str = ""
        self._chat_agent_id: str = ""
        self._latest_events: list[dict[str, Any]] = []
        # The number of chat events already pulled into _latest_events, so each
        # poll fetches only the new window instead of the whole history.
        self._seen_event_count: int = 0
        # The clean per-turn conversation: {"role": "user"/"agent", "text": ...}.
        self._conversation: list[dict[str, str]] = []
        # Word count of each individual agent message (accumulated across turns,
        # before the per-turn merge), the raw material for average_words_per_message.
        self._agent_message_word_counts: list[int] = []
        # Every decider-model call the turn sources made, in conversation order: the results for the
        # harness's own usage account, and the audit trail the trajectory's provenance carries.
        # Accumulated on the driver because each entry has a source of its own, and the usage is
        # the whole run's.
        self._decider_results: list[DeciderResult] = []
        self._decider_turns: list[DeciderTurn] = []
        # How each prompts entry played out: its kind, the exchanges it actually sent, and why it
        # stopped. The structural gates are founded on these.
        self._entry_records: list[EntryRecord] = []
        self._transcript_capture: TranscriptCapture = evidence_collection.not_attempted_transcript_capture()
        # The background workers the evidence phase captured, their streams read back for the usage
        # account, and the launches the capture's caps left out.
        self._worker_captures: list[WorkerCapture] = []
        self._worker_stream_records_by_name: dict[str, list[dict[str, Any]]] = {}
        self._worker_capture_overflow: list[str] = []
        # The trajectory.json text the box last accepted, so a failed final publish can put the host
        # copy back to exactly what the verifier will read.
        self._box_trajectory_json: str | None = None
        self._case: CaseConfig | None = None
        self._started_at: float = 0.0
        # Client messages actually sent across the whole conversation, which is not the same thing
        # as the entry count: one goal entry can send several.
        self._waits_done: int = 0
        self._test_state: str = "ongoing"
        # HEAD of the per-case dwt clone the workspace was created from: the base of the
        # incremental git bundle the evidence phase captures, so the recorded deliverable is only
        # the agent's own commits.
        self._clone_base_sha: str = ""
        # The dwt tip the base clone was made from, recorded so a replay can regenerate that base
        # and check it reproduces _clone_base_sha before unbundling the agent's commits onto it.
        self._dwt_tip_sha: str = ""
        # What the workspace already served before the agent ran, so the evidence phase can tell the
        # delivered apps from the ones that booted with the workspace.
        self._preexisting_registrations: frozenset[str] | None = None
        self._verification_metadata: dict[str, Any] = {}
        self._verifier_usage: ui_flows.VerifierUsage | None = None

    @staticmethod
    def name() -> str:
        return "minds-persona-driver"

    def version(self) -> str | None:
        return "0.1.0"

    @property
    def _decider_model(self) -> str:
        return self._parsed_model_name or decider.DEFAULT_DECIDER_MODEL

    @property
    def _verifier_model(self) -> str:
        return self._verifier_model_override or self._decider_model

    async def setup(self, environment: BaseEnvironment) -> None:
        # Create the evidence directory before anything else can fail. harbor records a missing
        # declared artifact path as a failed entry, and `harbor trial regrade` refuses any trial
        # that has one -- an EMPTY directory is tolerated, an absent one is not. Without this, a
        # trial that dies before the collection phase would be permanently non-regradable.
        await evidence_collection.ensure_evidence_dir(environment)
        # The staged mngr clone's HEAD is the exact SHA the dataset was generated
        # at, so the box env can be built without seeing the instruction.
        self._mngr_sha = await minds_bridge.read_box_mngr_sha(environment)
        trial_name = self.logs_dir.parent.name
        self._user_id = derive_user_id(trial_name, self._salt)
        modal_config_path = (
            Path(self._modal_config_path) if self._modal_config_path else minds_bridge.default_modal_config_path()
        )
        self._box_env = minds_bridge.build_box_env(
            activation_env=await minds_bridge.fetch_minds_activation_env(environment, MINDS_ENV),
            modal_token_env=minds_bridge.load_modal_token_env(modal_config_path),
            user_id=self._user_id,
            mngr_sha=self._mngr_sha,
            minds_env=MINDS_ENV,
        )
        logger.info("Starting the Minds backend (trial user id {})", self._user_id)
        await minds_bridge.start_backend(environment, self._box_env)
        self._api_port = await minds_bridge.discover_api_port(environment, self._box_env, BACKEND_BOOT_TIMEOUT_SECONDS)
        logger.info("Minds backend is up on port {}", self._api_port)

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        case = parse_case_config(instruction)
        self._case = case
        self._started_at = time.time()
        deadline = self._started_at + case.timeout_seconds
        try:
            await self._run_conversation(case, environment, deadline)
        finally:
            # Before anything is torn down: the workspace (and the app inside it) is only alive
            # here, and the verifier runs long after it is gone.
            await self._collect_verification_evidence(environment)
            if self._is_proxy_enabled:
                await self._collect_proxy_usage(environment)
            # Written here rather than in populate_context_post_run: harbor only
            # calls that hook when the agent context is still empty, and this
            # driver always populates the context below.
            trajectory_source = await self._publish_trajectory(case, environment)
            self._populate_context_metadata(context, trajectory_source)
            await self._teardown(environment)

    def _build_verification_agent(self) -> ui_flows.VerificationAgent | None:
        """The UI-flow agent, or None when there is no key to run it with. The upstream key is used
        rather than the trial's proxy key: this is the harness reasoning about the workspace, not
        the workspace's own traffic, so it must not be metered as the agent under test's spend."""
        api_key = self._get_env("ANTHROPIC_API_KEY") or ""
        if not api_key:
            logger.warning("No ANTHROPIC_API_KEY for the UI-flow verification agent; flows cannot be measured")
            return None
        return ui_flows.AnthropicVerificationAgent(
            model=self._verifier_model,
            api_key=SecretStr(api_key),
            timeout_seconds=ui_flows.DEFAULT_CALL_TIMEOUT_SECONDS,
        )

    async def _collect_verification_evidence(self, environment: BaseEnvironment) -> None:
        """Capture what the delivered workspace actually is, while it still exists.

        The cheap registry/service/inventory/transcript capture runs for every trial that got as far
        as a workspace; the expectations-driven probes only run when the conversation finished, since an
        unfinished trial's structural gates already zero its reward. Any failure here is swallowed:
        evidence is best-effort and must never discard an already-completed trial or block the
        teardown that stops the nested sandboxes from leaking.
        """
        if self._box_env is None or self._case is None or not self._workspace_agent_id:
            return
        collector = evidence_collection.EvidenceCollector(
            environment=environment,
            box_env=self._box_env,
            workspace_agent_id=self._workspace_agent_id,
            chat_agent_id=self._chat_agent_id,
            case=self._case,
            clone_base_sha=self._clone_base_sha,
            dwt_tip_sha=self._dwt_tip_sha,
            preexisting_registrations=self._preexisting_registrations,
            host_logs_dir=self.logs_dir,
            # Monotonic, unlike the conversation's own deadline: a clock step during a ten-minute
            # collection phase would otherwise truncate or extend it.
            deadline=time.monotonic() + self._case.verification_timeout_seconds,
            verifier_model=self._verifier_model,
            verification_agent=self._build_verification_agent(),
            preauth_cookie=SecretStr(forward_instance.mint_forward_secret()),
            browser_bridge_token=SecretStr(forward_instance.mint_forward_secret()),
        )
        logger.info("Collecting outcome-verification evidence from the workspace")
        manifest: EvidenceManifest | None
        try:
            manifest = await collector.collect(
                is_expectations_collection_wanted=self._test_state == "finished",
            )
        except Exception as exc:
            logger.opt(exception=exc).warning("Evidence collection failed; grading on what it managed to write")
            manifest = None
        # Whatever the flow agent spent before a failure is still spent, so keep the account, and
        # whatever the transcript capture brought out before it is still worth having.
        self._verifier_usage = collector.verifier_usage()
        self._transcript_capture = collector.transcript_capture
        self._worker_captures = list(collector.worker_captures)
        self._worker_capture_overflow = list(collector.worker_capture_overflow)
        self._worker_stream_records_by_name = _read_worker_streams(self._worker_captures)
        if manifest is None:
            return
        self._verification_metadata = {
            "is_evidence_complete": manifest.is_evidence_complete,
            "entry_count": len(manifest.entries),
            "failed_entry_count": sum(1 for entry in manifest.entries if entry.status == CheckStatus.FAILED),
            "error_entry_count": sum(1 for entry in manifest.entries if entry.status == CheckStatus.ERROR),
        }
        logger.info(
            "Recorded {} verification entr(ies) ({} failed, {} errored)",
            len(manifest.entries),
            self._verification_metadata["failed_entry_count"],
            self._verification_metadata["error_entry_count"],
        )

    async def _teardown(self, environment: BaseEnvironment) -> None:
        """Destroy the trial's workspace sandboxes, swallowing any teardown error so a transport
        hiccup at cleanup never discards an already-completed trial or masks the original failure
        (the nested sandboxes' own timeout is the backstop)."""
        if self._box_env is None:
            return
        try:
            await minds_bridge.destroy_workspaces(environment, self._box_env)
        except Exception as exc:
            logger.warning("Workspace teardown raised (sandbox timeout is the backstop): {}", exc)

    async def _run_conversation(self, case: CaseConfig, environment: BaseEnvironment, deadline: float) -> None:
        assert self._box_env is not None, "setup() must run before run()"
        await self._sync_trial_files(environment)

        # Prepare the per-case dwt clone inside the box and create the workspace
        # through the production Minds API path.
        await self._prepare_workspace_clone(case, environment)
        workspace_host_name = "EVAL-{}".format(self._user_id[:34])
        payload = minds_bridge.build_create_payload(
            dwt_repo=_case_clone_dir(case.case_id),
            dwt_branch="",
            host_name=workspace_host_name,
        )
        logger.info("Creating the workspace for case {}", case.case_id)
        self._workspace_agent_id = await minds_bridge.create_workspace_and_wait(
            environment, self._box_env, self._api_port, payload, deadline, self._poll_seconds
        )
        logger.info("Workspace is up (agent {})", self._workspace_agent_id)

        if self._is_proxy_probe_enabled:
            await self._probe_reverse_tunnel(environment)
        if self._is_proxy_enabled:
            is_proxy_up = await self._start_proxy(environment, case)
            if not is_proxy_up:
                await self._mark_timed_out(environment, "the in-box LLM proxy did not come up")
                return

        sign_in = await self._authenticate_workspace(environment, deadline)
        if not sign_in.is_signed_in:
            await self._mark_timed_out(environment, "could not authenticate the workspace")
            return

        # A workspace boots with no chat, and a chat binds to the account it is created against, so
        # this can only happen once the sign-in above has minted one.
        chat_agent_id = await minds_bridge.create_chat_agent(
            environment,
            self._box_env,
            self._workspace_agent_id,
            workspace_host_name,
            sign_in.account_id,
            deadline,
            self._poll_seconds,
        )
        if chat_agent_id is None:
            await self._mark_timed_out(environment, "could not create the workspace chat agent")
            return
        self._chat_agent_id = chat_agent_id

        is_chat_ready = await minds_bridge.wait_for_chat_state(
            environment,
            self._box_env,
            self._workspace_agent_id,
            self._chat_agent_id,
            is_waiting_desired=True,
            deadline=deadline,
            poll_seconds=self._poll_seconds,
        )
        if not is_chat_ready:
            await self._mark_timed_out(environment, "the workspace chat agent never reached WAITING")
            return

        # WAITING alone does not mean the chat is done being set up. The workspace's first chat is
        # created with `/welcome` as its initial message and types it in only once the agent reports
        # ready, so the chat is listed as WAITING -- carrying no messages at all -- for as long as
        # that delivery takes. Turn 1 must not be sent into that window: it would race the welcome's
        # own keystrokes, and the greeting would land past turn 1's baseline, where it would be
        # recorded and graded as the answer to turn 1. Waiting for the greeting itself is what
        # separates the two; polling for a RUNNING edge would miss a welcome that finished between
        # two polls.
        # The baseline is the whole stream: this chat is new, so every event in it is the welcome's.
        is_welcomed = await self._wait_for_reply(environment, deadline, baseline_event_count=0)
        if not is_welcomed:
            await self._mark_timed_out(environment, "the workspace chat never answered its welcome")
            return

        await self._capture_preexisting_registrations(environment)

        sources = self.build_turn_sources(case)

        # Outer loop over the config's entries, inner loop over one entry's exchanges. A string
        # entry is one exchange; a goal entry keeps going until its client is satisfied or the
        # budget stops it.
        for entry_index, (entry, source) in enumerate(zip(case.prompts, sources, strict=True)):
            is_entry_done = await self._run_entry(case, entry, entry_index, source, environment, deadline)
            if not is_entry_done:
                return

        self._test_state = "finished"
        # The agent can keep working after it reports WAITING -- the workspace's own turn-end flow
        # runs then -- so pull once more before the transcript is written for the last time. Without
        # this the transcript ends at the final reply while the proxy keeps metering, which shows up
        # as the proxy accounting for requests the transcript has no messages for.
        await self._refresh_events(environment)
        await self._sync_trial_files(environment)
        logger.info("Finished {} entr(ies) in {} exchange(s)", len(sources), self._waits_done)

    def build_turn_sources(self, case: CaseConfig) -> list[TurnSource]:
        """The turn source for each of the case's prompts entries.

        The one seam between the conversation loop and the model calls that feed it, so the loop can
        be driven end to end against scripted sources without any network.
        """
        return resolve_turn_sources(case, self._decider_model, self._get_env("ANTHROPIC_API_KEY") or "")

    async def _run_entry(
        self,
        case: CaseConfig,
        entry: PromptEntry,
        entry_index: int,
        source: TurnSource,
        environment: BaseEnvironment,
        deadline: float,
    ) -> bool:
        """Drive one prompts entry to its outcome; False means the trial was marked timed out.

        The budget is enforced here rather than in the source, so no source can exceed it by
        construction: the loop simply stops asking once the entry has sent its allowance.
        """
        assert self._box_env is not None
        budget = entry_exchange_budget(entry)
        end: Done | None = None
        exchange_count = 0
        while exchange_count < budget:
            action = await self._run_exchange(case, entry_index, exchange_count, source, environment, deadline)
            match action:
                case None:
                    return False
                case Done():
                    end = action
                    break
                case Say():
                    exchange_count += 1
                case _ as unreachable:
                    assert_never(unreachable)
        # An entry the budget stopped never reported an ending of its own; what the ceiling means
        # depends on the source, which is the only thing that knows whether it had more to say.
        # Reason and detail come from the one `Done` either way, so they cannot disagree.
        if end is None:
            end = source.exhaustion_end
        self._entry_records.append(
            EntryRecord(
                index=entry_index,
                kind=source.kind,
                exchange_count=exchange_count,
                outcome=end.reason,
                detail=end.detail,
            )
        )
        await self._sync_trial_files(environment)
        is_final_entry = entry_index == len(case.prompts) - 1
        # Gated on the whole run's message count, not this entry's: a final goal entry can be
        # satisfied at exchange 0, and the workspace the earlier entries built is still worth
        # capturing. Only a conversation that never said anything has nothing to snapshot.
        is_snapshot_point = is_final_entry and self._waits_done > 0
        if is_snapshot_point and is_snapshot_wanted(self._snapshot_mode, SnapshotPoint.AFTER_FINAL_ENTRY):
            await minds_bridge.snapshot_workspace(
                environment, self._box_env, self._workspace_agent_id, "post_message_{}".format(self._waits_done)
            )
        return True

    async def _run_exchange(
        self,
        case: CaseConfig,
        entry_index: int,
        exchange_index: int,
        source: TurnSource,
        environment: BaseEnvironment,
        deadline: float,
    ) -> TurnAction | None:
        """One exchange: ask the source, and if it spoke, run the full wait/send/reply/sync sequence.
        None means the trial was marked timed out and the conversation must stop.

        The source is asked before the workspace is touched. Whether the entry is over is a property
        of the conversation the source already has, so an entry that ends here costs no round trip
        -- and, more to the point, no WAITING poll whose expiry would be recorded as a trial timeout
        for a conversation that was simply finished.
        """
        result_count_before = len(source.results)
        # The client's judgment is a blocking HTTP call of up to a couple of minutes, and harbor
        # runs every concurrent trial on one event loop, so it goes to a thread: run it inline and
        # every other trial's polling -- and its deadline -- stalls behind this one.
        action = await asyncio.to_thread(
            source.next_action, case, Transcript(events=tuple(_conversation_events(self._conversation)))
        )
        if isinstance(action, Done):
            self._record_decider_calls(source, result_count_before, entry_index, exchange_index, action, None)
            return action

        message_index = self._waits_done + 1
        failure_reason = await self._say(
            action.text, entry_index, exchange_index, message_index, environment, deadline
        )
        # `_waits_done` advances only once the message has reached the workspace, so it is what says
        # whether this call's message actually went out: a wait or a send that expired leaves the
        # audit event with no turn number, while a message that went out but drew no reply keeps one.
        is_message_sent = self._waits_done == message_index
        # Recorded before the trial can be marked timed out, because marking it writes the transcript
        # for the last time.
        self._record_decider_calls(
            source,
            result_count_before,
            entry_index,
            exchange_index,
            action,
            message_index if is_message_sent else None,
        )
        if failure_reason is not None:
            await self._mark_timed_out(environment, failure_reason)
            return None
        return action

    async def _say(
        self,
        text: str,
        entry_index: int,
        exchange_index: int,
        message_index: int,
        environment: BaseEnvironment,
        deadline: float,
    ) -> str | None:
        """Send one client message and collect the agent's reply; the reason to time the trial out,
        or None when the exchange completed.

        The reason is returned rather than acted on so the caller can finish recording the exchange
        before the trial is marked timed out, which is what writes the transcript for the last time.
        """
        assert self._box_env is not None
        is_ready = await minds_bridge.wait_for_chat_state(
            environment,
            self._box_env,
            self._workspace_agent_id,
            self._chat_agent_id,
            is_waiting_desired=True,
            deadline=deadline,
            poll_seconds=self._poll_seconds,
        )
        if not is_ready:
            return "agent never reached WAITING before message {}".format(message_index)

        # Refreshed after the wait, not before the source is asked: the agent can keep emitting
        # events after it reports WAITING, and the baseline below has to be taken on the freshest
        # view of the stream or that trailing work is read back as this message's reply.
        await self._refresh_events(environment)
        logger.info(
            "Sending entry {} exchange {} as message {}: {}",
            entry_index + 1,
            exchange_index + 1,
            message_index,
            text[:80],
        )
        # Anchor reply detection on the event count before the send, so an
        # injected user message can't be mistaken for the agent's reply.
        baseline_event_count = len(self._latest_events)
        is_sent = await minds_bridge.send_chat_message(
            environment,
            self._box_env,
            self._workspace_agent_id,
            self._chat_agent_id,
            text,
            deadline,
            self._poll_seconds,
        )
        if not is_sent:
            return "could not send message {}".format(message_index)
        self._conversation.append({"role": "user", "text": text})
        self._waits_done = message_index
        await self._sync_trial_files(environment)

        is_replied = await self._wait_for_reply(environment, deadline, baseline_event_count)
        if not is_replied:
            return "no reply to message {}".format(message_index)
        reply_texts = _new_agent_reply_texts(self._latest_events, baseline_event_count)
        # Record each message's length before merging, so the per-message
        # metric sees the agent's real (short) messages, not the merged wall.
        self._agent_message_word_counts.extend(len(reply_text.split()) for reply_text in reply_texts)
        self._conversation.append({"role": "agent", "text": "\n\n".join(reply_texts)})
        await self._sync_trial_files(environment)

        if is_snapshot_wanted(self._snapshot_mode, SnapshotPoint.AFTER_EXCHANGE):
            await minds_bridge.snapshot_workspace(
                environment, self._box_env, self._workspace_agent_id, "post_message_{}".format(message_index)
            )
        return None

    def _record_decider_calls(
        self,
        source: TurnSource,
        result_count_before: int,
        entry_index: int,
        exchange_index: int,
        action: TurnAction,
        message_index: int | None,
    ) -> None:
        """Record every decider-model call the source just made, so an LLM-driven message -- or a
        decision to stop without one -- can be traced back to the exchange that produced it.
        ``message_index`` is the 1-based index of the message the call produced, or None when it
        produced none (it ended the entry, or its message never reached the workspace)."""
        for result in source.results[result_count_before:]:
            self._decider_results.append(result)
            self._decider_turns.append(
                DeciderTurn(
                    # A call that sent nothing carries no message number: stamping it with the next one
                    # would hand that number to two calls, and (entry_index, exchange) locates it.
                    turn=message_index,
                    entry_index=entry_index,
                    exchange=exchange_index,
                    entry_kind=source.kind,
                    model=result.model or self._decider_model,
                    is_fallback=result.is_fallback,
                    detail=action.detail if isinstance(action, Done) else "",
                )
            )

    async def _probe_reverse_tunnel(self, environment: BaseEnvironment) -> None:
        """Check that a box-local port is reachable from inside the workspace.

        An in-box LLM proxy depends on this and on nothing else: the workspace would reach it at a
        loopback address, so LLM traffic never leaves the sandbox. Reported rather than enforced --
        this is a measurement, not a gate.
        """
        assert self._box_env is not None
        ssh_info = await minds_bridge.fetch_agent_ssh_info(environment, self._box_env, self._workspace_agent_id)
        if ssh_info is None:
            logger.error("Reverse-tunnel probe: no SSH endpoint for the workspace in `mngr list`")
            return
        logger.info("Reverse-tunnel probe: workspace ssh {}@{}", ssh_info["user"], ssh_info["host"])
        await minds_bridge.start_reverse_tunnel(
            environment,
            self._box_env,
            self._workspace_agent_id,
            ssh_info,
            PROXY_PORT,
            PROXY_PROBE_HOLD_SECONDS,
            is_probe_token_served=True,
        )
        tunnel_log = "{}/{}".format(minds_bridge.BOX_LOGS_DIR, minds_bridge.TUNNEL_LOG_FILENAME)
        deadline = time.time() + PROXY_PROBE_READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            if "TUNNEL_READY" in await minds_bridge.read_box_file(environment, self._box_env, tunnel_log):
                break
            await asyncio.sleep(self._poll_seconds)
        else:
            logger.error(
                "Reverse-tunnel probe: the tunnel never came up:\n{}",
                await minds_bridge.read_box_file(environment, self._box_env, tunnel_log),
            )
            return
        observed = await minds_bridge.fetch_from_workspace(
            environment, self._box_env, self._workspace_agent_id, "http://127.0.0.1:{}/".format(PROXY_PORT)
        )
        is_reachable = PROXY_PROBE_TOKEN in observed
        logger.info(
            "Reverse-tunnel probe: workspace fetched {!r} -- box-local port {} is {}",
            observed[:120],
            PROXY_PORT,
            "reachable" if is_reachable else "NOT reachable",
        )

    @property
    def _proxy_base_url(self) -> str:
        """The loopback address the workspace reaches the box's proxy at, once one is running."""
        return "http://127.0.0.1:{}".format(PROXY_PORT) if self._is_proxy_enabled else ""

    async def _start_proxy(self, environment: BaseEnvironment, case: CaseConfig) -> bool:
        """Bring up the in-box proxy and the tunnel that exposes it inside the workspace.

        Ordered before sign-in because the workspace is handed the proxy's address as its base URL:
        a workspace signed in against a proxy that is not listening cannot take a turn.
        """
        assert self._box_env is not None
        upstream_key = self._get_env("ANTHROPIC_API_KEY") or ""
        if not upstream_key:
            logger.error("No ANTHROPIC_API_KEY for the proxy to call the model with")
            return False
        self._proxy_key = "sk-eval-{}".format(uuid.uuid4().hex)
        await minds_bridge.start_proxy(
            environment,
            self._box_env,
            proxy_config.render_proxy_config(),
            upstream_key,
            self._proxy_key,
            PROXY_PORT,
        )
        is_up = await minds_bridge.wait_for_proxy(
            environment,
            self._box_env,
            PROXY_PORT,
            time.time() + PROXY_BOOT_TIMEOUT_SECONDS,
            self._poll_seconds,
        )
        if not is_up:
            logger.error(
                "The proxy never answered:\n{}",
                await minds_bridge.read_box_file(
                    environment,
                    self._box_env,
                    "{}/{}".format(minds_bridge.BOX_LOGS_DIR, minds_bridge.PROXY_LOG_FILENAME),
                ),
            )
            return False
        ssh_info = await minds_bridge.fetch_agent_ssh_info(environment, self._box_env, self._workspace_agent_id)
        if ssh_info is None:
            logger.error("No SSH endpoint for the workspace, so the proxy cannot be exposed to it")
            return False
        await minds_bridge.start_reverse_tunnel(
            environment,
            self._box_env,
            self._workspace_agent_id,
            ssh_info,
            PROXY_PORT,
            # Outlive the case, so the tunnel never closes under a running conversation, but stay
            # bounded so a driver that dies without tearing it down cannot leave it up.
            case.timeout_seconds + PROXY_TUNNEL_GRACE_SECONDS,
            is_probe_token_served=False,
        )
        tunnel_deadline = time.time() + PROXY_TUNNEL_READY_TIMEOUT_SECONDS
        tunnel_log = "{}/{}".format(minds_bridge.BOX_LOGS_DIR, minds_bridge.TUNNEL_LOG_FILENAME)
        while time.time() < tunnel_deadline:
            if "TUNNEL_READY" in await minds_bridge.read_box_file(environment, self._box_env, tunnel_log):
                logger.info("The proxy is up and reachable inside the workspace on port {}", PROXY_PORT)
                return True
            await asyncio.sleep(self._poll_seconds)
        logger.error(
            "The tunnel to the workspace never came up:\n{}",
            await minds_bridge.read_box_file(environment, self._box_env, tunnel_log),
        )
        return False

    async def _collect_proxy_usage(self, environment: BaseEnvironment) -> None:
        """Pull the proxy's per-request log into the trial artifacts.

        This is the metering record: every model call the workspace made, including any the agent
        delegated to a subagent or a worker, since all of them share the workspace's credential.
        """
        assert self._box_env is not None
        contents = await minds_bridge.read_box_file(environment, self._box_env, minds_bridge.BOX_PROXY_USAGE_LOG_PATH)
        if not contents:
            logger.warning("The proxy recorded no requests")
            return
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / PROXY_USAGE_FILENAME).write_text(contents + "\n")
        self._proxy_usage_records = usage_accounting.parse_proxy_usage_log(contents)

    async def _authenticate_workspace(
        self, environment: BaseEnvironment, deadline: float
    ) -> minds_bridge.WorkspaceSignIn:
        """Sign the workspace in after create, the way a user does; reports the account it minted.

        A workspace boots unauthenticated -- the product's create path supplies no AI credentials --
        so without this there is no account for a chat to bind to, and the workspace refuses to
        create one. Doing it through the sign-in endpoint rather than the create-time host env is
        what keeps the graded agent in production's shared config-dir regime.
        """
        assert self._box_env is not None
        # Behind the proxy the workspace gets the trial's own key, never the upstream one: it is
        # scoped to this trial, and the credential the agent can see buys nothing anywhere else.
        api_key = self._proxy_key or self._get_env("ANTHROPIC_API_KEY") or ""
        if not api_key:
            logger.error("No ANTHROPIC_API_KEY to sign the workspace in with")
            return minds_bridge.NOT_SIGNED_IN
        is_endpoint_ready = await minds_bridge.wait_for_auth_endpoint(
            environment, self._box_env, self._workspace_agent_id, deadline, self._poll_seconds
        )
        if not is_endpoint_ready:
            logger.error("The workspace's claude-auth endpoint never came up")
            return minds_bridge.NOT_SIGNED_IN
        logger.info("Signing the workspace in through the claude-auth endpoint")
        return await minds_bridge.authenticate_workspace(
            environment,
            self._box_env,
            self._workspace_agent_id,
            api_key,
            self._proxy_base_url or self._get_env("ANTHROPIC_BASE_URL") or "",
        )

    async def _wait_for_reply(self, environment: BaseEnvironment, deadline: float, baseline_event_count: int) -> bool:
        """Wait until the agent has produced a reply to the just-sent message AND is WAITING again.
        Polling on the reply itself (rather than a leave-WAITING edge) cannot miss a fast
        WAITING->BUSY->WAITING transition between polls. Anchors on the event count captured before
        the send, so framework-injected user messages cannot be mistaken for the agent's reply.
        Gives up early (returning False) once the bridge fails too many polls in a row, rather than
        silently burning the whole case budget on a wedged bridge."""
        assert self._box_env is not None
        consecutive_failures = 0
        while time.time() < deadline:
            is_refreshed = await self._refresh_events(environment)
            if not is_refreshed:
                consecutive_failures += 1
                if consecutive_failures % _FETCH_FAILURE_LOG_INTERVAL == 0:
                    logger.warning("Bridge event fetch has failed {} times in a row", consecutive_failures)
                if consecutive_failures >= _MAX_CONSECUTIVE_FETCH_FAILURES:
                    logger.error("Giving up on the reply after {} consecutive bridge failures", consecutive_failures)
                    return False
                await asyncio.sleep(self._poll_seconds)
                continue
            consecutive_failures = 0
            if _new_agent_reply_texts(self._latest_events, baseline_event_count):
                state = await minds_bridge.fetch_chat_agent_state(
                    environment, self._box_env, self._workspace_agent_id, self._chat_agent_id
                )
                if state == "WAITING":
                    # One more pull before handing back. The state check races the refresh above, so
                    # messages the agent emitted between the two would otherwise never be fetched --
                    # and once the workspace is destroyed they are gone. Cheap: a no-op when the
                    # event total has not moved.
                    await self._refresh_events(environment)
                    return True
            await asyncio.sleep(self._poll_seconds)
        return False

    async def _refresh_events(self, environment: BaseEnvironment) -> bool:
        """Pull any new chat events into _latest_events incrementally; returns whether the refresh
        succeeded (False on a transient bridge failure, so callers can track consecutive failures)."""
        assert self._box_env is not None
        total = await minds_bridge.fetch_event_total(
            environment, self._box_env, self._workspace_agent_id, self._chat_agent_id
        )
        if total is None:
            return False
        if total <= self._seen_event_count:
            return True
        new_events = await minds_bridge.fetch_events_window(
            environment,
            self._box_env,
            self._workspace_agent_id,
            self._chat_agent_id,
            self._seen_event_count,
            total - self._seen_event_count,
        )
        if new_events is None:
            return False
        self._latest_events.extend(new_events)
        self._seen_event_count += len(new_events)
        return True

    async def _prepare_workspace_clone(self, case: CaseConfig, environment: BaseEnvironment) -> None:
        """Clone the workspace template at its pinned SHA in the box and overwrite its vendored mngr
        with the box's /work/mngr, leaving a committed clone the workspace can be created from."""
        assert self._box_env is not None
        # Paths and config-derived values travel unquoted and are shell-quoted where each command
        # builder interpolates them. The case id and the dwt repo/branch/sha come from an
        # author-controlled eval config, and a quote or space in one must not break out of a command.
        clone_dir = _case_clone_dir(case.case_id)
        commit_message = "eval case {}".format(case.case_id)
        logger.info(
            "Preparing the workspace template clone ({}@{}, pinned from {})",
            case.dwt_repo,
            case.dwt_sha[:12],
            case.dwt_branch,
        )
        await minds_bridge.check_run_in_box(
            environment,
            build_eval_base_clone_command(
                dwt_repo=case.dwt_repo,
                dwt_branch=case.dwt_branch,
                dwt_sha=case.dwt_sha,
                eval_base_dir=_EVAL_BASE_DIR,
            ),
            self._box_env,
            600,
        )
        await minds_bridge.check_run_in_box(
            environment,
            build_case_clone_command(clone_dir),
            self._box_env,
            300,
        )
        await minds_bridge.check_run_in_box(
            environment,
            build_vendor_mngr_command(clone_dir),
            self._box_env,
            600,
        )
        await minds_bridge.check_run_in_box(
            environment,
            build_eval_case_commit_command(clone_dir, commit_message),
            self._box_env,
            300,
        )
        # Record where the agent's own history starts, and what the base was built from. The
        # evidence phase bundles only <base>..HEAD, so the captured deliverable is the agent's
        # commits rather than the whole template. The base commit is deterministic (fixed identity
        # and dates over a tree that is a function of the dwt tip and the mngr SHA), so recording
        # both shas is what keeps that bundle reproducible: preparing the clone again from the same
        # inputs yields this exact base sha, and the bundle unbundles only onto it.
        base_result = await minds_bridge.check_run_in_box(
            environment,
            build_clone_probe_command(clone_dir, _EVAL_BASE_DIR),
            self._box_env,
            120,
        )
        sections = evidence_collection.split_sections(base_result.stdout or "")
        self._dwt_tip_sha = sections.get("dwt_tip_sha", "").strip()
        base_output = sections.get("base_sha", "").strip()
        self._clone_base_sha = base_output.splitlines()[-1].strip() if base_output else ""

    async def _capture_preexisting_registrations(self, environment: BaseEnvironment) -> None:
        """Snapshot what the workspace already serves, before the first turn can change it.

        This is the last moment the workspace is purely the template's doing: it has finished its
        own setup -- booted, signed in, chat created and welcomed -- and the eval has not sent a
        turn yet. Recording it here is what lets the evidence phase attribute a registry row to the
        agent rather than guessing from names.

        A probe that comes back failed, or a registry that is not there yet, leaves the set unknown,
        which the collector records as unmeasured -- never as "the workspace served nothing", which
        would credit the agent with every app the template booted. A transport-level failure
        propagates like every other pre-turn step's does.
        """
        assert self._box_env is not None
        is_success, output = await minds_bridge.run_in_workspace(
            environment,
            self._box_env,
            self._workspace_agent_id,
            evidence_collection.workspace_state_command(),
            evidence_collection.PROBE_TIMEOUT_SECONDS,
        )
        self._preexisting_registrations = evidence_collection.parse_registry_snapshot(output) if is_success else None
        if self._preexisting_registrations is None:
            cause = (
                "the probe ran but found no readable app registry"
                if is_success
                else "the bridged exec failed: {}".format(output.strip()[:300] or "no output")
            )
            logger.warning(
                "Could not snapshot the workspace app registry before turn 1 ({}); the delivered apps "
                "cannot be told from the ones the workspace booted with, so those checks will be "
                "unmeasured",
                cause,
            )
        else:
            logger.info(
                "The workspace already serves {} app(s) before turn 1: {}",
                len(self._preexisting_registrations),
                ", ".join(sorted(self._preexisting_registrations)) or "none",
            )

    async def _mark_timed_out(self, environment: BaseEnvironment, reason: str) -> None:
        logger.warning("Marking the trial timed_out: {}", reason)
        self._test_state = "timed_out"
        await self._sync_trial_files(environment)

    def _state_payload(self) -> dict[str, Any]:
        entry_count = len(self._case.prompts) if self._case is not None else 0
        return {
            "eval_name": self.logs_dir.parent.name,
            "case_name": self._case.case_id if self._case is not None else "",
            "mngr_sha": self._mngr_sha,
            "dwt_sha": self._case.dwt_sha if self._case is not None else "",
            "waits_done": self._waits_done,
            # "num_turns" is the ported state.json schema key (the old harness's readers consume
            # it). It counts CONFIGURED ENTRIES, which a goal entry can outrun -- "waits_done" is
            # the messages actually sent, and the per-entry records below account for those
            # messages entry by entry. An entry earns its record only once it has stopped, so a
            # timed-out trial's records end at the entry it died in and account for fewer messages
            # than "waits_done"; only a finished trial's two views are expected to agree.
            "num_turns": entry_count,
            "entries": [record.model_dump(mode="json") for record in self._entry_records],
            "test_state": self._test_state,
            "timed_out": self._test_state == "timed_out",
            "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - self._started_at, 1),
            "timeout_seconds": self._case.timeout_seconds if self._case is not None else 0.0,
        }

    async def _sync_trial_files(self, environment: BaseEnvironment) -> None:
        """Write the hand-built trajectory and the state to the host logs dir and mirror them into the
        box's /logs/agent/, where the task's declared artifacts pick them up for the verifier."""
        assert self._case is not None, "the case is parsed before the conversation starts"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        file_contents = {STATE_FILENAME: json.dumps(self._state_payload(), indent=2)}
        hand_built = self._hand_built_trajectory(self._case)
        if hand_built is not None:
            file_contents[TRAJECTORY_FILENAME] = _trajectory_json(hand_built)
        await environment.exec("mkdir -p {}".format(minds_bridge.BOX_LOGS_DIR), timeout_sec=60)
        for filename, content in file_contents.items():
            local_path = self.logs_dir / filename
            local_path.write_text(content)
            await environment.upload_file(local_path, _box_trial_file_path(filename))
        if TRAJECTORY_FILENAME in file_contents:
            self._box_trajectory_json = file_contents[TRAJECTORY_FILENAME]

    def _resolve_workspace_usage(self) -> usage_accounting.ResolvedWorkspaceUsage:
        """The one resolution of the workspace agent's spend that every usage writer in this driver
        reads from; the choice between the two accounts is ``resolve_workspace_usage``'s."""
        # The launch count is the capture's scan of the captured transcripts when there was one; before
        # that, or without it, the polled feed's own heuristic stands.
        worker_launch_count = (
            len(self._worker_captures) + len(self._worker_capture_overflow)
            if self._transcript_capture.stream.is_captured
            else None
        )
        return usage_accounting.resolve_workspace_usage(
            self._latest_events,
            self._proxy_usage_records,
            list(self._worker_stream_records_by_name.values()),
            worker_launch_count,
            _settled_worker_count(self._worker_captures, self._worker_stream_records_by_name),
        )

    def _populate_context_metadata(self, context: AgentContext, trajectory_source: TrajectorySource) -> None:
        turn_word_counts = _words_per_agent_turn(self._conversation)
        message_word_counts = self._agent_message_word_counts
        decider_results = self._decider_results
        # Harbor's token/cost fields describe the agent under test, so they carry the workspace
        # agent's consumption. The decider is the harness's own spend and goes to metadata; putting
        # it here would report the simulated user's tokens as the agent's.
        resolved_usage = self._resolve_workspace_usage()
        workspace_usage = resolved_usage.reported
        decider_usage = usage_accounting.summarize_decider_usage(decider_results, self._decider_model)
        if workspace_usage.message_count:
            context.n_input_tokens = workspace_usage.n_input_tokens
            context.n_cache_tokens = workspace_usage.n_cache_tokens
            context.n_output_tokens = workspace_usage.tokens.output
            context.cost_usd = workspace_usage.cost_usd
        if workspace_usage.unpriced_models:
            logger.warning(
                "No pricing for {}; the trial's cost is reported as unknown rather than partial",
                ", ".join(workspace_usage.unpriced_models),
            )
        if not workspace_usage.is_cost_complete:
            logger.warning(
                "This trial delegated ({} subagent call(s); {} of {} worker launch(es) captured), so its reported "
                "cost is a lower bound: subagent turns and uncaptured workers are served on streams this total "
                "does not include",
                workspace_usage.delegated_call_count,
                workspace_usage.worker_captured_count,
                workspace_usage.worker_launch_count,
            )
        if workspace_usage.message_count:
            if not workspace_usage.is_speed_observed:
                logger.warning(
                    "Speed tier unobserved, so every request is priced at the standard rate. Minds runs fast mode "
                    "by default and fast mode bills at twice that, so treat this cost as a floor; run with "
                    "--ak proxy=true to price it exactly"
                )
            elif workspace_usage.fast_message_count:
                logger.info(
                    "{} of {} request(s) were served in fast mode and are priced at the fast-mode rate",
                    workspace_usage.fast_message_count,
                    workspace_usage.message_count,
                )
            else:
                logger.debug("Every request was served at standard speed")
        context.metadata = {
            "case_id": self._case.case_id if self._case is not None else "",
            # Messages sent versus entries configured: a goal entry can send several, so these
            # are two separate counts.
            "turns_completed": self._waits_done,
            "turn_count": len(self._case.prompts) if self._case is not None else 0,
            "entries": [record.model_dump(mode="json") for record in self._entry_records],
            "test_state": self._test_state,
            "timed_out": self._test_state == "timed_out",
            # Per merged agent turn vs. per individual agent message, both observability only: the
            # verifier re-derives its own counts from trajectory.json.
            "average_words_per_turn": round(sum(turn_word_counts) / len(turn_word_counts), 1)
            if turn_word_counts
            else 0.0,
            "average_words_per_message": round(sum(message_word_counts) / len(message_word_counts), 1)
            if message_word_counts
            else 0.0,
            "decider_model": self._decider_model,
            "modal_user_id": self._user_id,
            "mngr_sha": self._mngr_sha,
            # Both pinned inputs travel with the trial record, so a captured
            # trial says which mngr and which workspace template produced it.
            "dwt_sha": self._case.dwt_sha if self._case is not None else "",
            "workspace_usage": usage_accounting.workspace_usage_metadata(workspace_usage),
            # Both sources, so the two can be reconciled after the fact: they agree exactly when the
            # agent delegates nothing, and differ by the delegated spend when it does.
            "usage_source": _usage_source(resolved_usage).value,
            "transcript_usage": usage_accounting.workspace_usage_metadata(resolved_usage.transcript),
            # Which shape trajectory.json has, with the capture's own account of why, so a hand-built
            # document on a post-ATIF workspace is diagnosable rather than mysterious.
            "trajectory_source": trajectory_source.value,
            "transcript_capture": _transcript_capture_metadata(self._transcript_capture),
            # One entry per background worker launched in the trial (the chat agent's and, in turn,
            # theirs), and the launches the caps left uncaptured, so delegated work is accounted for
            # by name.
            "workers": [
                _worker_capture_metadata(capture, self._worker_stream_records_by_name.get(capture.launch.name))
                for capture in self._worker_captures
            ],
            "worker_capture_overflow": list(self._worker_capture_overflow),
            "decider_usage": usage_accounting.decider_usage_metadata(decider_usage),
            # The UI-flow verification agent is harness spend just like the decider: it measures
            # what the eval costs to run, never what the agent under test consumed.
            "verifier_agent_usage": usage_accounting.verifier_usage_metadata(self._verifier_usage)
            if self._verifier_usage is not None
            else {},
            # Empty when no evidence phase ran (no workspace, or collection failed outright);
            # the grade reads the bundle itself, this is for scanning runs at a glance.
            "verification": self._verification_metadata,
        }
        self._write_usage(workspace_usage, decider_usage)

    def _write_usage(
        self, workspace_usage: usage_accounting.TrialUsage, decider_usage: usage_accounting.DeciderUsage
    ) -> None:
        """Write the usage breakdown as its own trial artifact, so cost and cache behaviour can be
        read (and diffed across runs) without parsing the whole transcript."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspace_agent": usage_accounting.workspace_usage_metadata(workspace_usage),
            "decider": usage_accounting.decider_usage_metadata(decider_usage),
        }
        (self.logs_dir / USAGE_FILENAME).write_text(json.dumps(payload, indent=2))

    def _trajectory_provenance(self, case: CaseConfig, usage_source: UsageSource) -> TrajectoryProvenance:
        return TrajectoryProvenance(
            driver_name=self.name(),
            driver_version=self.version() or "unknown",
            decider_model=self._decider_model,
            decider_turns=tuple(self._decider_turns),
            harbor_session_id=self.session_id,
            case_id=case.case_id,
            usage_source=usage_source,
        )

    def _workspace_trajectory_or_none(
        self, document_path: Path, provenance: TrajectoryProvenance, workspace_usage: usage_accounting.TrialUsage
    ) -> Trajectory | None:
        """The captured workspace document with the eval's reconciliations and the captured workers
        embedded, or None when it cannot be read or is not valid ATIF -- in which case the hand-built
        shape is written instead."""
        try:
            document_json = document_path.read_text()
        except OSError as exc:
            logger.warning("Could not read the captured trajectory document; writing the hand-built one: {}", exc)
            return None
        try:
            return trajectory_building.build_workspace_trajectory(
                document_json, provenance, workspace_usage, _embedded_workers(self._worker_captures, self.logs_dir)
            )
        except TrajectoryDocumentError as exc:
            logger.warning("The captured trajectory document is unusable; writing the hand-built one: {}", exc)
            return None

    def _hand_built_trajectory(self, case: CaseConfig) -> Trajectory | None:
        """The driver's own per-turn summary of the conversation so far, or None before any exchange."""
        resolved_usage = self._resolve_workspace_usage()
        return trajectory_building.build_hand_built_trajectory(
            conversation=self._conversation,
            provenance=self._trajectory_provenance(case, _usage_source(resolved_usage)),
            workspace_usage=resolved_usage.reported,
            timestamp=_utc_now_iso(),
        )

    async def _publish_trajectory(self, case: CaseConfig, environment: BaseEnvironment) -> TrajectorySource:
        """Write the final trajectory.json, host-side and into the box: the workspace's own document
        when the evidence phase captured one, else the hand-built summary. Returns which shape the
        box holds.

        The box copy is the one that matters: harbor collects the declared artifacts from there for
        the verifier and downloads /logs/agent over the host logs dir afterwards. So when the upload
        fails, the last per-turn copy is what grading sees, and the host copy is put back to exactly
        that rather than left describing a document the verifier never got.
        """
        resolved_usage = self._resolve_workspace_usage()
        provenance = self._trajectory_provenance(case, _usage_source(resolved_usage))
        document_path = self._transcript_capture.document.host_path
        workspace_trajectory = (
            self._workspace_trajectory_or_none(document_path, provenance, resolved_usage.reported)
            if document_path is not None
            else None
        )
        if workspace_trajectory is not None:
            written_trajectory: Trajectory | None = workspace_trajectory
            source = TrajectorySource.WORKSPACE
        else:
            written_trajectory = self._hand_built_trajectory(case)
            source = TrajectorySource.HAND_BUILT
        if written_trajectory is None:
            # A trial that died before any exchange has no conversation to describe.
            return TrajectorySource.NONE
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        host_path = self.logs_dir / TRAJECTORY_FILENAME
        trajectory_json = _trajectory_json(written_trajectory)
        host_path.write_text(trajectory_json)
        try:
            await environment.upload_file(host_path, _box_trial_file_path(TRAJECTORY_FILENAME))
        except (OSError, RuntimeError, ModalError) as exc:
            logger.warning(
                "Could not publish the final trajectory into the box; grading on the per-turn copy: {}", exc
            )
            if self._box_trajectory_json is None:
                host_path.unlink()
                return TrajectorySource.NONE
            host_path.write_text(self._box_trajectory_json)
            return TrajectorySource.HAND_BUILT
        self._box_trajectory_json = trajectory_json
        return source
