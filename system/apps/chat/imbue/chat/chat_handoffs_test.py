import os
import threading
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.accounts import Account
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.agent_manager import _account_binding_args
from imbue.chat.agent_manager import _build_chat_create_command
from imbue.chat.chat_handoffs import HandoffCancelledError
from imbue.chat.chat_handoffs import HandoffDeps
from imbue.chat.chat_handoffs import HandoffRunner
from imbue.chat.chat_handoffs import SuccessorCreateSpec
from imbue.chat.chat_handoffs import archive_rename_command
from imbue.chat.chat_handoffs import archived_agent_name
from imbue.chat.chat_handoffs import has_user_turn
from imbue.chat.chat_handoffs import is_duplicate_id_refusal
from imbue.chat.chat_handoffs import is_summary_fresh
from imbue.chat.chat_handoffs import is_summary_written
from imbue.chat.chat_handoffs import last_user_turn_epoch
from imbue.chat.chat_handoffs import mngr_failure_reason
from imbue.chat.chat_handoffs import prompt_message_id
from imbue.chat.chat_handoffs import summary_path
from imbue.chat.chat_handoffs import summary_request_message
from imbue.chat.chat_records import ChatAgentEntry
from imbue.chat.chat_records import ChatHandoffRecord
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.chat_transcript import AGENT_SWITCH_EVENT_TYPE
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.message_display import HANDOFF_SUMMARY_COMMAND
from imbue.chat.harnesses.mock_transcript_reader_test import ListTranscriptReader
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.models import ActivityState
from imbue.chat.models import AgentStateItem
from imbue.chat.models import HandoffFailedStep
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.models import HeldSendOrigin
from imbue.chat.models import ModelApplyError
from imbue.chat.models import ModelPick
from imbue.chat.models import SummaryOutcome
from imbue.chat.primitives import ChatId
from imbue.chat.testing import CONTINUE_CHAT_TEMPLATE_PATH
from imbue.chat.testing import write_summary_for_request
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
_OPENAI_ACCOUNT = Account(id="acct-openai", lane="openai", seq=1, display="OpenAI")
_PICK = ModelPick(model_id="gpt-6-astra", effort="high", fast=False)


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
    applied: list[tuple[str, ModelPick]] = Field(default_factory=list)
    # Every deliver and apply in the order they happened, so a test can assert the pick landed
    # before the successor's first message.
    steps: list[str] = Field(default_factory=list)
    broadcasts: list[tuple[str, list[dict[str, Any]]]] = Field(default_factory=list)
    drained: list[str] = Field(default_factory=list)
    stopped: list[str] = Field(default_factory=list)
    drain_block: str = ""
    # What ``deliver`` does with the summary request: write the file, or nothing.
    is_summary_written_on_request: bool = True
    # Whether the summary request is refused (the agent is blocked on a dialog, say); every other
    # send lands, so the successor's prompt can still be read off ``delivered``.
    is_summary_request_refused: bool = False
    is_model_apply_refused: bool = False
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
        if self.is_summary_request_refused and text.startswith(HANDOFF_SUMMARY_COMMAND):
            raise SendFailedError("the agent is in shell mode", kind="INPUT_BLOCKED")
        self.delivered.append((agent_info.id, text, message_id))
        self.steps.append(f"deliver:{message_id}")
        if self.is_summary_written_on_request:
            write_summary_for_request(text)
        return SendOutcome.OK

    def apply_model(self, agent_info: AgentInfo, pick: ModelPick) -> None:
        if self.is_model_apply_refused:
            raise ModelApplyError(f"Unknown model '{pick.model_id}'")
        self.applied.append((agent_info.id, pick))
        self.steps.append("apply")

    def delivered_prompt(self, handoff_id: str = "h-1") -> str:
        """The handoff prompt the successor received, the one send under the prompt's own id."""
        texts = [text for _agent, text, message_id in self.delivered if message_id == prompt_message_id(handoff_id)]
        assert len(texts) == 1, texts
        return texts[0]

    def drain_to_composer(self, agent_info: AgentInfo) -> str:
        self.drained.append(agent_info.id)
        return self.drain_block

    def ensure_watcher(self, agent_info: AgentInfo) -> _EventsReader:
        return _EventsReader(self.events_by_agent.get(agent_info.id, []))

    def stop_agent(self, agent_info: AgentInfo) -> None:
        self.stopped.append(agent_info.id)
        state = self.agents[agent_info.id]
        self.agents[agent_info.id] = state.model_copy_update(to_update(state.field_ref().state, "STOPPED"))

    def destroy_agent(self, agent_id: str) -> None:
        # Logged in the fake mngr's own line shape, so a test reads the whole switch off one log.
        with self.mngr_log.open("a") as log:
            log.write(f"destroy {agent_id} --force\n")
        self.agents.pop(agent_id, None)

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
            _account_binding_args(spec.harness, spec.account_id, self.tmp_path / "agents" / spec.agent_id),
            extra_labels=spec.extra_labels,
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
    # The successor's create runs in the primary agent's work dir, which has to exist.
    (tmp_path / "work").mkdir()
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
        trigger_text="Now do it in Codex",
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
        prompt_template_path=CONTINUE_CHAT_TEMPLATE_PATH,
        shutdown_event=ShutdownEvent.build_root(),
        read_record=workspace.read_record,
        update_record=workspace.update_record,
        take_next_held_send=workspace.take_next_held_send,
        get_agent_state=workspace.get_agent_state,
        get_agent_info=workspace.get_agent_info,
        resolve_account=lambda account_id: _OPENAI_ACCOUNT,
        deliver=workspace.deliver,
        apply_model=workspace.apply_model,
        drain_to_composer=workspace.drain_to_composer,
        ensure_watcher=workspace.ensure_watcher,
        stop_agent=workspace.stop_agent,
        destroy_agent=workspace.destroy_agent,
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


def test_the_archival_rename_argv_is_accepted_by_the_live_cli() -> None:
    """The archive is one rename carrying every label, checked against the vendored mngr like the manager's argvs."""
    argv = archive_rename_command(
        "mngr",
        "agent-123",
        archived_agent_name(1, "Chat-1", "agent-123"),
        {
            "display_name": "Chat 1 (archived 1)",
            "chat_id": "agent-123",
            "chat_seq": "1",
            "archived_at": _NOW.isoformat(),
        },
    )
    assert_mngr_argv_valid(argv)
    assert argv[:4] == ["mngr", "rename", "agent-123", "archived-1-Chat-1-agent-123"]
    assert [argv[i + 1] for i, token in enumerate(argv) if token == "--label"] == [
        "display_name=Chat 1 (archived 1)",
        "chat_id=agent-123",
        "chat_seq=1",
        f"archived_at={_NOW.isoformat()}",
    ]


def _finished_rename(returncode: int, stderr: str = "", is_timed_out: bool = False) -> FinishedProcess:
    return FinishedProcess(
        returncode=returncode,
        stdout="",
        stderr=stderr,
        command=("mngr", "rename"),
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


def test_a_failed_mngr_verb_names_its_reason_even_when_mngr_printed_nothing() -> None:
    """The step error is the one trace of why a switch stalled, so a timeout or a signal is named
    rather than reported as an empty stderr."""
    assert mngr_failure_reason("rename", _finished_rename(1, stderr="No agent named x\n"), 30.0) == "No agent named x"
    assert mngr_failure_reason("rename", _finished_rename(-15, is_timed_out=True, stderr="killed\n"), 30.0) == (
        "mngr rename did not finish within 30s and was stopped"
    )
    assert mngr_failure_reason("rename", _finished_rename(-9), 30.0) == "mngr rename was stopped by signal 9"
    assert mngr_failure_reason("label", _finished_rename(2), 30.0) == "mngr label exited with code 2"


def test_a_handoff_runs_every_phase_and_the_successor_takes_over(tmp_path: Path) -> None:
    workspace, first, successor = _workspace(tmp_path)
    workspace.drain_block = "still queued"
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
    # The successor is created under its pre-minted id, with the chat's name, membership, and account, and
    # silent: the prompt follows through the send path, so a model pick can land before the first turn.
    create = argv[1].split(" ")
    assert create[:3] == ["create", "Chat-1", "--id"] and create[3] == successor
    assert "--type codex" in argv[1]
    assert f"--label chat_id={chat_id} --label chat_seq=2" in argv[1]
    assert f"--label account={_OPENAI_ACCOUNT.id}" in argv[1]
    assert "--label project=inbox" in argv[1]
    assert "--message" not in argv[1]
    prompt = workspace.delivered_prompt()
    assert "Now do it in Codex" in prompt
    assert f"also on disk at {summary}" in prompt
    assert "<predecessor-summary>\n# Summary\n\nThe user wants the tests green.\n</predecessor-summary>" in prompt
    assert "${" not in prompt
    assert successor in workspace.agents and workspace.agents[successor].labels["chat_seq"] == "2"
    # The chip went out on the chat's stream, and the held send followed the prompt to the successor.
    assert [(chat, [event["type"] for event in events]) for chat, events in workspace.broadcasts] == [
        (str(chat_id), [AGENT_SWITCH_EVENT_TYPE])
    ]
    switch = workspace.broadcasts[0][1][0]
    assert (switch["from_agent_id"], switch["to_agent_id"], switch["to_harness"]) == (first, successor, "codex")
    # The message the user switched with rides inside the prompt, so the chip carries it for the page,
    # and the record keeps it for every later read.
    assert (switch["message_id"], switch["message"]) == ("m-trigger", "Now do it in Codex")
    adopted = workspace.record().agents[-1]
    assert (adopted.opening_message_id, adopted.opening_message) == ("m-trigger", "Now do it in Codex")
    assert workspace.delivered[1:] == [
        (successor, prompt, prompt_message_id("h-1")),
        (successor, "and also this", "m-2"),
    ]
    # No pick was made, so the successor keeps its harness's default.
    assert workspace.applied == []


def test_a_fresh_summary_is_reused_and_a_stale_one_is_asked_for_again(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    summary = summary_path(tmp_path / "chats", workspace.chat_id, 1)
    summary.parent.mkdir(parents=True)
    summary.write_text("# earlier summary\n")
    stale = last_user_turn_epoch(workspace.events_by_agent[first])
    assert stale is not None
    # Written a minute after the last user turn, whatever the clock says today.
    os.utime(summary, (stale + 60.0, stale + 60.0))
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    # Fresh: nothing was asked, and the prompt carries it.
    assert not any(text.startswith("/handoff-summary") for _agent, text, _id in workspace.delivered)
    assert f"also on disk at {summary}" in workspace.delivered_prompt()
    assert "<predecessor-summary>\n# earlier summary\n</predecessor-summary>" in workspace.delivered_prompt()

    assert is_summary_fresh(stale - 1.0, stale) is False
    assert is_summary_fresh(stale + 1.0, stale) is True
    assert is_summary_fresh(None, stale) is False
    assert is_summary_fresh(1.0, None) is True


def test_a_stale_summary_is_not_taken_for_the_one_just_requested(tmp_path: Path) -> None:
    """A summary older than the last user turn stays at the path while a new one is asked for; the
    wait must not proceed on it, only on the file the agent writes in answer."""
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
    workspace.activity_by_agent[first] = ActivityState.IDLE
    summary = summary_path(tmp_path / "chats", workspace.chat_id, 1)
    summary.parent.mkdir(parents=True)
    summary.write_text("# stale summary\n")
    stale = last_user_turn_epoch(workspace.events_by_agent[first])
    assert stale is not None
    os.utime(summary, (stale - 60.0, stale - 60.0))

    _runner(workspace).run(workspace.chat_id, "h-1")

    # The request went out, the turn ended without a new file, and the successor is told so.
    assert workspace.delivered[0][1].startswith("/handoff-summary ")
    assert workspace.clock == pytest.approx(3.0)
    assert "did not produce a summary" in workspace.delivered_prompt()
    assert is_summary_written(stale - 60.0, stale - 60.0) is False
    assert is_summary_written(None, stale - 60.0) is False
    assert is_summary_written(stale + 1.0, stale - 60.0) is True
    assert is_summary_written(stale + 1.0, None) is True


def test_a_prompt_template_that_cannot_be_filled_in_leaves_the_handoff_where_it_is(tmp_path: Path) -> None:
    """The reference document is editable; one with an unknown placeholder is the step that could not
    finish, not a dead thread: the record keeps its phase for a resume once the template is repaired."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    template = tmp_path / "continue-chat.md"
    template.write_text("Continue ${title}; the summary is ${summary}; ${no_such_placeholder}.\n")

    _runner(workspace, prompt_template_path=template).run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is not None and record.handoff.phase is HandoffPhase.SUMMARIZING
    assert record.handoff.prompt is None
    assert workspace.stopped == [] and workspace.argv_lines() == []
    assert successor not in workspace.agents and workspace.agents[first].name == "Chat-1"


def test_the_prompt_names_an_earlier_predecessor_by_the_archival_name_it_was_given(tmp_path: Path) -> None:
    """A chat rename between two handoffs renames the active agent only, so an earlier member keeps the
    archival name recorded on its entry; the prompt has to say that name, and derive one from the current
    chat name only for the agent retiring now, which is not archived yet when the prompt is rendered."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    second = f"agent-{uuid4().hex}"
    workspace.agents[second] = workspace.agents[first].model_copy_update(
        to_update(workspace.agents[first].field_ref().id, second)
    )
    workspace.events_by_agent[second] = workspace.events_by_agent[first]
    record = workspace.record()
    assert record.handoff is not None
    retired_first = record.agents[0].model_copy_update(
        to_update(record.agents[0].field_ref().ended_at, _NOW),
        to_update(record.agents[0].field_ref().archived_name, archived_agent_name(1, "Old-Name", first)),
        to_update(record.agents[0].field_ref().final_event_count, 2),
    )
    live_second = ChatAgentEntry(
        seq=2,
        agent_id=second,
        lane="anthropic",
        account_id="acct-anthropic",
        harness=HarnessType.CLAUDE,
        started_at=_NOW,
    )
    workspace.store.write(
        record.model_copy_update(
            to_update(record.field_ref().agents, (retired_first, live_second)),
            to_update(
                record.field_ref().handoff,
                record.handoff.model_copy_update(
                    to_update(record.handoff.field_ref().retiring_seq, 2),
                    to_update(record.handoff.field_ref().next_seq, 3),
                ),
            ),
        )
    )

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    prompt = workspace.delivered_prompt()
    assert f"- seq 1: {archived_agent_name(1, 'Old-Name', first)}, id {first}" in prompt
    assert f"- seq 2: {archived_agent_name(2, 'Chat-1', second)}, id {second}" in prompt
    assert archived_agent_name(1, "Chat-1", first) not in prompt
    assert successor in workspace.agents


def test_a_turn_that_ends_without_a_summary_moves_on_and_the_prompt_says_so(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
    workspace.activity_by_agent[first] = ActivityState.IDLE
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is None
    # The request landed, the agent went idle with no file, and the wait ended at the grace period.
    assert workspace.delivered[0][1].startswith("/handoff-summary ")
    assert workspace.clock == pytest.approx(3.0)
    assert "did not produce a summary" in workspace.delivered_prompt()


def test_a_busy_agent_is_waited_for_until_it_goes_idle(tmp_path: Path) -> None:
    workspace, first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_summary_written_on_request = False
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
    workspace.is_summary_request_refused = True

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert "did not produce a summary" in workspace.delivered_prompt()


def test_a_failed_create_leaves_the_failed_phase_with_the_reason_and_a_retry_reuses_the_prompt(tmp_path: Path) -> None:
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    (workspace.fail_dir / "fail-create").write_text("")
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is not None
    assert record.handoff.phase is HandoffPhase.FAILED
    assert record.handoff.failed_step is HandoffFailedStep.START
    assert record.handoff.error is not None
    assert "exited with code 3" in record.handoff.error and "No provider account is signed in" in record.handoff.error
    # The retiring agent is archived and measured; only the successor is missing.
    assert record.agents[0].archived_name == archived_agent_name(1, "Chat-1", first)
    assert successor not in workspace.agents
    prompt_before = record.handoff.prompt
    assert prompt_before is not None and "Now do it in Codex" in prompt_before

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
    assert workspace.delivered_prompt() == prompt_before
    assert workspace.agents[successor].harness is HarnessType.CODEX


def test_a_model_pick_is_applied_to_the_successor_before_its_first_message(tmp_path: Path) -> None:
    """The pick governs the whole segment: it lands on the created, still silent successor, and only then
    does the prompt go out."""
    workspace, _first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.update_record(
        workspace.chat_id,
        "h-1",
        lambda current: current.model_copy_update(
            to_update(
                current.field_ref().handoff,
                current.handoff.model_copy_update(to_update(current.handoff.field_ref().model_pick, _PICK))
                if current.handoff is not None
                else None,
            )
        ),
    )

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert workspace.applied == [(successor, _PICK)]
    assert workspace.steps.index("apply") < workspace.steps.index(f"deliver:{prompt_message_id('h-1')}")


def test_a_refused_model_pick_fails_the_switch_at_that_step_and_a_retry_adopts_the_successor(tmp_path: Path) -> None:
    """The successor exists but is not the chat's yet; the failed page names the pick; a retry on the same
    account reruns only the pick and the delivery, with no second create."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.is_model_apply_refused = True
    workspace.update_record(
        workspace.chat_id,
        "h-1",
        lambda current: current.model_copy_update(
            to_update(
                current.field_ref().handoff,
                current.handoff.model_copy_update(to_update(current.handoff.field_ref().model_pick, _PICK))
                if current.handoff is not None
                else None,
            )
        ),
    )
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "h-1")

    record = workspace.record()
    assert record.handoff is not None
    assert record.handoff.phase is HandoffPhase.FAILED
    assert record.handoff.failed_step is HandoffFailedStep.MODEL
    assert record.handoff.error == "Unknown model 'gpt-6-astra'"
    # The successor was created and is tracked, but the chat still lists only its retiring agent, and
    # nothing reached the successor.
    assert successor in workspace.agents
    assert [entry.agent_id for entry in record.agents] == [first]
    assert [message_id for _agent, _text, message_id in workspace.delivered] == ["handoff-summary-h-1"]
    assert workspace.broadcasts == []

    workspace.is_model_apply_refused = False
    workspace.update_record(
        workspace.chat_id,
        "h-1",
        lambda current: current.model_copy_update(
            to_update(
                current.field_ref().handoff,
                current.handoff.model_copy_update(
                    to_update(current.handoff.field_ref().phase, HandoffPhase.SWITCHING),
                    to_update(current.handoff.field_ref().error, None),
                    to_update(current.handoff.field_ref().failed_step, None),
                )
                if current.handoff is not None
                else None,
            )
        ),
    )
    argv_before = workspace.argv_lines()
    runner.run(workspace.chat_id, "h-1")

    finished = workspace.record()
    assert finished.handoff is None
    assert [entry.agent_id for entry in finished.agents] == [first, successor]
    assert workspace.argv_lines() == argv_before
    assert workspace.applied == [(successor, _PICK)]
    assert workspace.delivered[-1] == (successor, workspace.delivered_prompt(), prompt_message_id("h-1"))


def test_a_fresh_start_asks_for_no_summary_and_hands_the_successor_the_message_as_is(tmp_path: Path) -> None:
    """A retiring agent that never received a user turn has no context to carry: no summary request, no
    prompt, and the confirming message (when there is one) reaches the successor as an ordinary send."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
    workspace.update_record(
        workspace.chat_id,
        "h-1",
        lambda current: current.model_copy_update(
            to_update(
                current.field_ref().handoff,
                current.handoff.model_copy_update(to_update(current.handoff.field_ref().is_fresh_start, True))
                if current.handoff is not None
                else None,
            )
        ),
    )

    _runner(workspace).run(workspace.chat_id, "h-1")

    finished = workspace.record()
    assert finished.handoff is None
    assert [entry.agent_id for entry in finished.agents] == [first, successor]
    assert workspace.delivered == [(successor, "Now do it in Codex", "m-trigger")]
    assert not (tmp_path / "chats" / workspace.chat_id / "summaries").exists()
    assert [event["type"] for _chat, events in workspace.broadcasts for event in events] == [AGENT_SWITCH_EVENT_TYPE]
    # Delivered as a turn of its own, the message is not the chip's to show.
    assert workspace.broadcasts[0][1][0]["message"] is None
    assert finished.agents[-1].opening_message is None


def test_a_transcript_counts_as_having_a_user_turn_only_for_a_message_the_user_typed() -> None:
    """The fresh-start rule: the hidden ``/welcome`` and a system chip are not the user's turns."""
    welcome_only = [
        {"event_id": "u-0", "type": "user_message", "display": "hidden", "timestamp": "2026-09-13T11:00:00+00:00"},
        {"event_id": "a-0", "type": "assistant_message", "timestamp": "2026-09-13T11:00:05+00:00"},
        {"event_id": "u-chip", "type": "user_message", "display": "chip", "timestamp": "2026-09-13T11:01:00+00:00"},
    ]
    assert has_user_turn(welcome_only) is False
    assert has_user_turn([]) is False
    typed = [*welcome_only, {"event_id": "u-1", "type": "user_message", "timestamp": "2026-09-13T11:02:00+00:00"}]
    assert has_user_turn(typed) is True
    # A successor's handoff prompt is a chip on the wire but carries the user's message, so a chat
    # that has only received it still has context to hand on.
    prompted = [
        *welcome_only,
        {
            "event_id": "u-p",
            "type": "user_message",
            "display": "chip",
            "display_label": "Handoff prompt",
            "timestamp": "2026-09-13T11:03:00+00:00",
        },
    ]
    assert has_user_turn(prompted) is True


def test_a_half_made_successor_is_destroyed_and_created_again(tmp_path: Path) -> None:
    workspace, _first, successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)
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
    record = workspace.record()

    # The cancel route clears the handoff while the runner waits for the summary.
    def sleep_then_cancel(seconds: float) -> None:
        workspace.clock += seconds
        workspace.store.write(record.model_copy_update(to_update(record.field_ref().handoff, None)))

    _runner(workspace, sleep=sleep_then_cancel).run(workspace.chat_id, "h-1")

    assert workspace.record().handoff is None
    assert workspace.stopped == [] and workspace.argv_lines() == []
    assert successor not in workspace.agents and workspace.agents[first].name == "Chat-1"


def test_a_cancel_during_draining_still_returns_the_drained_queue(tmp_path: Path) -> None:
    """The queue is out of the agent once drained, so a cancel that lands meanwhile must not lose it:
    the block still comes back for the composer, and the record stays without a handoff."""
    workspace, first, _successor = _workspace(tmp_path)
    record = workspace.record()

    def drain_then_cancel(agent_info: AgentInfo) -> str:
        workspace.store.write(record.model_copy_update(to_update(record.field_ref().handoff, None)))
        return "typed while it ran"

    block = _runner(workspace, drain_to_composer=drain_then_cancel).drain(workspace.chat_id, "h-1")

    assert block == "typed while it ran"
    assert workspace.record().handoff is None
    assert workspace.agents[first].name == "Chat-1"


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
    # The adopted agent had not been prompted yet, so the resume hands it the stored prompt.
    assert workspace.delivered == [(successor, "the stored prompt", prompt_message_id("h-1"))]


def _write_record_mid_delivery(workspace: _FakeWorkspace, successor: str) -> ChatRecord:
    """The record as a process dying mid-delivery leaves it: the retiring agent closed, the successor
    appended, and one send still held for it. Returns what was written."""
    record = workspace.record()
    assert record.handoff is not None
    retired = record.agents[0].model_copy_update(
        to_update(record.agents[0].field_ref().ended_at, _NOW),
        to_update(
            record.agents[0].field_ref().archived_name, archived_agent_name(1, "Chat-1", record.agents[0].agent_id)
        ),
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
    mid_delivery = record.model_copy_update(
        to_update(record.field_ref().agents, (retired, appended)),
        to_update(
            record.field_ref().handoff,
            record.handoff.model_copy_update(
                to_update(record.handoff.field_ref().prompt, "the stored prompt"),
                to_update(record.handoff.field_ref().is_prompt_delivered, True),
                to_update(record.handoff.field_ref().summary_outcome, SummaryOutcome.WRITTEN),
                to_update(record.handoff.field_ref().held_sends, (late,)),
            ),
        ),
    )
    workspace.store.write(mid_delivery)
    return mid_delivery


def test_a_resume_mid_delivery_delivers_what_is_still_held_and_touches_neither_agent(tmp_path: Path) -> None:
    """A restart after the successor was appended to the record but before every held send reached
    it: the switch is not run again on the successor (the record's last entry); only the delivery is."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SWITCHING)
    _track_archived_retiring_and_running_successor(workspace, first, successor)
    mid_delivery = _write_record_mid_delivery(workspace, successor)

    _runner(workspace).run(workspace.chat_id, "h-1")

    finished = workspace.record()
    assert finished.handoff is None
    assert finished.agents == mid_delivery.agents
    assert workspace.stopped == [] and workspace.argv_lines() == [] and workspace.broadcasts == []
    assert (workspace.agents[successor].state, workspace.agents[successor].name) == ("RUNNING", "Chat-1")
    assert workspace.delivered == [(successor, "one more", "m-late")]


def test_a_resume_mid_delivery_keeps_the_held_sends_while_the_successor_is_untracked(tmp_path: Path) -> None:
    """A resume that runs before the observe stream lists the appended successor has nothing to deliver
    to: the sends stay on the record, in the switching phase, for the next resume."""
    workspace, first, successor = _workspace(tmp_path, phase=HandoffPhase.SWITCHING)
    _track_archived_retiring_and_running_successor(workspace, first, successor)
    del workspace.agents[successor]
    mid_delivery = _write_record_mid_delivery(workspace, successor)

    _runner(workspace).run(workspace.chat_id, "h-1")

    assert workspace.record() == mid_delivery
    assert workspace.delivered == [] and workspace.argv_lines() == []


def test_a_runner_for_a_handoff_that_is_gone_does_nothing(tmp_path: Path) -> None:
    workspace, _first, _successor = _workspace(tmp_path, phase=HandoffPhase.SUMMARIZING)

    _runner(workspace).run(workspace.chat_id, "another-handoff")

    assert workspace.delivered == [] and workspace.argv_lines() == []
    assert workspace.record().handoff is not None
