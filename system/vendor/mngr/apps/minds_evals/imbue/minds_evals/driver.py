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
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import DECIDE_SENTINEL
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.errors import InstructionParseError

TRANSCRIPT_FILENAME: Final[str] = "full_transcript.jsonl"
# The eval's own user turns paired with the agent's replies, filtered free of
# framework noise (the /welcome skill body, tool events, injected messages). The
# judge scores this rather than the raw stream, and the decider reads it as the
# conversation so far.
CONVERSATION_FILENAME: Final[str] = "conversation.jsonl"
STATE_FILENAME: Final[str] = "state.json"
MINDS_ENV: Final[str] = "staging"

# Electron plus the backend need several minutes on first boot; the agent-level
# override_setup_timeout_sec in the run recipe must cover this.
BACKEND_BOOT_TIMEOUT_SECONDS: Final[float] = 600.0

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


class SnapshotMode(UpperCaseStrEnum):
    """When the driver snapshots the workspace home tree into the trial artifacts."""

    PER_TURN = auto()
    FINAL = auto()
    OFF = auto()


@pure
def parse_snapshot_mode(raw_value: str) -> SnapshotMode:
    return SnapshotMode(raw_value.replace("-", "_").upper())


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


@pure
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MindsPersonaDriver(BaseAgent):
    """The host-side persona-driver agent: one Minds box per trial, one nested workspace per case."""

    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        *args: Any,
        snapshot_mode: str = "per-turn",
        modal_config_path: str = "",
        poll_seconds: float = 5.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
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

    @staticmethod
    def name() -> str:
        return "minds-persona-driver"

    def version(self) -> str | None:
        return "0.1.0"

    @property
    def _decider_model(self) -> str:
        return self._parsed_model_name or decider.DEFAULT_DECIDER_MODEL

    async def setup(self, environment: BaseEnvironment) -> None:
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
            anthropic_api_key=self._get_env("ANTHROPIC_API_KEY") or "",
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
            self._populate_context_metadata(context)
            # Written here rather than in populate_context_post_run: harbor only
            # calls that hook when the agent context is still empty, and this
            # driver always populates the context above.
            self._write_trajectory()
            await self._teardown(environment)

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
        await self._sync_trial_files(environment)
        logger.info("Finished after {} turns", len(sources))

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
        """Clone the workspace template in the box and overwrite its vendored mngr with the box's
        /work/mngr (ported from the old harness's launch clone prep, minus the retired eval worker's
        metadata file)."""
        assert self._box_env is not None
        exclude_flags = " ".join("--exclude='{}'".format(pattern) for pattern in _VENDOR_EXCLUDES)
        # Config-derived values (case id, dwt repo/branch) are shell-quoted:
        # they come from an author-controlled eval config, but a quote or space
        # in one must not break out of the command.
        clone_dir = shlex.quote("/work/clones/{}".format(case.case_id))
        commit_message = shlex.quote("eval case {}".format(case.case_id))
        logger.info("Preparing the workspace template clone ({}@{})", case.dwt_repo, case.dwt_branch)
        await minds_bridge.check_run_in_box(
            environment,
            "rm -rf /work/eval-base && git clone --branch {} {} /work/eval-base".format(
                shlex.quote(case.dwt_branch), shlex.quote(case.dwt_repo)
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
            "cd {clone} && git add -A && git -c user.email=eval@minds -c user.name=minds-eval commit -q -m {message}".format(
                clone=clone_dir, message=commit_message
            ),
            self._box_env,
            300,
        )

    async def _mark_timed_out(self, environment: BaseEnvironment, reason: str) -> None:
        logger.warning("Marking the trial timed_out: {}", reason)
        self._test_state = "timed_out"
        await self._sync_trial_files(environment)

    def _state_payload(self) -> dict[str, Any]:
        turn_count = len(self._case.prompts) if self._case is not None else 0
        return {
            "eval_name": self.logs_dir.parent.name,
            "case_name": self._case.case_id if self._case is not None else "",
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
        context.n_input_tokens = sum(result.input_token_count for result in decider_results) or None
        context.n_output_tokens = sum(result.output_token_count for result in decider_results) or None
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
        }

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
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=self.session_id,
            agent=TrajectoryAgent(
                name=self.name(), version=self.version() or "unknown", model_name=self._decider_model
            ),
            steps=steps,
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "trajectory.json").write_text(json.dumps(trajectory.to_json_dict(), indent=2))
