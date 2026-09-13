import threading
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.accounts import Account
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.chat_handoffs import HandoffCancelledError
from imbue.chat.chat_handoffs import HandoffDeps
from imbue.chat.chat_handoffs import HandoffRunner
from imbue.chat.chat_handoffs import SuccessorCreateSpec
from imbue.chat.chat_handoffs import archived_agent_name
from imbue.chat.chat_handoffs import is_duplicate_id_refusal
from imbue.chat.chat_handoffs import is_summary_fresh
from imbue.chat.chat_handoffs import last_user_turn_epoch
from imbue.chat.chat_handoffs import prompt_path
from imbue.chat.chat_handoffs import summary_path
from imbue.chat.chat_handoffs import summary_request_message
from imbue.chat.chat_records import ChatAgentEntry
from imbue.chat.chat_records import ChatHandoffRecord
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.chat_transcript import AGENT_SWITCH_EVENT_TYPE
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.mock_transcript_reader_test import ListTranscriptReader
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.models import ActivityState
from imbue.chat.models import AgentStateItem
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.models import HeldSendOrigin
from imbue.chat.models import SummaryOutcome
from imbue.chat.primitives import ChatId
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

# The repo's own prompt template, so its placeholders are checked against the runner's fields.
_PROMPT_TEMPLATE = Path(__file__).parents[5] / ".agents" / "shared" / "references" / "continue-chat.md"

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
_OPENAI_ACCOUNT = Account(id="acct-openai", lane="openai", seq=1, display="OpenAI")


class _EventsReader(ListTranscriptReader):
    """A transcript double whose events carry the fields the summary freshness rule reads."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        super().__init__([str(event["event_id"]) for event in events])
        self._full_events = events

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._full_events)


class _FakeWorkspace(MutableModel):
    """The manager's side of a handoff, in memory: the record, the tracked agents, the sends, the mngr log.

    Every ``HandoffDeps`` callable is bound to a method here, so a test reads what the runner
    did off one object.
    """

    model_config = {"arbitrary_types_allowed": True}

    tmp_path: Path
    chat_id: ChatId
    store: InMemoryChatRecordStore = Field(default_factory=InMemoryChatRecordStore)
    agents: dict[str, AgentStateItem] = Field(default_factory=dict)
    activity_by_agent: dict[str, ActivityState | None] = Field(default_factory=dict)
    events_by_agent: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    delivered: list[tuple[str, str, str]] = Field(default_factory=list)
    broadcasts: list[tuple[str, list[dict[str, Any]]]] = Field(default_factory=list)
    drained: list[str] = Field(default_factory=list)
    stopped: list[str] = Field(default_factory=list)
    drain_block: str = ""
    # What ``deliver`` does with the summary request: write the file, or nothing.
    is_summary_written_on_request: bool = True
    is_delivery_refused: bool = False
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    mngr_log: Path
    fail_dir: Path
    clock: float = 0.0

    def record(self) -> ChatRecord:
        record = self.store.read(self.chat_id)
        assert record is not None
        return record

    def read_record(self, chat_id: ChatId) -> ChatRecord | None:
        return self.store.read(chat_id)

    def _require(self, chat_id: ChatId, handoff_id: str) -> ChatRecord:
        record = self.store.read(chat_id)
        if record is None or record.handoff is None or record.handoff.handoff_id != handoff_id:
            raise HandoffCancelledError("not this handoff")
        return record

    def update_record(self, chat_id: ChatId, handoff_id: str, apply: Callable[[ChatRecord], ChatRecord]) -> ChatRecord:
        with self._lock:
            updated = apply(self._require(chat_id, handoff_id))
            self.store.write(updated)
            return updated

    def take_next_held_send(self, chat_id: ChatId, handoff_id: str) -> HeldSend | None:
        with self._lock:
            record = self._require(chat_id, handoff_id)
            handoff = record.handoff
            assert handoff is not None
            if handoff.held_sends:
                remaining = handoff.model_copy_update(
                    to_update(handoff.field_ref().held_sends, handoff.held_sends[1:])
                )
                self.store.write(record.model_copy_update(to_update(record.field_ref().handoff, remaining)))
                return handoff.held_sends[0]
            self.store.write(record.model_copy_update(to_update(record.field_ref().handoff, None)))
            return None

    def get_agent_state(self, agent_id: str) -> AgentStateItem | None:
        state = self.agents.get(agent_id)
        if state is None:
            return None
        return state.model_copy_update(
            to_update(state.field_ref().activity_state, self.activity_by_agent.get(agent_id))
        )

    def get_agent_info(self, agent_id: str) -> AgentInfo | None:
        state = self.agents.get(agent_id)
        if state is None:
            return None
        return AgentInfo(
            id=agent_id,
            name=state.name,
            state=state.state,
            agent_state_dir=self.tmp_path / "agents" / agent_id,
            claude_config_dir=self.tmp_path / "claude",
            labels=state.labels,
            harness=state.harness,
        )

    def deliver(self, agent_info: AgentInfo, text: str, message_id: str) -> SendOutcome:
        if self.is_delivery_refused:
            raise SendFailedError("the agent is in shell mode", kind="INPUT_BLOCKED")
        self.delivered.append((agent_info.id, text, message_id))
        if text.startswith("/handoff-summary ") and self.is_summary_written_on_request:
            path = Path(text.split(" ", 1)[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Summary\n\nThe user wants the tests green.\n")
        return SendOutcome.OK

    def drain_to_composer(self, agent_info: AgentInfo) -> str:
        self.drained.append(agent_info.id)
        return self.drain_block

    def ensure_watcher(self, agent_info: AgentInfo) -> _EventsReader:
        return _EventsReader(self.events_by_agent.get(agent_info.id, []))

    def stop_agent(self, agent_info: AgentInfo) -> None:
        self.stopped.append(agent_info.id)
        state = self.agents[agent_info.id]
        self.agents[agent_info.id] = state.model_copy_update(to_update(state.field_ref().state, "STOPPED"))

    def note_agent_renamed(self, agent_id: str, name: str, labels: Mapping[str, str]) -> None:
        state = self.agents[agent_id]
        self.agents[agent_id] = state.model_copy_update(
            to_update(state.field_ref().name, name), to_update(state.field_ref().labels, {**state.labels, **labels})
        )

    def note_agent_created(self, agent_state: AgentStateItem) -> None:
        self.agents[agent_state.id] = agent_state

    def build_create_command(self, spec: SuccessorCreateSpec) -> list[str]:
        return _build_chat_create_command(
            str(self.tmp_path / "fake-mngr"),
            spec.name,
            spec.chat_id,
            spec.agent_id,
            {},
            spec.harness,
            (),
            spec.project_id,
            spec.account_args,
            extra_labels=spec.extra_labels,
            message_file=spec.message_file,
        )

    def broadcast(self, chat_id: ChatId, events: list[dict[str, Any]]) -> None:
        self.broadcasts.append((str(chat_id), events))

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def argv_lines(self) -> list[str]:
        return self.mngr_log.read_text().splitlines() if self.mngr_log.exists() else []


def _write_fake_mngr(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in ``mngr`` that logs its argv; ``create`` fails while ``fail-create`` exists in the
    fail dir and refuses the id once while ``dup-once`` does."""
    log = tmp_path / "mngr-argv.log"
    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()
    script = tmp_path / "fake-mngr"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "create" ]; then\n'
        f'  if [ -f "{fail_dir}/fail-create" ]; then echo "No provider account is signed in" >&2; exit 3; fi\n'
        f'  if [ -f "{fail_dir}/dup-once" ]; then rm "{fail_dir}/dup-once"; '
        'echo "DuplicateAgentIdOnHostError: an agent with that id exists" >&2; exit 1; fi\n'
        "fi\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return log, fail_dir


def _workspace(tmp_path: Path, *, phase: HandoffPhase = HandoffPhase.DRAINING) -> tuple[_FakeWorkspace, str, str]:
    """A claude chat of one agent with a handoff to codex written in ``phase``; returns it with the two agent ids."""
    log, fail_dir = _write_fake_mngr(tmp_path)
    first = f"agent-{uuid4().hex}"
    successor = f"agent-{uuid4().hex}"
    chat_id = ChatId(first)
    workspace = _FakeWorkspace(tmp_path=tmp_path, chat_id=chat_id, mngr_log=log, fail_dir=fail_dir)
    workspace.agents[first] = AgentStateItem(
        id=first,
        name="Chat-1",
        state="RUNNING",
        labels={"display_name": "Chat 1", "account": "acct-anthropic", "project": "inbox"},
        work_dir=str(tmp_path / "work"),
        harness=HarnessType.CLAUDE,
    )
    workspace.events_by_agent[first] = [
        {"event_id": "u-1", "type": "user_message", "timestamp": "2026-09-13T11:00:00+00:00"},
        {"event_id": "a-1", "type": "assistant_message", "timestamp": "2026-09-13T11:00:05+00:00"},
        {"event_id": "u-2", "type": "user_message", "timestamp": "2026-09-13T11:30:00+00:00"},
    ]
    handoff = ChatHandoffRecord(
        handoff_id="h-1",
        phase=phase,
        started_at=_NOW,
        target_lane="openai",
        target_account_id=_OPENAI_ACCOUNT.id,
        target_harness=HarnessType.CODEX,
        retiring_seq=1,
        next_agent_id=successor,
        next_seq=2,
        chat_name="Chat-1",
        chat_title="Chat 1",
        project_label="inbox",
        trigger_message_id="m-trigger",
        held_sends=(
            HeldSend(
                message_id="m-trigger", text="Now do it in Codex", origin=HeldSendOrigin.CLIENT, received_at=_NOW
            ),
        ),
    )
    workspace.store.write(
        ChatRecord(
            chat_id=chat_id,
            agents=(
                ChatAgentEntry(
                    seq=1,
                    agent_id=first,
                    lane="anthropic",
                    account_id="acct-anthropic",
                    harness=HarnessType.CLAUDE,
                    started_at=_NOW,
                ),
            ),
            handoff=handoff,
        )
    )
    return workspace, first, successor


def _runner(workspace: _FakeWorkspace, **overrides: Any) -> HandoffRunner:
    bound: dict[str, Any] = dict(
        mngr_binary=str(workspace.tmp_path / "fake-mngr"),
        host_dir=workspace.tmp_path,
        work_dir=workspace.tmp_path / "work",
        chat_files_root=workspace.tmp_path / "chats",
        prompt_template_path=_PROMPT_TEMPLATE,
        shutdown_event=ShutdownEvent.build_root(),
        read_record=workspace.read_record,
        update_record=workspace.update_record,
        take_next_held_send=workspace.take_next_held_send,
        get_agent_state=workspace.get_agent_state,
        get_agent_info=workspace.get_agent_info,
        resolve_account=lambda account_id: _OPENAI_ACCOUNT,
        deliver=workspace.deliver,
        drain_to_composer=workspace.drain_to_composer,
        ensure_watcher=workspace.ensure_watcher,
        stop_agent=workspace.stop_agent,
        note_agent_renamed=workspace.note_agent_renamed,
        note_agent_created=workspace.note_agent_created,
        build_create_command=workspace.build_create_command,
        broadcast_transcript_events=workspace.broadcast,
        now=lambda: _NOW,
        monotonic=workspace.monotonic,
        sleep=workspace.sleep,
        summary_poll_interval_seconds=1.0,
        summary_idle_grace_seconds=3.0,
        summary_timeout_seconds=20.0,
    )
    return HandoffRunner.build(HandoffDeps(**{**bound, **overrides}))


def test_a_handoff_runs_every_phase_and_the_successor_takes_over(tmp_path: Path) -> None:
    workspace, first, successor = _workspace(tmp_path)
    workspace.drain_block = "still queued"
    workspace.mngr_log.parent.mkdir(exist_ok=True)
    (tmp_path / "work").mkdir()
    runner = _runner(workspace)
    chat_id = workspace.chat_id

    # Draining runs on the route's thread and hands the queue back.
    assert runner.drain(chat_id, "h-1") == "still queued"
    assert workspace.drained == [first]
    after_drain = workspace.record().handoff
    assert after_drain is not None and after_drain.phase is HandoffPhase.SUMMARIZING
    # A send that arrives while converging is held behind the trigger.
    workspace.update_record(
        chat_id,
        "h-1",
        lambda record: record.model_copy_update(
            to_update(
                record.field_ref().handoff,
                record.handoff.model_copy_update(
                    to_update(
                        record.handoff.field_ref().held_sends,
                        (
                            *record.handoff.held_sends,
                            HeldSend(
                                message_id="m-2", text="and also this", origin=HeldSendOrigin.SCRIPT, received_at=_NOW
                            ),
                        ),
                    )
                )
                if record.handoff is not None
                else None,
            )
        ),
    )

    runner.run(chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is None
    assert [(entry.seq, entry.agent_id, entry.harness) for entry in record.agents] == [
        (1, first, HarnessType.CLAUDE),
        (2, successor, HarnessType.CODEX),
    ]
    retired = record.agents[0]
    assert retired.ended_at == _NOW
    assert retired.archived_name == archived_agent_name(1, "Chat-1", first)
    assert retired.final_event_count == 3
    assert record.agents[1].account_id == _OPENAI_ACCOUNT.id and record.agents[1].lane == "openai"

    # The retiring agent was asked for its summary, stopped, and archived in one rename carrying every label.
    summary = summary_path(tmp_path / "chats", chat_id, 1)
    assert workspace.delivered[0] == (first, summary_request_message(summary), "handoff-summary-h-1")
    assert workspace.stopped == [first]
    argv = workspace.argv_lines()
    assert argv[0] == (
        f"rename {first} {archived_agent_name(1, 'Chat-1', first)} --label display_name=Chat 1 (archived 1) "
        f"--label chat_id={chat_id} --label chat_seq=1 --label archived_at={_NOW.isoformat()}"
    )
    assert workspace.agents[first].name == archived_agent_name(1, "Chat-1", first)
    assert workspace.agents[first].labels["archived_at"] == _NOW.isoformat()
    # The successor is created under its pre-minted id, with the chat's name, membership, account, and the prompt file.
    create = argv[1].split(" ")
    assert create[:3] == ["create", "Chat-1", "--id"] and create[3] == successor
    assert "--type codex" in argv[1]
    assert f"--label chat_id={chat_id} --label chat_seq=2" in argv[1]
    assert f"--label account={_OPENAI_ACCOUNT.id}" in argv[1]
    assert "--label project=inbox" in argv[1]
    prompt_file = prompt_path(tmp_path / "chats", chat_id, 2)
    assert argv[1].endswith(f"--message-file {prompt_file}")
    prompt = prompt_file.read_text()
    assert "Now do it in Codex" in prompt
    assert f"summary is at {summary}" in prompt
    assert "${" not in prompt
    assert successor in workspace.agents and workspace.agents[successor].labels["chat_seq"] == "2"
    # The chip went out on the chat's stream, and the held send followed the prompt to the successor.
    assert [(chat, [event["type"] for event in events]) for chat, events in workspace.broadcasts] == [
        (str(chat_id), [AGENT_SWITCH_EVENT_TYPE])
    ]
    switch = workspace.broadcasts[0][1][0]
    assert (switch["from_agent_id"], switch["to_agent_id"], switch["to_harness"]) == (first, successor, "codex")
    assert workspace.delivered[1:] == [(successor, "and also this", "m-2")]


def test_a_fresh_summary_is_reused_and_a_stale_one_is_asked_for_again(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    summary = summary_path(tmp_path / "chats", workspace.chat_id, 1)
    summary.parent.mkdir(parents=True)
    summary.write_text("# earlier summary\n")
    runner = _runner(workspace)
    (tmp_path / "work").mkdir()

    runner.run(workspace.chat_id, "h-1")

    # Written now, after the last user turn: nothing was asked, and the prompt points at it.
    assert not any(text.startswith("/handoff-summary") for _agent, text, _id in workspace.delivered)
    assert f"summary is at {summary}" in prompt_path(tmp_path / "chats", workspace.chat_id, 2).read_text()

    stale = last_user_turn_epoch(workspace.events_by_agent[first])
    assert stale is not None
    assert is_summary_fresh(stale - 1.0, stale) is False
    assert is_summary_fresh(stale + 1.0, stale) is True
    assert is_summary_fresh(None, stale) is False
    assert is_summary_fresh(1.0, None) is True


def test_a_turn_that_ends_without_a_summary_moves_on_and_the_prompt_says_so(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
    workspace.activity_by_agent[first] = ActivityState.IDLE
    (tmp_path / "work").mkdir()
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is None
    # The request landed, the agent went idle with no file, and the wait ended at the grace period.
    assert workspace.delivered[0][1].startswith("/handoff-summary ")
    assert workspace.clock == pytest.approx(3.0)
    assert "did not produce a summary" in prompt_path(tmp_path / "chats", workspace.chat_id, 2).read_text()


def test_a_busy_agent_is_waited_for_until_it_goes_idle(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
    (tmp_path / "work").mkdir()
    # Busy for the first polls, then idle: the idle reading after a busy one ends the wait at once.
    readings = iter([ActivityState.THINKING, ActivityState.TOOL_RUNNING, ActivityState.IDLE])

    def get_agent_state(agent_id: str) -> AgentStateItem | None:
        state = workspace.get_agent_state(agent_id)
        if state is None or agent_id != first:
            return state
        return state.model_copy_update(to_update(state.field_ref().activity_state, next(readings, ActivityState.IDLE)))

    runner = _runner(workspace, get_agent_state=get_agent_state)
    runner.run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert workspace.clock == pytest.approx(2.0)


def test_a_refused_summary_request_is_a_missing_summary_not_a_stuck_handoff(tmp_path: Path) -> None:
    workspace, _first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_delivery_refused = True
    (tmp_path / "work").mkdir()

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert "did not produce a summary" in prompt_path(tmp_path / "chats", workspace.chat_id, 2).read_text()


def test_a_failed_create_leaves_the_failed_phase_with_the_reason_and_a_retry_reuses_the_prompt(tmp_path: Path) -> None:
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    (tmp_path / "work").mkdir()
    (workspace.fail_dir / "fail-create").write_text("")
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is not None
    assert record.handoff.phase is HandoffPhase.FAILED
    assert record.handoff.error is not None
    assert "exited with code 3" in record.handoff.error and "No provider account is signed in" in record.handoff.error
    # The retiring agent is archived and measured; only the successor is missing.
    assert record.agents[0].archived_name == archived_agent_name(1, "Chat-1", first)
    assert successor not in workspace.agents
    prompt_before = prompt_path(tmp_path / "chats", workspace.chat_id, 2).read_text()

    # A retry (what the route writes) runs the create again with the same prompt and nothing else.
    (workspace.fail_dir / "fail-create").unlink()
    workspace.update_record(
        workspace.chat_id,
        "h-1",
        lambda current: current.model_copy_update(
            to_update(
                current.field_ref().handoff,
                current.handoff.model_copy_update(
                    to_update(current.handoff.field_ref().phase, HandoffPhase.SWITCHING),
                    to_update(current.handoff.field_ref().error, None),
                )
                if current.handoff is not None
                else None,
            )
        ),
    )
    argv_before = workspace.argv_lines()
    runner.run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    new_argv = workspace.argv_lines()[len(argv_before) :]
    assert [line.split(" ")[0] for line in new_argv] == ["create"]
    assert prompt_path(tmp_path / "chats", workspace.chat_id, 2).read_text() == prompt_before
    assert workspace.agents[successor].harness is HarnessType.CODEX


def test_a_half_made_successor_is_destroyed_and_created_again(tmp_path: Path) -> None:
    workspace, _first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    (tmp_path / "work").mkdir()
    (workspace.fail_dir / "dup-once").write_text("")

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    verbs = [line.split(" ")[0] for line in workspace.argv_lines()]
    assert verbs == ["rename", "create", "destroy", "create"]
    assert f"destroy {successor} --force" in workspace.argv_lines()
    assert is_duplicate_id_refusal("An agent with id 'agent-x' already exists on host h", "agent-x")
    assert not is_duplicate_id_refusal("something else went wrong", "agent-x")


def test_a_cancelled_handoff_stops_the_runner_before_switching(tmp_path: Path) -> None:
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
    (tmp_path / "work").mkdir()
    record = workspace.record()

    # The cancel route clears the handoff while the runner waits for the summary.
    def sleep_then_cancel(seconds: float) -> None:
        workspace.clock += seconds
        workspace.store.write(record.model_copy_update(to_update(record.field_ref().handoff, None)))

    _runner(workspace, sleep=sleep_then_cancel).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert workspace.stopped == [] and workspace.argv_lines() == []
    assert successor not in workspace.agents and workspace.agents[first].name == "Chat-1"


def _track_archived_retiring_and_running_successor(workspace: _FakeWorkspace, first: str, successor: str) -> str:
    """What mngr shows after the switch's stop, rename, and create: the first agent stopped under its
    archival name and the successor running. Returns the archival name."""
    archival = archived_agent_name(1, "Chat-1", first)
    workspace.agents[first] = workspace.agents[first].model_copy_update(
        to_update(workspace.agents[first].field_ref().name, archival),
        to_update(workspace.agents[first].field_ref().state, "STOPPED"),
    )
    workspace.agents[successor] = AgentStateItem(
        id=successor,
        name="Chat-1",
        state="RUNNING",
        labels={"chat_seq": "2"},
        work_dir=None,
        harness=HarnessType.CODEX,
    )
    return archival


def test_a_resumed_switch_finds_its_earlier_steps_done_and_adopts_the_successor(tmp_path: Path) -> None:
    """A restart mid-switch: the retiring agent is already archived and the successor's create
    landed without this process seeing it, so the resume renames and creates nothing."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SWITCHING)
    (tmp_path / "work").mkdir()
    archival = _track_archived_retiring_and_running_successor(workspace, first, successor)
    record = workspace.record()
    assert record.handoff is not None
    workspace.store.write(
        record.model_copy_update(
            to_update(
                record.field_ref().handoff,
                record.handoff.model_copy_update(
                    to_update(record.handoff.field_ref().prompt, "the stored prompt"),
                    to_update(record.handoff.field_ref().summary_outcome, SummaryOutcome.WRITTEN),
                    # Summarizing already folded the trigger into the prompt.
                    to_update(record.handoff.field_ref().held_sends, ()),
                ),
            )
        )
    )

    _runner(workspace).run(workspace.chat_id, "h-1")

    finished = workspace.record()
    assert finished.handoff is None
    assert [entry.agent_id for entry in finished.agents] == [first, successor]
    assert finished.agents[0].archived_name == archival and finished.agents[0].final_event_count == 3
    assert workspace.argv_lines() == [] and workspace.stopped == []
    # The adopted agent got the prompt from its own create; nothing else was held for it.
    assert workspace.delivered == []


def test_a_resume_mid_delivery_delivers_what_is_still_held_and_touches_neither_agent(tmp_path: Path) -> None:
    """A restart after the successor was appended to the record but before every held send reached
    it: the switch is not run again on the successor (the record's last entry); only the delivery is."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SWITCHING)
    (tmp_path / "work").mkdir()
    archival = _track_archived_retiring_and_running_successor(workspace, first, successor)
    record = workspace.record()
    assert record.handoff is not None
    retired = record.agents[0].model_copy_update(
        to_update(record.agents[0].field_ref().ended_at, _NOW),
        to_update(record.agents[0].field_ref().archived_name, archival),
        to_update(record.agents[0].field_ref().final_event_count, 3),
    )
    appended = ChatAgentEntry(
        seq=2,
        agent_id=successor,
        lane="openai",
        account_id=_OPENAI_ACCOUNT.id,
        harness=HarnessType.CODEX,
        started_at=_NOW,
    )
    late = HeldSend(message_id="m-late", text="one more", origin=HeldSendOrigin.SCRIPT, received_at=_NOW)
    workspace.store.write(
        record.model_copy_update(
            to_update(record.field_ref().agents, (retired, appended)),
            to_update(
                record.field_ref().handoff,
                record.handoff.model_copy_update(
                    to_update(record.handoff.field_ref().prompt, "the stored prompt"),
                    to_update(record.handoff.field_ref().summary_outcome, SummaryOutcome.WRITTEN),
                    to_update(record.handoff.field_ref().held_sends, (late,)),
                ),
            ),
        )
    )

    _runner(workspace).run(workspace.chat_id, "h-1")

    finished = workspace.record()
    assert finished.handoff is None
    assert finished.agents == (retired, appended)
    assert workspace.stopped == [] and workspace.argv_lines() == [] and workspace.broadcasts == []
    assert (workspace.agents[successor].state, workspace.agents[successor].name) == ("RUNNING", "Chat-1")
    assert workspace.delivered == [(successor, "one more", "m-late")]


def test_a_runner_for_a_handoff_that_is_gone_does_nothing(tmp_path: Path) -> None:
    workspace, _first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    (tmp_path / "work").mkdir()

    _runner(workspace).run(workspace.chat_id, "another-handoff")

    assert workspace.delivered == [] and workspace.argv_lines() == []
    assert workspace.record().handoff is not None
