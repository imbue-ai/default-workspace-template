"""MindsPersonaDriver: a host-side harbor agent that owns the whole persona conversation loop.

The harbor environment is the Minds box; the driver starts the backend with per-trial env, creates
one nested Modal workspace through the production Minds API, drives the scripted multi-turn
conversation against the workspace's system_interface (bridged through ``mngr exec``), snapshots the
workspace after turns, and keeps the raw transcript, the clean ``conversation.jsonl`` (what the
verifier grades), and ``state.json`` current in the box so even a timed-out trial leaves a gradeable
partial record.
"""

import asyncio
import json
import re
import shlex
import time
import uuid
from abc import ABC
from abc import abstractmethod
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
from harbor.models.trajectories import Agent as TrajectoryAgent
from harbor.models.trajectories import FinalMetrics
from harbor.models.trajectories import Step
from harbor.models.trajectories import Trajectory
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import decider
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import proxy_config
from imbue.minds_evals import usage as usage_accounting
from imbue.minds_evals import verification
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.errors import AgentKwargError
from imbue.minds_evals.errors import InstructionParseError

TRANSCRIPT_FILENAME: Final[str] = "full_transcript.jsonl"
# The eval's own user turns paired with the agent's replies, filtered free of
# framework noise (the /welcome skill body, tool events, injected messages). The
# judge scores this rather than the raw stream, and the decider reads it as the
# conversation so far.
CONVERSATION_FILENAME: Final[str] = "conversation.jsonl"
STATE_FILENAME: Final[str] = "state.json"
# Token and cost accounting, written host-side beside the trajectory (the verifier does not grade
# it, so unlike the transcript files it is not mirrored into the box).
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


@pure
def _is_snapshot_wanted(snapshot_mode: SnapshotMode, is_final_turn: bool) -> bool:
    match snapshot_mode:
        case SnapshotMode.PER_TURN:
            return True
        case SnapshotMode.FINAL:
            return is_final_turn
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


class TurnSource(MutableModel, ABC):
    """Produces the simulated user's message for one conversation turn."""

    @abstractmethod
    def next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        """Return the simulated user's next message for the current turn."""


class LiteralTurnSource(TurnSource):
    """Deterministic: returns the config's literal prompt string verbatim."""

    prompt: str = Field(frozen=True, description="The literal message sent for this turn")

    def next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        return self.prompt


class PersonaLLMTurnSource(TurnSource):
    """Non-deterministic: renders the persona plus the transcript so far into the role-play prompt,
    calls the decider model, and falls back to the literal "Sounds good." on any error."""

    model: str = Field(frozen=True, description="The decider model")
    api_key: SecretStr = Field(frozen=True, description="Anthropic API key for the decider")
    results: list[DeciderResult] = Field(default_factory=list, description="One entry per decider call")

    def next_message(self, case: CaseConfig, transcript: Transcript) -> str:
        result = decider.decide_next_message(
            persona=case.persona,
            transcript=transcript,
            model=self.model,
            api_key=self.api_key.get_secret_value(),
        )
        self.results.append(result)
        return result.message


@pure
def resolve_turn_sources(case: CaseConfig, decider_model: str, api_key: str) -> list[TurnSource]:
    """Map each prompts entry to its turn source: a literal string -> LiteralTurnSource, the
    DECIDE_FROM_PERSONA sentinel -> a shared PersonaLLMTurnSource (shared so decider usage
    accumulates in one place)."""
    persona_source = PersonaLLMTurnSource(model=decider_model, api_key=SecretStr(api_key))
    return [
        persona_source if prompt == DECIDE_SENTINEL else LiteralTurnSource(prompt=prompt) for prompt in case.prompts
    ]


@pure
def _new_agent_reply_texts(events: list[dict[str, Any]], baseline_event_count: int) -> list[str]:
    """The non-empty agent reply texts at or after ``baseline_event_count`` (the event count captured
    just before the turn was sent). Anchoring on the send-time index -- rather than "after the last
    user_message" -- avoids being fooled by framework-injected user messages (the /welcome skill
    body, queued prompts, is_meta events) that can land after the agent's reply."""
    return [
        (event.get("text") or "").strip()
        for event in events[baseline_event_count:]
        if event.get("type") == "assistant_message" and (event.get("text") or "").strip()
    ]


@pure
def _words_per_agent_turn(conversation: list[dict[str, str]]) -> list[int]:
    """Word count of each agent turn -- one entry per client turn, over the turn's merged reply
    (several agent messages joined). Contrast ``average_words_per_message``, which counts each agent
    message on its own."""
    return [len(entry["text"].split()) for entry in conversation if entry["role"] == "agent" and entry["text"]]


@pure
def _conversation_events(conversation: list[dict[str, str]]) -> list[dict[str, str]]:
    """The clean conversation rendered back into the workspace event schema (user_message.content /
    assistant_message.text), for the decider's transcript and the judged conversation.jsonl."""
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
def build_eval_case_commit_command(quoted_clone_dir: str, quoted_commit_message: str) -> str:
    """The eval-case commit, made reproducibly: fixed identity and fixed author/committer dates."""
    return (
        "cd {clone} && git add -A && "
        "GIT_AUTHOR_DATE={date} GIT_COMMITTER_DATE={date} "
        "git -c user.email={email} -c user.name={name} commit -q -m {message}"
    ).format(
        clone=quoted_clone_dir,
        date=shlex.quote(EVAL_CASE_COMMIT_DATE),
        email=shlex.quote(EVAL_CASE_COMMIT_EMAIL),
        name=shlex.quote(EVAL_CASE_COMMIT_NAME),
        message=quoted_commit_message,
    )


@pure
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
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
        self._decider_events: list[dict[str, Any]] = []
        self._persona_source: PersonaLLMTurnSource | None = None
        self._case: CaseConfig | None = None
        self._started_at: float = 0.0
        self._waits_done: int = 0
        self._test_state: str = "ongoing"
        # HEAD of the per-case dwt clone the workspace was created from: the base of the
        # incremental git bundle the evidence phase captures, so the recorded deliverable is only
        # the agent's own commits.
        self._clone_base_sha: str = ""
        # The dwt tip the base clone was made from, recorded so a replay can regenerate that base
        # and check it reproduces _clone_base_sha before unbundling the agent's commits onto it.
        self._dwt_tip_sha: str = ""
        self._verification_metadata: dict[str, Any] = {}

    @staticmethod
    def name() -> str:
        return "minds-persona-driver"

    def version(self) -> str | None:
        return "0.1.0"

    @property
    def _decider_model(self) -> str:
        return self._parsed_model_name or decider.DEFAULT_DECIDER_MODEL

    async def setup(self, environment: BaseEnvironment) -> None:
        # Create the evidence directory before anything else can fail. harbor records a missing
        # declared artifact path as a failed entry, and `harbor trial regrade` refuses any trial
        # that has one -- an EMPTY directory is tolerated, an absent one is not. Without this, a
        # trial that dies before the collection phase would be permanently non-regradable.
        await verification.ensure_evidence_dir(environment)
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
            self._populate_context_metadata(context)
            # Written here rather than in populate_context_post_run: harbor only
            # calls that hook when the agent context is still empty, and this
            # driver always populates the context above.
            self._write_trajectory()
            await self._teardown(environment)

    async def _collect_verification_evidence(self, environment: BaseEnvironment) -> None:
        """Capture what the delivered workspace actually is, while it still exists.

        The cheap registry/service/inventory capture runs for every trial that got as far as a
        workspace; the expectations-driven probes only run when the conversation finished, since an
        unfinished trial's structural gates already zero its reward. Any failure here is swallowed:
        evidence is best-effort and must never discard an already-completed trial or block the
        teardown that stops the nested sandboxes from leaking.
        """
        if self._box_env is None or self._case is None or not self._workspace_agent_id:
            return
        collector = verification.EvidenceCollector(
            environment=environment,
            box_env=self._box_env,
            workspace_agent_id=self._workspace_agent_id,
            case=self._case,
            clone_base_sha=self._clone_base_sha,
            dwt_tip_sha=self._dwt_tip_sha,
            host_logs_dir=self.logs_dir,
            # Monotonic, unlike the conversation's own deadline: a clock step during a ten-minute
            # collection phase would otherwise truncate or extend it.
            deadline=time.monotonic() + self._case.verification_timeout_seconds,
        )
        logger.info("Collecting outcome-verification evidence from the workspace")
        try:
            manifest = await collector.collect(
                is_expectations_collection_wanted=self._test_state == "finished",
            )
        except Exception as exc:
            logger.opt(exception=exc).warning("Evidence collection failed; grading on what it managed to write")
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
            dwt_repo="/work/clones/{}".format(case.case_id),
            dwt_branch="",
            host_name=workspace_host_name,
        )
        logger.info("Creating the workspace for case {}", case.case_id)
        self._workspace_agent_id = await minds_bridge.create_workspace_and_wait(
            environment, self._box_env, self._api_port, payload, deadline, self._poll_seconds
        )
        logger.info("Workspace is up (agent {})", self._workspace_agent_id)

        chat_agent_id = await minds_bridge.fetch_chat_agent_id(
            environment, self._box_env, self._workspace_agent_id, workspace_host_name, deadline, self._poll_seconds
        )
        if chat_agent_id is None:
            await self._mark_timed_out(environment, "could not resolve the workspace chat agent id")
            return
        self._chat_agent_id = chat_agent_id

        if self._is_proxy_probe_enabled:
            await self._probe_reverse_tunnel(environment)
        if self._is_proxy_enabled:
            is_proxy_up = await self._start_proxy(environment, case)
            if not is_proxy_up:
                await self._mark_timed_out(environment, "the in-box LLM proxy did not come up")
                return

        is_authenticated = await self._authenticate_workspace(environment, deadline)
        if not is_authenticated:
            await self._mark_timed_out(environment, "could not authenticate the workspace")
            return

        sources = resolve_turn_sources(case, self._decider_model, self._get_env("ANTHROPIC_API_KEY") or "")
        self._persona_source = next((source for source in sources if isinstance(source, PersonaLLMTurnSource)), None)

        for turn, source in enumerate(sources, start=1):
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
                await self._mark_timed_out(environment, "agent never reached WAITING for turn {}".format(turn))
                return

            await self._refresh_events(environment)
            message = source.next_message(case, Transcript(events=tuple(_conversation_events(self._conversation))))
            if isinstance(source, PersonaLLMTurnSource) and source.results:
                latest = source.results[-1]
                self._decider_events.append(
                    {
                        "type": "decider_message",
                        "turn": turn,
                        "model": latest.model or self._decider_model,
                        "text": latest.message,
                        "is_fallback": latest.is_fallback,
                    }
                )
            logger.info("Sending turn {}/{}: {}", turn, len(sources), message[:80])
            # Anchor reply detection on the event count before the send, so an
            # injected user message can't be mistaken for the agent's reply.
            baseline_event_count = len(self._latest_events)
            is_sent = await minds_bridge.send_chat_message(
                environment,
                self._box_env,
                self._workspace_agent_id,
                self._chat_agent_id,
                message,
                deadline,
                self._poll_seconds,
            )
            if not is_sent:
                await self._mark_timed_out(environment, "could not send turn {}".format(turn))
                return
            self._conversation.append({"role": "user", "text": message})
            self._waits_done = turn
            await self._sync_trial_files(environment)

            is_replied = await self._wait_for_reply(environment, deadline, baseline_event_count)
            if not is_replied:
                await self._mark_timed_out(environment, "no reply to turn {}".format(turn))
                return
            reply_texts = _new_agent_reply_texts(self._latest_events, baseline_event_count)
            # Record each message's length before merging, so the per-message
            # metric sees the agent's real (short) messages, not the merged wall.
            self._agent_message_word_counts.extend(len(text.split()) for text in reply_texts)
            reply_text = "\n\n".join(reply_texts)
            self._conversation.append({"role": "agent", "text": reply_text})
            await self._sync_trial_files(environment)

            is_final_turn = turn == len(sources)
            if _is_snapshot_wanted(self._snapshot_mode, is_final_turn):
                await minds_bridge.snapshot_workspace(
                    environment, self._box_env, self._workspace_agent_id, "post_message_{}".format(turn)
                )

        self._test_state = "finished"
        # The agent can keep working after it reports WAITING -- the workspace's own turn-end flow
        # runs then -- so pull once more before the transcript is written for the last time. Without
        # this the transcript ends at the final reply while the proxy keeps metering, which shows up
        # as the proxy accounting for requests the transcript has no messages for.
        await self._refresh_events(environment)
        await self._sync_trial_files(environment)
        logger.info("Finished after {} turns", len(sources))

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

    async def _authenticate_workspace(self, environment: BaseEnvironment, deadline: float) -> bool:
        """Sign the workspace in after create, the way a user does.

        A workspace boots unauthenticated -- the product's create path supplies no AI credentials --
        so without this the chat agent can never take a turn. Doing it through the sign-in endpoint
        rather than the create-time host env is what keeps the graded agent in production's shared
        config-dir regime.
        """
        assert self._box_env is not None
        # Behind the proxy the workspace gets the trial's own key, never the upstream one: it is
        # scoped to this trial, and the credential the agent can see buys nothing anywhere else.
        api_key = self._proxy_key or self._get_env("ANTHROPIC_API_KEY") or ""
        if not api_key:
            logger.error("No ANTHROPIC_API_KEY to sign the workspace in with")
            return False
        is_endpoint_ready = await minds_bridge.wait_for_auth_endpoint(
            environment, self._box_env, self._workspace_agent_id, deadline, self._poll_seconds
        )
        if not is_endpoint_ready:
            logger.error("The workspace's claude-auth endpoint never came up")
            return False
        logger.info("Signing the workspace in through the claude-auth endpoint")
        is_submitted = await minds_bridge.authenticate_workspace(
            environment,
            self._box_env,
            self._workspace_agent_id,
            api_key,
            self._proxy_base_url or self._get_env("ANTHROPIC_BASE_URL") or "",
        )
        if not is_submitted:
            return False
        # Submitting restarts the claude agents, so the chat agent is briefly gone; the turn loop's
        # own WAITING gate covers the rest.
        return await minds_bridge.wait_for_chat_state(
            environment,
            self._box_env,
            self._workspace_agent_id,
            self._chat_agent_id,
            is_waiting_desired=True,
            deadline=deadline,
            poll_seconds=self._poll_seconds,
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
        with the box's /work/mngr (ported from the old harness's launch clone prep, minus the retired
        eval worker's metadata file)."""
        assert self._box_env is not None
        exclude_flags = " ".join("--exclude='{}'".format(pattern) for pattern in _VENDOR_EXCLUDES)
        # Config-derived values are shell-quoted wherever they are interpolated
        # into a box command -- the case id here, the dwt repo/branch/sha in
        # build_eval_base_clone_command. They come from an author-controlled eval
        # config, but a quote or space in one must not break out of the command.
        clone_dir = shlex.quote("/work/clones/{}".format(case.case_id))
        commit_message = shlex.quote("eval case {}".format(case.case_id))
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
                eval_base_dir="/work/eval-base",
            ),
            self._box_env,
            600,
        )
        await minds_bridge.check_run_in_box(
            environment,
            "mkdir -p /work/clones && rm -rf {clone} && git clone /work/eval-base {clone}".format(clone=clone_dir),
            self._box_env,
            300,
        )
        await minds_bridge.check_run_in_box(
            environment,
            "mkdir -p {clone}/system/vendor/mngr && rsync -a --delete {excludes} /work/mngr/ {clone}/system/vendor/mngr/".format(
                clone=clone_dir, excludes=exclude_flags
            ),
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
            "printf '{base_marker}\\n'; git -C {clone} rev-parse HEAD; "
            "printf '{dwt_marker}\\n'; git -C /work/eval-base rev-parse HEAD".format(
                clone=clone_dir,
                base_marker=verification.section_marker("base_sha"),
                dwt_marker=verification.section_marker("dwt_tip_sha"),
            ),
            self._box_env,
            120,
        )
        sections = verification.split_sections(base_result.stdout or "")
        self._dwt_tip_sha = sections.get("dwt_tip_sha", "").strip()
        base_output = sections.get("base_sha", "").strip()
        self._clone_base_sha = base_output.splitlines()[-1].strip() if base_output else ""

    async def _mark_timed_out(self, environment: BaseEnvironment, reason: str) -> None:
        logger.warning("Marking the trial timed_out: {}", reason)
        self._test_state = "timed_out"
        await self._sync_trial_files(environment)

    def _state_payload(self) -> dict[str, Any]:
        turn_count = len(self._case.prompts) if self._case is not None else 0
        return {
            "eval_name": self.logs_dir.parent.name,
            "case_name": self._case.case_id if self._case is not None else "",
            "mngr_sha": self._mngr_sha,
            "dwt_sha": self._case.dwt_sha if self._case is not None else "",
            "waits_done": self._waits_done,
            # "num_turns" is the ported state.json schema key (the old harness's
            # readers and the verifier gates both consume it).
            "num_turns": turn_count,
            "test_state": self._test_state,
            "timed_out": self._test_state == "timed_out",
            "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - self._started_at, 1),
            "timeout_seconds": self._case.timeout_seconds if self._case is not None else 0.0,
        }

    @staticmethod
    def _jsonl(records: list[dict[str, Any]]) -> str:
        lines = [json.dumps(record) for record in records]
        return "\n".join(lines) + ("\n" if lines else "")

    def _transcript_jsonl(self) -> str:
        # Workspace events verbatim (same schema as today), with the harness's
        # decider events appended at the end so LLM turns stay auditable.
        return self._jsonl([*self._latest_events, *self._decider_events])

    async def _sync_trial_files(self, environment: BaseEnvironment) -> None:
        """Write the raw transcript, the clean conversation, and state to the host logs dir and
        mirror them into the box's /logs/agent/, where the task's declared artifacts pick them up
        for the verifier."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        file_contents = {
            TRANSCRIPT_FILENAME: self._transcript_jsonl(),
            CONVERSATION_FILENAME: self._jsonl(_conversation_events(self._conversation)),
            STATE_FILENAME: json.dumps(self._state_payload(), indent=2),
        }
        await environment.exec("mkdir -p {}".format(minds_bridge.BOX_LOGS_DIR), timeout_sec=60)
        for filename, content in file_contents.items():
            local_path = self.logs_dir / filename
            local_path.write_text(content)
            await environment.upload_file(local_path, "{}/{}".format(minds_bridge.BOX_LOGS_DIR, filename))

    def _populate_context_metadata(self, context: AgentContext) -> None:
        turn_word_counts = _words_per_agent_turn(self._conversation)
        message_word_counts = self._agent_message_word_counts
        decider_results = self._persona_source.results if self._persona_source is not None else []
        # Harbor's token/cost fields describe the agent under test, so they carry the workspace
        # agent's consumption. The decider is the harness's own spend and goes to metadata; putting
        # it here would report the simulated user's tokens as the agent's.
        transcript_usage = usage_accounting.summarize_workspace_usage(self._latest_events)
        proxy_usage = (
            usage_accounting.summarize_proxy_usage(self._proxy_usage_records) if self._proxy_usage_records else None
        )
        # The proxy is the complete account when one ran: it is the boundary every call crosses, so
        # it includes delegated work the transcript never sees. On a delegating case the two differ
        # by that work -- measured at 45% of the real cost -- so preferring the transcript here would
        # publish the understated figure.
        workspace_usage = proxy_usage if proxy_usage is not None else transcript_usage
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
                "This trial delegated ({} subagent call(s), {} worker launch(es)), so its reported cost is a "
                "lower bound: delegated work is served on streams this driver does not read",
                workspace_usage.delegated_call_count,
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
            "turns_completed": self._waits_done,
            "turn_count": len(self._case.prompts) if self._case is not None else 0,
            "test_state": self._test_state,
            "timed_out": self._test_state == "timed_out",
            # Per merged agent turn (feeds the verifier's wordiness gate) vs. per
            # individual agent message (observability only; the judge grades the
            # per-message rendering it re-derives from full_transcript.jsonl).
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
            "usage_source": "proxy" if proxy_usage is not None else "transcript",
            "transcript_usage": usage_accounting.workspace_usage_metadata(transcript_usage),
            "decider_usage": usage_accounting.decider_usage_metadata(decider_usage),
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

    def _write_trajectory(self) -> None:
        """Write the ATIF trajectory (for `harbor view`) from the clean per-turn conversation."""
        timestamp = _utc_now_iso()
        steps: list[Step] = [
            Step(
                step_id=index + 1,
                timestamp=timestamp,
                source="user" if entry["role"] == "user" else "agent",
                message=entry["text"],
            )
            for index, entry in enumerate(self._conversation)
            if entry["text"].strip()
        ]
        if not steps:
            # ATIF requires at least one step; a trial that died before any
            # exchange has no conversation to render.
            return
        workspace_usage = usage_accounting.summarize_workspace_usage(self._latest_events)
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=self.session_id,
            agent=TrajectoryAgent(
                name=self.name(), version=self.version() or "unknown", model_name=self._decider_model
            ),
            steps=steps,
            # The workspace agent's totals, not the decider's: the trajectory describes the
            # conversation being graded. total_steps counts conversation turns, not LLM calls.
            final_metrics=FinalMetrics(
                total_prompt_tokens=workspace_usage.n_input_tokens,
                total_completion_tokens=workspace_usage.tokens.output,
                total_cached_tokens=workspace_usage.n_cache_tokens,
                total_cost_usd=workspace_usage.cost_usd,
                total_steps=len(steps),
            )
            if workspace_usage.message_count
            else None,
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "trajectory.json").write_text(json.dumps(trajectory.to_json_dict(), indent=2))
