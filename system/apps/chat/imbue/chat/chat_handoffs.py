"""The handoff: continuing a chat on another harness (``docs/system/blueprint/chat-agent-split/`` section 5).

A handoff converges the chat's active agent, archives it, and creates its successor with a
summary. Its whole working state lives on the chat record's ``handoff`` entry, and every step
re-checks reality before acting, so a chat-app restart at any point resumes by running the
steps again: each one either finds its work done or does it. The runner here owns the steps;
the manager owns the record, the lock, and the tracked agents, and hands the runner what it
needs as bound callables (``HandoffDeps``), the same shape the harness sessions take.

The phases, in order: ``draining`` (wait out an in-flight send, return the queue to the
composer), ``summarizing`` (reuse a fresh summary or ask the retiring agent for one),
``switching`` (stop, archive, record the segment's length, create the successor, deliver the
held sends), then active again with the ``agent_switch`` chip between the two segments. A
failed create leaves the chat in the ``failed`` phase, which a retry on any account runs the
create step of again with the same prompt.
"""

import string
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.accounts import Account
from imbue.chat.accounts import AccountError
from imbue.chat.accounts import account_dir
from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import is_lifecycle_dead
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.chat_records import ChatAgentEntry
from imbue.chat.chat_records import ChatHandoffRecord
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import ChatRecordError
from imbue.chat.chat_transcript import TranscriptSegment
from imbue.chat.chat_transcript import agent_switch_event
from imbue.chat.harnesses.binding import create_args as binding_create_args
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import HARNESS_LABEL
from imbue.chat.harnesses.message_display import HANDOFF_SUMMARY_COMMAND
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.chat.models import AgentRestartError
from imbue.chat.models import AgentStateItem
from imbue.chat.models import AgentStopError
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.models import SummaryOutcome
from imbue.chat.primitives import ChatId
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# The default template of the successor's first message, relative to the repo root every
# supervised program runs from; a reference document so its wording stays editable.
DEFAULT_PROMPT_TEMPLATE_PATH: Final[Path] = Path(".agents/shared/references/continue-chat.md")

# How long the retiring agent gets to write its summary once the request was accepted. A
# summary can take a while; an agent that cannot write one (out of tokens, a full context
# window) ends its turn within seconds and is caught by the idle rule long before this.
SUMMARY_TIMEOUT_SECONDS: Final[float] = 300.0
# The turn the request starts can be shorter than one poll, so an idle reading counts as the
# turn having ended either after a busy reading or after this much time with none.
SUMMARY_IDLE_GRACE_SECONDS: Final[float] = 10.0
SUMMARY_POLL_INTERVAL_SECONDS: Final[float] = 1.0

# How long one ``mngr rename`` (a metadata write) and one ``mngr destroy`` may take.
_RENAME_TIMEOUT_SECONDS: Final[float] = 30.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 120.0
# The successor's ``mngr create`` provisions, starts, awaits readiness (45s in this workspace)
# and delivers the prompt before it returns.
_CREATE_TIMEOUT_SECONDS: Final[float] = 300.0
# How much of a failed create's output the failed phase carries.
CREATION_OUTPUT_TAIL_LINES: Final[int] = 20

_SUMMARIES_DIRNAME: Final[str] = "summaries"
_PROMPT_FILENAME_PREFIX: Final[str] = "handoff-prompt-"


class HandoffCancelledError(RuntimeError):
    """The handoff a runner was working on is no longer the chat's (cancelled, or replaced by a retry)."""


class HandoffStepError(RuntimeError):
    """A step of the switch that mngr refused; the runner stops and a resume runs the step again."""


@pure
def archived_agent_name(seq: int, chat_name: str, agent_id: str) -> str:
    """The archival mngr name (spec 4.3): sorts archived agents together, orders them, stays unique."""
    return f"archived-{seq}-{chat_name}-{agent_id}"


@pure
def archived_display_name(chat_title: str, seq: int) -> str:
    return f"{chat_title} (archived {seq})"


@pure
def summary_path(chat_files_root: Path, chat_id: ChatId, retiring_seq: int) -> Path:
    """Where the retiring agent's summary goes: beside the chat's record, named by its sequence number."""
    return chat_files_root / chat_id / _SUMMARIES_DIRNAME / f"{retiring_seq}.md"


@pure
def prompt_path(chat_files_root: Path, chat_id: ChatId, next_seq: int) -> Path:
    return chat_files_root / chat_id / f"{_PROMPT_FILENAME_PREFIX}{next_seq}.md"


@pure
def summary_request_message(path: Path) -> str:
    """The slash command that asks the retiring agent for its summary (the ``handoff-summary`` skill)."""
    return f"{HANDOFF_SUMMARY_COMMAND} {path}"


@pure
def last_user_turn_epoch(events: list[dict[str, Any]]) -> float | None:
    """When the transcript's last genuine user turn happened, or None when it has none.

    A genuine turn is a ``user_message`` with no display decision: a chip (the summary request
    itself, a nudge), a hidden framework line, or a permission verdict is not one.
    """
    for event in reversed(events):
        if event.get("type") == "user_message" and event.get("display") is None:
            return parse_iso_timestamp_to_epoch(event.get("timestamp"))
    return None


@pure
def is_summary_fresh(path_mtime: float | None, last_turn_epoch: float | None) -> bool:
    """A summary is fresh when it was written after the retiring agent's last genuine user turn."""
    if path_mtime is None:
        return False
    return last_turn_epoch is None or path_mtime > last_turn_epoch


@pure
def failure_notice(error: str | None, output_tail: str) -> str:
    """What a failed create's page says: the reason, then the last lines mngr printed."""
    reason = error or "mngr create failed"
    return f"{reason}\n{output_tail}" if output_tail else reason


@pure
def is_duplicate_id_refusal(output: str, agent_id: str) -> bool:
    """Whether a failed create refused the pre-minted id because a half-made agent already holds it."""
    return "DuplicateAgentIdOnHostError" in output or (agent_id in output and "already exists" in output)


class CreationOutputTail(MutableModel):
    """Keeps the last lines a ``mngr create`` printed, for the notice a failed create shows.

    Every line is also logged as it arrives, so a create that fails is diagnosable from the
    app's log after the fact; the tail is what the chat page can show at once.
    """

    lines: list[str] = Field(default_factory=list)

    def __call__(self, line: str, _is_stdout: bool) -> None:
        stripped = line.rstrip("\n")
        logger.debug("mngr create: {}", stripped)
        self.lines = [*self.lines, stripped][-CREATION_OUTPUT_TAIL_LINES:]

    def text(self) -> str:
        return "\n".join(self.lines)


class SuccessorCreateSpec(FrozenModel):
    """What the successor's ``mngr create`` names, for the manager's shared argv builder."""

    name: str = Field(description="The chat's display name; its canonical form is the mngr name the successor takes")
    chat_id: ChatId = Field(description="The chat the successor joins")
    agent_id: str = Field(description="The pre-minted successor id")
    harness: HarnessType = Field(description="The harness the successor runs")
    project_id: str = Field(description="The project label to carry, '' for none")
    account_args: tuple[str, ...] = Field(description="The account binding arguments")
    extra_labels: tuple[str, ...] = Field(description="Further ``KEY=VALUE`` labels: the chat membership")
    message_file: Path = Field(description="The file holding the handoff prompt, the successor's first message")


class HandoffDeps(FrozenModel):
    """Everything the runner needs from the manager and the app state, bound once."""

    model_config = {"arbitrary_types_allowed": True}

    mngr_binary: str
    host_dir: Path
    # The primary agent's work dir: where the successor's create runs, like every chat create.
    work_dir: Path
    chat_files_root: Path
    prompt_template_path: Path
    shutdown_event: ShutdownEvent
    read_record: Callable[[ChatId], ChatRecord | None]
    # Replace the record's handoff entry under the manager's lock, given the current record;
    # raises ``HandoffCancelledError`` when the record no longer carries this handoff.
    update_record: Callable[[ChatId, str, Callable[[ChatRecord], ChatRecord]], ChatRecord]
    # Pop the next held send, or clear the handoff and return None when none remain; atomic
    # with the message route's hold, so a send can never be appended to a handoff that just
    # finished. Raises ``HandoffCancelledError`` for another handoff.
    take_next_held_send: Callable[[ChatId, str], HeldSend | None]
    get_agent_state: Callable[[str], AgentStateItem | None]
    get_agent_info: Callable[[str], AgentInfo | None]
    resolve_account: Callable[[str], Account]
    # The send path the message route takes, revival included; raises ``SendFailedError``.
    deliver: Callable[[AgentInfo, str, str], SendOutcome]
    # Interrupt the agent's turn and return its queue as one block (the stop button's path).
    drain_to_composer: Callable[[AgentInfo], str]
    ensure_watcher: Callable[[AgentInfo], TranscriptReader]
    # ``mngr stop`` plus the session's dead-lifecycle teardown, reflected in the tracked state.
    stop_agent: Callable[[AgentInfo], None]
    note_agent_renamed: Callable[[str, str, Mapping[str, str]], None]
    note_agent_created: Callable[[AgentStateItem], None]
    build_create_command: Callable[[SuccessorCreateSpec], list[str]]
    broadcast_transcript_events: Callable[[ChatId, list[dict[str, Any]]], None]
    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    summary_timeout_seconds: float = SUMMARY_TIMEOUT_SECONDS
    summary_idle_grace_seconds: float = SUMMARY_IDLE_GRACE_SECONDS
    summary_poll_interval_seconds: float = SUMMARY_POLL_INTERVAL_SECONDS


class HandoffRunner:
    """Runs one chat's handoff through its phases, each step idempotent against mngr's state."""

    _deps: HandoffDeps

    @classmethod
    def build(cls, deps: HandoffDeps) -> "HandoffRunner":
        runner = cls.__new__(cls)
        runner._deps = deps
        return runner

    def _current(self, chat_id: ChatId, handoff_id: str) -> tuple[ChatRecord, ChatHandoffRecord]:
        if self._deps.shutdown_event.is_set():
            raise HandoffCancelledError("the chat app is shutting down; the handoff resumes on the next start")
        record = self._deps.read_record(chat_id)
        if record is None or record.handoff is None or record.handoff.handoff_id != handoff_id:
            raise HandoffCancelledError(f"chat {chat_id} no longer carries handoff {handoff_id}")
        return record, record.handoff

    def _update_handoff(
        self, chat_id: ChatId, handoff_id: str, change: Callable[[ChatHandoffRecord], ChatHandoffRecord]
    ) -> ChatRecord:
        return self._deps.update_record(chat_id, handoff_id, lambda record: _with_handoff_changed(record, change))

    def run(self, chat_id: ChatId, handoff_id: str) -> None:
        """Take the handoff from whatever phase it is in to active or failed.

        Quiet when the handoff was cancelled or the app is shutting down. A step mngr refused
        is logged and left where it is: the record still carries the phase, and the next
        resume (a restart, or a retry) runs the step again.
        """
        try:
            self._run_phases(chat_id, handoff_id)
        except HandoffCancelledError as e:
            logger.info("Handoff of chat {} stopped: {}", chat_id, e)
        except (HandoffStepError, AgentStopError, ChatRecordError, OSError) as e:
            logger.opt(exception=e).error("Handoff of chat {} could not finish its current step", chat_id)

    def _run_phases(self, chat_id: ChatId, handoff_id: str) -> None:
        is_done = False
        while not is_done:
            record, handoff = self._current(chat_id, handoff_id)
            match handoff.phase:
                case HandoffPhase.DRAINING:
                    self.drain(chat_id, handoff_id)
                case HandoffPhase.SUMMARIZING:
                    self._summarize(chat_id, handoff_id, record, handoff)
                case HandoffPhase.SWITCHING:
                    self._switch(chat_id, handoff_id, record, handoff)
                    is_done = True
                case HandoffPhase.FAILED:
                    is_done = True

    # -- draining ------------------------------------------------------------------------------

    def drain(self, chat_id: ChatId, handoff_id: str) -> str:
        """Return the retiring agent's queue to the composer and move on to summarizing.

        A confirmed switch is a stop (spec 5.3): the queue cannot be pulled out of a live turn
        without ending it, so the stop button's own interrupt does the draining, which also
        waits out an in-flight send under the message lock. A stopped agent has nothing to
        drain. Returns the block for the composer; the route answers with it, and it also
        stays on the record for a page that reloads.
        """
        record, handoff = self._current(chat_id, handoff_id)
        if handoff.phase is not HandoffPhase.DRAINING:
            return handoff.returned_block
        retiring_id = record.agents[-1].agent_id
        agent_state = self._deps.get_agent_state(retiring_id)
        agent_info = self._deps.get_agent_info(retiring_id)
        block = ""
        if agent_state is not None and agent_info is not None and not is_lifecycle_dead(agent_state.state):
            try:
                block = self._deps.drain_to_composer(agent_info)
            except (AgentRestartError, OSError) as e:
                # The switch stops the agent regardless; what was queued is then gone with the
                # session, which the queue contract allows, so this is logged, not fatal.
                logger.warning("Handoff of chat {}: could not drain agent {}: {}", chat_id, retiring_id, e)
        self._update_handoff(
            chat_id,
            handoff_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().phase, HandoffPhase.SUMMARIZING),
                to_update(current.field_ref().returned_block, _joined_blocks(current.returned_block, block)),
            ),
        )
        return _joined_blocks(handoff.returned_block, block)

    # -- summarizing ---------------------------------------------------------------------------

    def _summarize(self, chat_id: ChatId, handoff_id: str, record: ChatRecord, handoff: ChatHandoffRecord) -> None:
        outcome = self._summary_outcome(chat_id, handoff_id, record, handoff)
        # The prompt is built once, here, and resent verbatim by every retry (spec 5.8); the
        # trigger message rides inside it, so it leaves the held list.
        trigger = handoff.held_send_for(handoff.trigger_message_id)
        prompt = self._render_prompt(record, handoff, outcome, trigger.text if trigger is not None else "")
        self._update_handoff(
            chat_id,
            handoff_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().phase, HandoffPhase.SWITCHING),
                to_update(current.field_ref().summary_outcome, outcome),
                to_update(current.field_ref().prompt, prompt),
                to_update(
                    current.field_ref().held_sends,
                    tuple(held for held in current.held_sends if held.message_id != current.trigger_message_id),
                ),
            ),
        )

    def _summary_outcome(
        self, chat_id: ChatId, handoff_id: str, record: ChatRecord, handoff: ChatHandoffRecord
    ) -> SummaryOutcome:
        """Reuse a fresh summary, else ask the retiring agent for one and wait for the proceed conditions (spec 5.5)."""
        retiring_id = record.agents[-1].agent_id
        agent_info = self._deps.get_agent_info(retiring_id)
        if agent_info is None:
            logger.warning(
                "Handoff of chat {}: agent {} is gone, so no summary can be asked for", chat_id, retiring_id
            )
            return SummaryOutcome.MISSING
        path = summary_path(self._deps.chat_files_root, chat_id, handoff.retiring_seq)
        watcher = self._deps.ensure_watcher(agent_info)
        if is_summary_fresh(_non_empty_mtime(path), last_user_turn_epoch(watcher.get_all_events())):
            logger.info("Handoff of chat {}: reusing the fresh summary at {}", chat_id, path)
            return SummaryOutcome.REUSED
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            sent = self._deps.deliver(agent_info, summary_request_message(path), f"handoff-summary-{handoff_id}")
        except SendFailedError as e:
            logger.warning("Handoff of chat {}: the summary request was refused: {}", chat_id, e.detail)
            return SummaryOutcome.MISSING
        if sent is not SendOutcome.OK:
            logger.warning("Handoff of chat {}: the summary request did not land ({})", chat_id, sent.value)
            return SummaryOutcome.MISSING
        return self._await_summary(chat_id, handoff_id, retiring_id, path)

    def _await_summary(self, chat_id: ChatId, handoff_id: str, retiring_id: str, path: Path) -> SummaryOutcome:
        """Wait for the file, the turn ending without it, or the timeout; cancel is checked on every poll."""
        accepted_at = self._deps.monotonic()
        deadline = accepted_at + self._deps.summary_timeout_seconds
        is_busy_seen = False
        outcome: SummaryOutcome | None = None
        while outcome is None:
            self._current(chat_id, handoff_id)
            agent_state = self._deps.get_agent_state(retiring_id)
            activity = agent_state.activity_state if agent_state is not None else None
            is_dead = agent_state is None or is_lifecycle_dead(agent_state.state)
            now = self._deps.monotonic()
            is_busy_seen = is_busy_seen or activity in (ActivityState.THINKING, ActivityState.TOOL_RUNNING)
            is_turn_over = activity not in (ActivityState.THINKING, ActivityState.TOOL_RUNNING) and (
                is_dead or is_busy_seen or now - accepted_at >= self._deps.summary_idle_grace_seconds
            )
            if _non_empty_mtime(path) is not None:
                outcome = SummaryOutcome.WRITTEN
            elif is_turn_over:
                logger.info("Handoff of chat {}: the summary turn ended with no file at {}", chat_id, path)
                outcome = SummaryOutcome.MISSING
            elif now >= deadline:
                logger.warning("Handoff of chat {}: gave up waiting for a summary at {}", chat_id, path)
                outcome = SummaryOutcome.MISSING
            else:
                self._deps.sleep(self._deps.summary_poll_interval_seconds)
        return outcome

    def _render_prompt(
        self, record: ChatRecord, handoff: ChatHandoffRecord, outcome: SummaryOutcome, trigger_text: str
    ) -> str:
        """Fill the ``continue-chat`` reference in: the summary, the predecessors, the lanes, the user's message."""
        template = string.Template(self._deps.prompt_template_path.read_text())
        retiring = record.agents[-1]
        path = summary_path(self._deps.chat_files_root, record.chat_id, handoff.retiring_seq)
        match outcome:
            case SummaryOutcome.REUSED | SummaryOutcome.WRITTEN:
                summary_line = f"Your predecessor's summary is at {path}; read it first."
            case SummaryOutcome.MISSING:
                summary_line = "Your predecessor did not produce a summary; gather context from its transcript before anything else."
        predecessors = "\n".join(
            f"- seq {entry.seq}: {archived_agent_name(entry.seq, handoff.chat_name, entry.agent_id)}, id "
            f"{entry.agent_id}, harness {entry.harness.value}, state dir {self._deps.host_dir / 'agents' / entry.agent_id}"
            for entry in record.agents
        )
        return template.substitute(
            title=handoff.chat_title,
            chat_id=record.chat_id,
            predecessor_harness=HARNESS_LABEL[retiring.harness],
            successor_harness=HARNESS_LABEL[handoff.target_harness],
            summary_line=summary_line,
            predecessors=predecessors,
            source_lane=retiring.lane,
            target_lane=handoff.target_lane,
            target_account=handoff.target_account_id,
            message=trigger_text,
        )

    # -- switching -----------------------------------------------------------------------------

    def _switch(self, chat_id: ChatId, handoff_id: str, record: ChatRecord, handoff: ChatHandoffRecord) -> None:
        """Stop, archive, and measure the retiring agent, then create the successor and hand it the held sends."""
        retiring = record.agents[-1]
        self._stop_retiring(chat_id, retiring)
        self._archive_retiring(chat_id, handoff, retiring)
        record_after_count = self._record_final_count(chat_id, handoff_id, retiring)
        successor_state = self._create_successor(chat_id, handoff_id, record_after_count, handoff)
        if successor_state is None:
            return
        self._complete(chat_id, handoff_id, record_after_count, handoff, successor_state)

    def _stop_retiring(self, chat_id: ChatId, retiring: ChatAgentEntry) -> None:
        agent_state = self._deps.get_agent_state(retiring.agent_id)
        agent_info = self._deps.get_agent_info(retiring.agent_id)
        if agent_state is None or agent_info is None or is_lifecycle_dead(agent_state.state):
            return
        logger.info("Handoff of chat {}: stopping agent {}", chat_id, retiring.agent_id)
        self._deps.stop_agent(agent_info)

    def _archive_retiring(self, chat_id: ChatId, handoff: ChatHandoffRecord, retiring: ChatAgentEntry) -> None:
        """One ``mngr rename`` carrying every label: the archival display name, the membership, ``archived_at``."""
        agent_state = self._deps.get_agent_state(retiring.agent_id)
        if agent_state is None:
            logger.warning("Handoff of chat {}: agent {} is gone and cannot be archived", chat_id, retiring.agent_id)
            return
        archival_name = archived_agent_name(retiring.seq, handoff.chat_name, retiring.agent_id)
        if agent_state.name == archival_name:
            return
        labels = {
            "display_name": archived_display_name(handoff.chat_title, retiring.seq),
            "chat_id": str(chat_id),
            "chat_seq": str(retiring.seq),
            "archived_at": self._deps.now().isoformat(),
        }
        command = [self._deps.mngr_binary, "rename", retiring.agent_id, archival_name]
        for key, value in labels.items():
            command.extend(["--label", f"{key}={value}"])
        result = run_local_command_modern_version(
            command=command, cwd=None, is_checked=False, timeout=_RENAME_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise HandoffStepError(
                f"could not archive agent {retiring.agent_id} of chat {chat_id}: {result.stderr.strip()}"
            )
        self._deps.note_agent_renamed(retiring.agent_id, archival_name, labels)

    def _record_final_count(self, chat_id: ChatId, handoff_id: str, retiring: ChatAgentEntry) -> ChatRecord:
        """Close the retiring agent's entry: when it ended, its archival name, and its segment's length."""
        if retiring.ended_at is not None and retiring.final_event_count is not None:
            return self._current(chat_id, handoff_id)[0]
        agent_info = self._deps.get_agent_info(retiring.agent_id)
        count = self._deps.ensure_watcher(agent_info).get_total_event_count() if agent_info is not None else 0
        ended_at = self._deps.now()
        return self._deps.update_record(
            chat_id, handoff_id, lambda record: _with_retiring_closed(record, retiring, ended_at, count)
        )

    def _create_successor(
        self, chat_id: ChatId, handoff_id: str, record: ChatRecord, handoff: ChatHandoffRecord
    ) -> AgentStateItem | None:
        """Create the successor under its pre-minted id, or adopt one an earlier attempt already made.

        Returns its tracked state, or None once the failed phase has been written.
        """
        existing = self._deps.get_agent_state(handoff.next_agent_id)
        if existing is not None:
            logger.info(
                "Handoff of chat {}: adopting agent {} from an earlier attempt", chat_id, handoff.next_agent_id
            )
            return existing
        try:
            account = self._deps.resolve_account(handoff.target_account_id)
        except AccountError as e:
            self._fail(chat_id, handoff_id, f"The account the chat was moving to is gone: {e}")
            return None
        state_dir = self._deps.host_dir / "agents" / handoff.next_agent_id
        prompt_file = prompt_path(self._deps.chat_files_root, chat_id, handoff.next_seq)
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(handoff.prompt or "")
        spec = SuccessorCreateSpec(
            name=handoff.chat_title,
            chat_id=chat_id,
            agent_id=handoff.next_agent_id,
            harness=handoff.target_harness,
            project_id=handoff.project_label,
            account_args=(
                *binding_create_args(handoff.target_harness, account_dir(account.id), state_dir),
                "--label",
                f"account={account.id}",
            ),
            extra_labels=(f"chat_id={chat_id}", f"chat_seq={handoff.next_seq}"),
            message_file=prompt_file,
        )
        command = self._deps.build_create_command(spec)
        error = self._run_create(chat_id, command)
        if error is not None and is_duplicate_id_refusal(error, handoff.next_agent_id):
            # A half-made agent from a create this process did not see finish holds the id;
            # mngr refuses to reuse it, so it is destroyed and the create run again once.
            logger.warning("Handoff of chat {}: destroying the half-made agent {}", chat_id, handoff.next_agent_id)
            run_local_command_modern_version(
                command=[self._deps.mngr_binary, "destroy", handoff.next_agent_id, "--force"],
                cwd=None,
                is_checked=False,
                timeout=_DESTROY_TIMEOUT_SECONDS,
            )
            error = self._run_create(chat_id, command)
        if error is not None:
            self._fail(chat_id, handoff_id, error)
            return None
        labels = {
            "user_created": "true",
            "display_name": handoff.chat_title,
            "account": account.id,
            "chat_id": str(chat_id),
            "chat_seq": str(handoff.next_seq),
        }
        if handoff.project_label:
            labels["project"] = handoff.project_label
        return AgentStateItem(
            id=handoff.next_agent_id,
            name=handoff.chat_name,
            state="RUNNING",
            labels=labels,
            work_dir=str(self._deps.work_dir),
            harness=handoff.target_harness,
        )

    def _fail(self, chat_id: ChatId, handoff_id: str, error: str) -> None:
        """The failed phase (spec 5.10): the chat has no running agent, the page shows why, and a retry reruns the create."""
        logger.warning("Handoff of chat {} failed: {}", chat_id, error)
        self._update_handoff(
            chat_id,
            handoff_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().phase, HandoffPhase.FAILED),
                to_update(current.field_ref().error, error),
            ),
        )

    def _run_create(self, chat_id: ChatId, command: list[str]) -> str | None:
        """Run the successor's create; None on success, else the notice the failed phase shows."""
        output_tail = CreationOutputTail()
        logger.info("Handoff of chat {}: mngr create: {}", chat_id, " ".join(command))
        try:
            result = run_local_command_modern_version(
                command=command,
                cwd=self._deps.work_dir,
                is_checked=False,
                trace_output=True,
                trace_on_line_callback=output_tail,
                shutdown_event=self._deps.shutdown_event,
                timeout=_CREATE_TIMEOUT_SECONDS,
            )
        except (OSError, ConcurrencyGroupError) as e:
            logger.opt(exception=e).error("Handoff of chat {}: error creating the successor", chat_id)
            return failure_notice(str(e), output_tail.text())
        if result.returncode == 0:
            return None
        return failure_notice(f"mngr create exited with code {result.returncode}", output_tail.text())

    def _complete(
        self,
        chat_id: ChatId,
        handoff_id: str,
        record: ChatRecord,
        handoff: ChatHandoffRecord,
        successor_state: AgentStateItem,
    ) -> None:
        """Make the successor the chat's agent, emit the chip, and deliver the held sends in order.

        The handoff entry is cleared only once the held list is empty, inside the same lock
        the message route appends under, so a send that arrives during delivery is delivered
        by this loop rather than overtaking one still held.
        """
        retiring = record.agents[-1]
        successor = ChatAgentEntry(
            seq=handoff.next_seq,
            agent_id=handoff.next_agent_id,
            lane=handoff.target_lane,
            account_id=handoff.target_account_id,
            harness=handoff.target_harness,
            started_at=self._deps.now(),
        )
        self._deps.update_record(
            chat_id,
            handoff_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().agents, (*current.agents, successor))
            ),
        )
        self._deps.note_agent_created(successor_state)
        self._deps.broadcast_transcript_events(
            chat_id,
            [
                agent_switch_event(
                    chat_id,
                    TranscriptSegment(
                        agent_id=retiring.agent_id,
                        harness=retiring.harness,
                        seq=retiring.seq,
                        recorded_event_count=retiring.final_event_count,
                        ended_at=retiring.ended_at,
                    ),
                    TranscriptSegment(
                        agent_id=successor.agent_id,
                        harness=successor.harness,
                        seq=successor.seq,
                        recorded_event_count=None,
                        ended_at=None,
                    ),
                )
            ],
        )
        successor_info = self._deps.get_agent_info(successor.agent_id)
        if successor_info is not None:
            self._deps.ensure_watcher(successor_info)
        while (held := self._deps.take_next_held_send(chat_id, handoff_id)) is not None:
            if successor_info is None:
                logger.warning(
                    "Handoff of chat {}: agent {} is untracked; a held send is lost", chat_id, successor.agent_id
                )
                continue
            try:
                outcome = self._deps.deliver(successor_info, held.text, held.message_id)
            except SendFailedError as e:
                logger.warning("Handoff of chat {}: a held send was refused: {}", chat_id, e.detail)
                continue
            if outcome is not SendOutcome.OK:
                logger.warning("Handoff of chat {}: a held send did not land ({})", chat_id, outcome.value)
        logger.info("Handoff of chat {}: now running on agent {}", chat_id, successor.agent_id)


@pure
def _joined_blocks(first: str, second: str) -> str:
    return "\n".join(block for block in (first, second) if block)


@pure
def _with_handoff_changed(record: ChatRecord, change: Callable[[ChatHandoffRecord], ChatHandoffRecord]) -> ChatRecord:
    assert record.handoff is not None, "update_record only applies to the record carrying this handoff"
    return record.model_copy_update(to_update(record.field_ref().handoff, change(record.handoff)))


@pure
def _with_retiring_closed(record: ChatRecord, retiring: ChatAgentEntry, ended_at: datetime, count: int) -> ChatRecord:
    """The record with its last entry closed: when it ended, its archival name, and its segment's length."""
    assert record.handoff is not None, "update_record only applies to the record carrying this handoff"
    closed = retiring.model_copy_update(
        to_update(retiring.field_ref().ended_at, ended_at),
        to_update(
            retiring.field_ref().archived_name,
            archived_agent_name(retiring.seq, record.handoff.chat_name, retiring.agent_id),
        ),
        to_update(retiring.field_ref().final_event_count, count),
    )
    return record.model_copy_update(to_update(record.field_ref().agents, (*record.agents[:-1], closed)))


def _non_empty_mtime(path: Path) -> float | None:
    """The file's mtime when it exists with content, else None."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime if stat.st_size > 0 else None
