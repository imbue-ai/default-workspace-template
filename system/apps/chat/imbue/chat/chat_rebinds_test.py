"""The rebind runner, driven through its phases against a fake workspace and a recording ``mngr``."""

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
from imbue.chat.accounts import AccountError
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.chat_rebinds import RebindCancelledError
from imbue.chat.chat_rebinds import RebindDeps
from imbue.chat.chat_rebinds import RebindRunner
from imbue.chat.chat_rebinds import rebind_cancel_refused_detail
from imbue.chat.chat_rebinds import relabel_account_command
from imbue.chat.chat_rebinds import start_command
from imbue.chat.chat_records import ChatAgentEntry
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.models import AgentStateItem
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.models import HeldSendOrigin
from imbue.chat.primitives import ChatId
from imbue.chat.testing import make_chat_rebind_record
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_OLD_ACCOUNT = Account(id="acct-anthropic", lane="anthropic", seq=1, display="Anthropic")
_NEW_ACCOUNT = Account(id="acct-anthropic-2", lane="anthropic", seq=2, display="Anthropic")
_SESSION_ID = "11111111-2222-4333-8444-555555555555"


class _FakeWorkspace(MutableModel):
    """The manager's side of a rebind, in memory: the record, the tracked agents, the sends, the mngr log."""

    model_config = {"arbitrary_types_allowed": True}

    tmp_path: Path
    chat_id: ChatId
    store: InMemoryChatRecordStore = Field(default_factory=InMemoryChatRecordStore)
    agents: dict[str, AgentStateItem] = Field(default_factory=dict)
    delivered: list[tuple[str, str, str]] = Field(default_factory=list)
    drained: list[str] = Field(default_factory=list)
    stopped: list[str] = Field(default_factory=list)
    evicted: list[str] = Field(default_factory=list)
    revived: list[str] = Field(default_factory=list)
    relabeled: list[tuple[str, dict[str, str]]] = Field(default_factory=list)
    drain_block: str = ""
    is_delivery_refused: bool = False
    is_target_account_gone: bool = False
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    mngr_log: Path
    fail_dir: Path

    def record(self) -> ChatRecord | None:
        return self.store.read(self.chat_id)

    def read_record(self, chat_id: ChatId) -> ChatRecord | None:
        return self.store.read(chat_id)

    def _require(self, chat_id: ChatId, rebind_id: str) -> ChatRecord:
        record = self.store.read(chat_id)
        if record is None or record.rebind is None or record.rebind.rebind_id != rebind_id:
            raise RebindCancelledError("not this rebind")
        return record

    def update_record(self, chat_id: ChatId, rebind_id: str, apply: Callable[[ChatRecord], ChatRecord]) -> ChatRecord:
        with self._lock:
            updated = apply(self._require(chat_id, rebind_id))
            self.store.write(updated)
            return updated

    def take_next_held_send(self, chat_id: ChatId, rebind_id: str) -> HeldSend | None:
        with self._lock:
            record = self._require(chat_id, rebind_id)
            rebind = record.rebind
            assert rebind is not None
            if rebind.held_sends:
                remaining = rebind.model_copy_update(to_update(rebind.field_ref().held_sends, rebind.held_sends[1:]))
                self.store.write(record.with_converging(remaining))
                return rebind.held_sends[0]
            # The manager drops a one-agent chat's record with the finished rebind.
            if len(record.agents) == 1:
                self.store.delete(chat_id)
            else:
                self.store.write(record.with_converging(None))
            return None

    def get_agent_state(self, agent_id: str) -> AgentStateItem | None:
        return self.agents.get(agent_id)

    def get_agent_info(self, agent_id: str) -> AgentInfo | None:
        state = self.agents.get(agent_id)
        if state is None:
            return None
        state_dir = self.tmp_path / "agents" / agent_id
        env_path = state_dir / "env"
        config_dir = self.tmp_path / "claude"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("CLAUDE_CONFIG_DIR="):
                    config_dir = Path(line.split("=", 1)[1])
        return AgentInfo(
            id=agent_id,
            name=state.name,
            state=state.state,
            agent_state_dir=state_dir,
            claude_config_dir=config_dir,
            labels=state.labels,
            harness=state.harness,
        )

    def resolve_account(self, account_id: str) -> Account:
        if self.is_target_account_gone:
            raise AccountError(f"no such account: {account_id}")
        return _NEW_ACCOUNT if account_id == _NEW_ACCOUNT.id else _OLD_ACCOUNT

    def account_dir(self, account_id: str) -> Path:
        return self.tmp_path / "accounts" / account_id

    def deliver(self, agent_info: AgentInfo, text: str, message_id: str) -> SendOutcome:
        if self.is_delivery_refused:
            raise SendFailedError("the agent is in shell mode", kind="INPUT_BLOCKED")
        self.delivered.append((agent_info.id, text, message_id))
        return SendOutcome.OK

    def drain_to_composer(self, agent_info: AgentInfo) -> str:
        self.drained.append(agent_info.id)
        return self.drain_block

    def stop_agent(self, agent_info: AgentInfo) -> None:
        self.stopped.append(agent_info.id)
        state = self.agents[agent_info.id]
        self.agents[agent_info.id] = state.model_copy_update(to_update(state.field_ref().state, "STOPPED"))

    def evict_watcher(self, agent_id: str) -> None:
        self.evicted.append(agent_id)

    def note_agent_relabeled(self, agent_id: str, labels: Mapping[str, str]) -> None:
        self.relabeled.append((agent_id, dict(labels)))
        state = self.agents[agent_id]
        self.agents[agent_id] = state.model_copy_update(
            to_update(state.field_ref().labels, {**state.labels, **labels})
        )

    def note_agent_alive(self, agent_id: str) -> None:
        self.revived.append(agent_id)
        state = self.agents[agent_id]
        self.agents[agent_id] = state.model_copy_update(to_update(state.field_ref().state, "WAITING"))

    def argv_lines(self) -> list[str]:
        return self.mngr_log.read_text().splitlines() if self.mngr_log.exists() else []

    def env_text(self, agent_id: str) -> str:
        return (self.tmp_path / "agents" / agent_id / "env").read_text()


def _write_fake_mngr(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in ``mngr`` that logs its argv; ``start`` fails while ``fail-start`` exists in the fail dir,
    and ``label`` while ``fail-label`` does."""
    log = tmp_path / "mngr-argv.log"
    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()
    script = tmp_path / "fake-mngr"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'if [ "$1" = "start" ] && [ -f "{fail_dir}/fail-start" ]; then echo "tmux: server exited" >&2; exit 1; fi\n'
        f'if [ "$1" = "label" ] && [ -f "{fail_dir}/fail-label" ]; then echo "host lock held" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return log, fail_dir


def _workspace(
    tmp_path: Path, *, phase: HandoffPhase = HandoffPhase.DRAINING, harness: HarnessType = HarnessType.CLAUDE
) -> tuple[_FakeWorkspace, str]:
    """A one-agent chat with a rebind to the second Anthropic account written in ``phase``; returns it and the agent id."""
    log, fail_dir = _write_fake_mngr(tmp_path)
    agent_id = f"agent-{uuid4().hex}"
    chat_id = ChatId(agent_id)
    workspace = _FakeWorkspace(tmp_path=tmp_path, chat_id=chat_id, mngr_log=log, fail_dir=fail_dir)
    workspace.agents[agent_id] = AgentStateItem(
        id=agent_id,
        name="Chat-1",
        state="RUNNING",
        labels={"display_name": "Chat 1", "account": _OLD_ACCOUNT.id},
        work_dir=str(tmp_path / "work"),
        harness=harness,
    )
    state_dir = tmp_path / "agents" / agent_id
    state_dir.mkdir(parents=True)
    old_account_dir = workspace.account_dir(_OLD_ACCOUNT.id)
    workspace.account_dir(_NEW_ACCOUNT.id).mkdir(parents=True)
    if harness is HarnessType.CLAUDE:
        (state_dir / "env").write_text(f"MNGR_AGENT_ID={agent_id}\nCLAUDE_CONFIG_DIR={old_account_dir}\n")
        (state_dir / "claude_session_id_history").write_text(f"{_SESSION_ID} 2026-09-14\n")
        project_dir = old_account_dir / "projects" / "-home-user-workspace"
        project_dir.mkdir(parents=True)
        (project_dir / f"{_SESSION_ID}.jsonl").write_text('{"type":"user"}\n')
        (project_dir / _SESSION_ID / "subagents").mkdir(parents=True)
        (project_dir / _SESSION_ID / "subagents" / "sub-1.jsonl").write_text("{}\n")
    else:
        (old_account_dir).mkdir(parents=True, exist_ok=True)
    base = make_chat_rebind_record(agent_id=agent_id, phase=phase, target_account_id=_NEW_ACCOUNT.id)
    rebind = base.model_copy_update(to_update(base.field_ref().target_harness, harness))
    workspace.store.write(
        ChatRecord(
            chat_id=chat_id,
            agents=(
                ChatAgentEntry(
                    seq=1,
                    agent_id=agent_id,
                    lane="anthropic",
                    account_id=_OLD_ACCOUNT.id,
                    harness=harness,
                    started_at=_NOW,
                ),
            ),
            rebind=rebind,
        )
    )
    return workspace, agent_id


def _runner(workspace: _FakeWorkspace, **overrides: Any) -> RebindRunner:
    bound: dict[str, Any] = dict(
        mngr_binary=str(workspace.tmp_path / "fake-mngr"),
        shutdown_event=ShutdownEvent.build_root(),
        read_record=workspace.read_record,
        update_record=workspace.update_record,
        take_next_held_send=workspace.take_next_held_send,
        get_agent_state=workspace.get_agent_state,
        get_agent_info=workspace.get_agent_info,
        resolve_account=workspace.resolve_account,
        account_dir=workspace.account_dir,
        deliver=workspace.deliver,
        drain_to_composer=workspace.drain_to_composer,
        stop_agent=workspace.stop_agent,
        evict_watcher=workspace.evict_watcher,
        note_agent_relabeled=workspace.note_agent_relabeled,
        note_agent_alive=workspace.note_agent_alive,
        now=lambda: _NOW,
    )
    return RebindRunner.build(RebindDeps(**{**bound, **overrides}))


def test_the_relabel_and_start_argv_are_accepted_by_the_live_cli() -> None:
    assert_mngr_argv_valid(relabel_account_command("mngr", "agent-1", "acct-2"))
    assert_mngr_argv_valid(start_command("mngr", "Chat-1"))


def test_a_rebind_cancel_refusal_names_the_account() -> None:
    assert rebind_cancel_refused_detail("Anthropic 2 (Claude Code)") == (
        "This chat's switch to Anthropic 2 (Claude Code) cannot be called off: "
        "the agent restarts on the new account as soon as the switch is confirmed."
    )


def test_a_claude_rebind_moves_the_sessions_repoints_the_env_relabels_restarts_and_delivers(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path)
    workspace.drain_block = "queued one"
    runner = _runner(workspace)

    assert runner.drain(workspace.chat_id, "rebind-1") == "queued one"
    record = workspace.record()
    assert record is not None and record.rebind is not None
    assert record.rebind.phase is HandoffPhase.RESTARTING and record.rebind.returned_block == "queued one"

    runner.run(workspace.chat_id, "rebind-1")

    # The agent was stopped, its watcher dropped, and it came back on the new account.
    assert workspace.stopped == [agent_id] and workspace.evicted == [agent_id] and workspace.revived == [agent_id]
    assert [line.split(" ")[0] for line in workspace.argv_lines()] == ["label", "start"]
    assert workspace.argv_lines()[0] == f"label {agent_id} --label account={_NEW_ACCOUNT.id}"
    assert workspace.argv_lines()[1] == "start Chat-1 --no-resume"
    assert workspace.relabeled == [(agent_id, {"account": _NEW_ACCOUNT.id})]
    # The env file names the new account and keeps its other lines.
    new_dir = workspace.account_dir(_NEW_ACCOUNT.id)
    assert workspace.env_text(agent_id) == f"MNGR_AGENT_ID={agent_id}\nCLAUDE_CONFIG_DIR={new_dir}\n"
    # The session and its subagents moved with the agent, so claude resumes and the watcher still reads.
    moved = new_dir / "projects" / "-home-user-workspace"
    assert (moved / f"{_SESSION_ID}.jsonl").read_text() == '{"type":"user"}\n'
    assert (moved / _SESSION_ID / "subagents" / "sub-1.jsonl").exists()
    assert not (
        workspace.account_dir(_OLD_ACCOUNT.id) / "projects" / "-home-user-workspace" / f"{_SESSION_ID}.jsonl"
    ).exists()
    # The confirming message went through the normal send path, and the one-agent record is gone.
    assert workspace.delivered == [(agent_id, "Carry on on the other account", "trigger-1")]
    assert workspace.record() is None


def test_a_symlink_harness_rebind_repoints_the_credential_link(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING, harness=HarnessType.CODEX)
    (workspace.account_dir(_NEW_ACCOUNT.id) / "auth.json").write_text("{}")
    runner = _runner(workspace)

    runner.run(workspace.chat_id, "rebind-1")

    link = tmp_path / "agents" / agent_id / "plugin" / "codex" / "home" / "auth.json"
    assert link.is_symlink() and os.readlink(link) == str(workspace.account_dir(_NEW_ACCOUNT.id) / "auth.json")
    assert [line.split(" ")[0] for line in workspace.argv_lines()] == ["label", "start"]
    assert workspace.delivered == [(agent_id, "Carry on on the other account", "trigger-1")]


def test_a_multi_agent_chats_record_keeps_its_active_entry_on_the_new_account(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    record = workspace.record()
    assert record is not None
    earlier = f"agent-{uuid4().hex}"
    two_agents = ChatRecord(
        chat_id=ChatId(earlier),
        agents=(
            ChatAgentEntry(
                seq=1,
                agent_id=earlier,
                lane="openai",
                account_id="acct-openai",
                harness=HarnessType.CODEX,
                started_at=_NOW,
                ended_at=_NOW,
                archived_name=f"archived-1-Chat-1-{earlier}",
                final_event_count=3,
            ),
            record.agents[0].model_copy_update(to_update(record.agents[0].field_ref().seq, 2)),
        ),
        rebind=record.rebind,
    )
    workspace.store.delete(workspace.chat_id)
    workspace.chat_id = ChatId(earlier)
    workspace.store.write(two_agents)

    _runner(workspace).run(workspace.chat_id, "rebind-1")

    after = workspace.record()
    assert after is not None and after.rebind is None
    assert after.agents[1].agent_id == agent_id
    assert (after.agents[1].account_id, after.agents[1].lane) == (_NEW_ACCOUNT.id, "anthropic")


def test_a_failed_start_leaves_the_failed_phase_with_mngrs_words_and_the_binding_in_place(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    (workspace.fail_dir / "fail-start").touch()

    _runner(workspace).run(workspace.chat_id, "rebind-1")

    record = workspace.record()
    assert record is not None and record.rebind is not None
    assert record.rebind.phase is HandoffPhase.FAILED
    # The exit summary on one line, mngr's output under it, neither repeated.
    assert record.rebind.error == "mngr start exited with code 1\ntmux: server exited"
    assert workspace.delivered == [] and workspace.revived == []
    # The binding and the label already point at the new account, so a retry only has to start it.
    assert f"CLAUDE_CONFIG_DIR={workspace.account_dir(_NEW_ACCOUNT.id)}" in workspace.env_text(agent_id)
    assert workspace.agents[agent_id].labels["account"] == _NEW_ACCOUNT.id

    # The retry (the manager rewrites the phase) finds every step done and only starts and delivers.
    (workspace.fail_dir / "fail-start").unlink()
    workspace.store.write(
        record.with_converging(
            record.rebind.model_copy_update(
                to_update(record.rebind.field_ref().phase, HandoffPhase.RESTARTING),
                to_update(record.rebind.field_ref().error, None),
            )
        )
    )
    _runner(workspace).run(workspace.chat_id, "rebind-1")
    assert [line.split(" ")[0] for line in workspace.argv_lines()] == ["label", "start", "label", "start"]
    assert workspace.stopped == [agent_id]
    assert workspace.delivered == [(agent_id, "Carry on on the other account", "trigger-1")]
    assert workspace.record() is None


def test_a_refused_relabel_leaves_the_phase_for_the_next_resume(tmp_path: Path) -> None:
    workspace, _agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    (workspace.fail_dir / "fail-label").touch()

    _runner(workspace).run(workspace.chat_id, "rebind-1")

    record = workspace.record()
    assert record is not None and record.rebind is not None
    assert record.rebind.phase is HandoffPhase.RESTARTING and record.rebind.error is None
    assert [line.split(" ")[0] for line in workspace.argv_lines()] == ["label"]
    assert workspace.delivered == []


def test_a_resume_after_the_env_was_rewritten_still_finds_the_sessions_where_they_were(tmp_path: Path) -> None:
    """The previous config dir is recorded before the rewrite, so a resume does not look for the files under the new one."""
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    old_dir = workspace.account_dir(_OLD_ACCOUNT.id)
    record = workspace.record()
    assert record is not None and record.rebind is not None
    # A last process that died after recording the old dir and rewriting the env, before moving.
    workspace.store.write(
        record.with_converging(
            record.rebind.model_copy_update(
                to_update(record.rebind.field_ref().previous_claude_config_dir, str(old_dir))
            )
        )
    )
    (tmp_path / "agents" / agent_id / "env").write_text(
        f"MNGR_AGENT_ID={agent_id}\nCLAUDE_CONFIG_DIR={workspace.account_dir(_NEW_ACCOUNT.id)}\n"
    )
    workspace.agents[agent_id] = workspace.agents[agent_id].model_copy_update(
        to_update(workspace.agents[agent_id].field_ref().state, "STOPPED")
    )

    _runner(workspace).run(workspace.chat_id, "rebind-1")

    moved = workspace.account_dir(_NEW_ACCOUNT.id) / "projects" / "-home-user-workspace" / f"{_SESSION_ID}.jsonl"
    assert moved.exists()
    # A stopped agent is not stopped again.
    assert workspace.stopped == []
    assert workspace.record() is None


def test_a_gone_agent_or_account_fails_the_rebind_with_the_reason(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    workspace.is_target_account_gone = True
    _runner(workspace).run(workspace.chat_id, "rebind-1")
    record = workspace.record()
    assert record is not None and record.rebind is not None
    assert record.rebind.phase is HandoffPhase.FAILED and "is gone" in str(record.rebind.error)
    assert workspace.stopped == []

    workspace.is_target_account_gone = False
    workspace.store.write(
        record.with_converging(
            record.rebind.model_copy_update(to_update(record.rebind.field_ref().phase, HandoffPhase.RESTARTING))
        )
    )
    del workspace.agents[agent_id]
    _runner(workspace).run(workspace.chat_id, "rebind-1")
    record_after = workspace.record()
    assert record_after is not None and record_after.rebind is not None
    assert "no longer listed" in str(record_after.rebind.error)


def test_a_refused_held_send_does_not_stop_the_ones_behind_it(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    record = workspace.record()
    assert record is not None and record.rebind is not None
    second = HeldSend(message_id="m-2", text="and this", origin=HeldSendOrigin.SCRIPT, received_at=_NOW)
    workspace.store.write(
        record.with_converging(
            record.rebind.model_copy_update(
                to_update(record.rebind.field_ref().held_sends, (*record.rebind.held_sends, second))
            )
        )
    )
    workspace.is_delivery_refused = True

    _runner(workspace).run(workspace.chat_id, "rebind-1")

    # Both were popped (a refusal is logged, not raised), and the rebind finished.
    assert workspace.delivered == [] and workspace.record() is None


def test_a_runner_for_a_rebind_that_is_gone_does_nothing(tmp_path: Path) -> None:
    workspace, agent_id = _workspace(tmp_path, phase=HandoffPhase.RESTARTING)
    workspace.store.delete(workspace.chat_id)
    _runner(workspace).run(workspace.chat_id, "rebind-1")
    assert workspace.stopped == [] and workspace.argv_lines() == []
    with pytest.raises(RebindCancelledError):
        _runner(workspace).drain(workspace.chat_id, "rebind-1")
    assert workspace.agents[agent_id].state == "RUNNING"
