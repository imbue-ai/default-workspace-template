"""The rebind: continuing a chat on another account of the same harness and lane
(``docs/system/blueprint/chat-agent-split/`` section 6).

A rebind keeps the chat's agent and restarts it on the new account: the agent's turn is
interrupted and its queue returned to the composer (``draining``), then the agent is stopped,
its binding repointed in its own state dir, its ``account`` label rewritten, and it is started
again (``restarting``); the confirming message and anything held meanwhile are delivered
through the normal send path once it is up. Its working state lives on the chat record's
``rebind`` entry, and every step re-checks reality before acting, so a chat-app restart at any
point resumes by running the steps again. The runner here owns the steps; the manager owns the
record, the lock, and the tracked agents, and hands the runner what it needs as bound
callables (``RebindDeps``), the shape the handoff runner takes.

There is no cancel: draining runs on the request's thread and the stop follows at once, so
the confirm dialog is the last chance. A start that fails leaves the chat in the ``failed``
phase, which a retry on an account of the same harness and lane runs the restart of again.
"""

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger as _loguru_logger

from imbue.chat.accounts import Account
from imbue.chat.accounts import AccountError
from imbue.chat.activity_state import is_lifecycle_dead
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.chat_handoffs import deliver_held_send
from imbue.chat.chat_handoffs import failure_notice
from imbue.chat.chat_handoffs import joined_blocks
from imbue.chat.chat_handoffs import mngr_exit_summary
from imbue.chat.chat_records import ChatRebindRecord
from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import ChatRecordError
from imbue.chat.harnesses.binding import BindingError
from imbue.chat.harnesses.binding import rebind_agent
from imbue.chat.harnesses.claude.session_files import claude_session_ids
from imbue.chat.harnesses.claude.session_files import move_claude_sessions
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.models import AgentRestartError
from imbue.chat.models import AgentStateItem
from imbue.chat.models import AgentStopError
from imbue.chat.models import HandoffPhase
from imbue.chat.models import HeldSend
from imbue.chat.primitives import ChatId
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# How long one ``mngr label`` (a metadata write) may take.
_LABEL_TIMEOUT_SECONDS: Final[float] = 30.0
# ``mngr start`` launches the tmux session and returns without awaiting the harness, but it
# rides the CLI's startup and the host lock, which a loaded host stretches.
_START_TIMEOUT_SECONDS: Final[float] = 120.0
# How much of a failed start's output the failed phase carries.
_START_OUTPUT_TAIL_LINES: Final[int] = 20


class RebindCancelledError(RuntimeError):
    """The rebind a runner was working on is no longer the chat's (the chat was destroyed, or the app is stopping)."""


class RebindStepError(RuntimeError):
    """A step of the restart that mngr refused; the runner stops and a resume runs the step again."""


@pure
def rebind_cancel_refused_detail(target_label: str) -> str:
    """What a cancel of a rebind tells the user (the 409's ``detail``, shown as is): there is no window for one."""
    return (
        f"This chat's switch to {target_label} cannot be called off: "
        "the agent restarts on the new account as soon as the switch is confirmed."
    )


@pure
def relabel_account_command(mngr_binary: str, agent_id: str, account_id: str) -> list[str]:
    """The ``mngr label`` that records the new account on the agent. Pure argv assembly, like the manager's builders."""
    return [mngr_binary, "label", agent_id, "--label", f"account={account_id}"]


@pure
def start_command(mngr_binary: str, agent_name: str) -> list[str]:
    """The ``mngr start`` that brings the rebound agent back: no resume message, since the held sends follow."""
    return [mngr_binary, "start", agent_name, "--no-resume"]


class RebindDeps(FrozenModel):
    """Everything the runner needs from the manager and the app state, bound once."""

    model_config = {"arbitrary_types_allowed": True}

    mngr_binary: str
    shutdown_event: ShutdownEvent
    read_record: Callable[[ChatId], ChatRecord | None]
    # Replace the record under the manager's lock, given the current record; raises
    # ``RebindCancelledError`` when the record no longer carries this rebind.
    update_record: Callable[[ChatId, str, Callable[[ChatRecord], ChatRecord]], ChatRecord]
    # Pop the next held send, or clear the rebind and return None when none remain; atomic with
    # the message route's hold. Raises ``RebindCancelledError`` for another rebind.
    take_next_held_send: Callable[[ChatId, str], HeldSend | None]
    get_agent_state: Callable[[str], AgentStateItem | None]
    get_agent_info: Callable[[str], AgentInfo | None]
    resolve_account: Callable[[str], Account]
    account_dir: Callable[[str], Path]
    # The send path the message route takes, revival included; raises ``SendFailedError``.
    deliver: Callable[[AgentInfo, str, str], SendOutcome]
    # Interrupt the agent's turn and return its queue as one block (the stop button's path).
    drain_to_composer: Callable[[AgentInfo], str]
    # ``mngr stop`` plus the session's dead-lifecycle teardown, reflected in the tracked state.
    stop_agent: Callable[[AgentInfo], None]
    # Drop the agent's resident watcher, so the next read rebuilds it against the new binding.
    evict_watcher: Callable[[str], None]
    note_agent_relabeled: Callable[[str, Mapping[str, str]], None]
    note_agent_alive: Callable[[str], None]


class RebindRunner:
    """Runs one chat's rebind through its phases, each step idempotent against the agent's files and mngr's state."""

    _deps: RebindDeps

    @classmethod
    def build(cls, deps: RebindDeps) -> "RebindRunner":
        runner = cls.__new__(cls)
        runner._deps = deps
        return runner

    def _current(self, chat_id: ChatId, rebind_id: str) -> tuple[ChatRecord, ChatRebindRecord]:
        if self._deps.shutdown_event.is_set():
            raise RebindCancelledError("the chat app is shutting down; the rebind resumes on the next start")
        record = self._deps.read_record(chat_id)
        if record is None or record.rebind is None or record.rebind.rebind_id != rebind_id:
            raise RebindCancelledError(f"chat {chat_id} no longer carries rebind {rebind_id}")
        return record, record.rebind

    def _update_rebind(
        self, chat_id: ChatId, rebind_id: str, change: Callable[[ChatRebindRecord], ChatRebindRecord]
    ) -> ChatRecord:
        return self._deps.update_record(chat_id, rebind_id, lambda record: _with_rebind_changed(record, change))

    def run(self, chat_id: ChatId, rebind_id: str) -> None:
        """Take the rebind from whatever phase it is in to done or failed.

        Quiet when the rebind is gone or the app is shutting down. A step mngr refused is logged
        and left where it is: the record still carries the phase, and the next resume runs the
        step again.
        """
        try:
            self._run_phases(chat_id, rebind_id)
        except RebindCancelledError as e:
            logger.info("Rebind of chat {} stopped: {}", chat_id, e)
        except (RebindStepError, AgentStopError, BindingError, ChatRecordError, OSError) as e:
            logger.opt(exception=e).error("Rebind of chat {} could not finish its current step", chat_id)

    def _run_phases(self, chat_id: ChatId, rebind_id: str) -> None:
        is_done = False
        while not is_done:
            record, rebind = self._current(chat_id, rebind_id)
            match rebind.phase:
                case HandoffPhase.DRAINING:
                    self.drain(chat_id, rebind_id)
                case HandoffPhase.RESTARTING:
                    self._restart(chat_id, rebind_id, record, rebind)
                    is_done = True
                case HandoffPhase.FAILED:
                    is_done = True
                case HandoffPhase.SUMMARIZING | HandoffPhase.SWITCHING:
                    raise RebindStepError(
                        f"the rebind of chat {chat_id} is in the handoff-only phase {rebind.phase.value}"
                    )
                case _ as unreachable:
                    assert_never(unreachable)

    # -- draining ------------------------------------------------------------------------------

    def drain(self, chat_id: ChatId, rebind_id: str) -> str:
        """Return the agent's queue to the composer and move on to restarting.

        The stop button's own interrupt does the draining, which also waits out an in-flight
        send under the message lock (spec 5.3); a stopped agent has nothing to drain. Returns
        the block for the composer; the route answers with it, and it also stays on the record
        for a page that reloads.
        """
        record, rebind = self._current(chat_id, rebind_id)
        if rebind.phase is not HandoffPhase.DRAINING:
            return rebind.returned_block
        agent_state = self._deps.get_agent_state(rebind.agent_id)
        agent_info = self._deps.get_agent_info(rebind.agent_id)
        block = ""
        if agent_state is not None and agent_info is not None and not is_lifecycle_dead(agent_state.state):
            try:
                block = self._deps.drain_to_composer(agent_info)
            except (AgentRestartError, OSError) as e:
                # The restart stops the agent regardless; what was queued is then gone with the
                # session, which the queue contract allows, so this is logged, not fatal.
                logger.warning("Rebind of chat {}: could not drain agent {}: {}", chat_id, rebind.agent_id, e)
        self._update_rebind(
            chat_id,
            rebind_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().phase, HandoffPhase.RESTARTING),
                to_update(current.field_ref().returned_block, joined_blocks(current.returned_block, block)),
            ),
        )
        return joined_blocks(rebind.returned_block, block)

    # -- restarting ----------------------------------------------------------------------------

    def _restart(self, chat_id: ChatId, rebind_id: str, record: ChatRecord, rebind: ChatRebindRecord) -> None:
        """Stop the agent, repoint its binding, relabel it, start it, and hand it the held sends.

        Each step finds its work done or does it: a stopped agent is not stopped again, a moved
        session file is not moved again, the env line and the link are rewritten to the same
        value, ``mngr label`` merges, and ``mngr start`` is a no-op for a running agent. The
        phase outlasts the start: the record's active entry is moved to the target account only
        once the start landed, so a resume that finds it there (the last process died
        mid-delivery) has only the delivery left to do and does not restart the agent again.
        """
        agent_state = self._deps.get_agent_state(rebind.agent_id)
        agent_info = self._deps.get_agent_info(rebind.agent_id)
        if agent_state is None or agent_info is None:
            self._fail(chat_id, rebind_id, f"The chat's agent {rebind.agent_id} is no longer listed by mngr")
            return
        if record.agents[-1].account_id != rebind.target_account_id:
            if not self._restart_on_target(chat_id, rebind_id, rebind, agent_state, agent_info):
                return
        self._deliver_held_sends(chat_id, rebind_id, rebind.agent_id)

    def _restart_on_target(
        self,
        chat_id: ChatId,
        rebind_id: str,
        rebind: ChatRebindRecord,
        agent_state: AgentStateItem,
        agent_info: AgentInfo,
    ) -> bool:
        """The restart proper, through to the record naming the new account; False once the failed phase has been written."""
        try:
            account = self._deps.resolve_account(rebind.target_account_id)
        except AccountError as e:
            self._fail(chat_id, rebind_id, f"The account the chat was moving to is gone: {e}")
            return False
        if not is_lifecycle_dead(agent_state.state):
            logger.info("Rebind of chat {}: stopping agent {}", chat_id, rebind.agent_id)
            self._deps.stop_agent(agent_info)
        target_dir = self._deps.account_dir(account.id)
        if agent_state.harness is HarnessType.CLAUDE:
            self._move_claude_sessions(chat_id, rebind_id, rebind, agent_info, target_dir)
        rebind_agent(agent_state.harness, target_dir, agent_info.agent_state_dir)
        # The watcher captured the binding it was built against; evicted only once the new one
        # is on disk, so a read that rebuilt it meanwhile is dropped too and the next read
        # follows the new binding.
        self._deps.evict_watcher(rebind.agent_id)
        self._relabel(chat_id, rebind, account.id)
        if not self._start(chat_id, rebind_id, agent_info):
            return False
        self._deps.update_record(
            chat_id, rebind_id, lambda current: _with_active_entry_rebound(current, rebind.agent_id, account)
        )
        return True

    def _move_claude_sessions(
        self, chat_id: ChatId, rebind_id: str, rebind: ChatRebindRecord, agent_info: AgentInfo, target_dir: Path
    ) -> None:
        """Carry the agent's session files to the new account's tree, tracking where they are on the record.

        The files are looked for under the dir the record names (the one the agent ran under
        before, read off the env file before the env line first changes) and under the env
        file's current dir: a resume can find the env rewritten before the move ran, and a retry
        on another account finds the files under the account the failed attempt moved them to.
        The record then names the target, so the next attempt looks there.
        """
        recorded = rebind.claude_sessions_config_dir
        if recorded is None:
            recorded = str(agent_info.claude_config_dir)
            self._record_claude_sessions_dir(chat_id, rebind_id, recorded)
        session_ids = claude_session_ids(agent_info.agent_state_dir, rebind.agent_id)
        for source_dir in dict.fromkeys((Path(recorded), agent_info.claude_config_dir)):
            moved = move_claude_sessions(session_ids, source_dir, target_dir)
            if moved:
                logger.info("Rebind of chat {}: moved {} session file(s) to {}", chat_id, len(moved), target_dir)
        if Path(recorded) != target_dir:
            self._record_claude_sessions_dir(chat_id, rebind_id, str(target_dir))

    def _record_claude_sessions_dir(self, chat_id: ChatId, rebind_id: str, config_dir: str) -> None:
        self._update_rebind(
            chat_id,
            rebind_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().claude_sessions_config_dir, config_dir)
            ),
        )

    def _relabel(self, chat_id: ChatId, rebind: ChatRebindRecord, account_id: str) -> None:
        result = run_local_command_modern_version(
            command=relabel_account_command(self._deps.mngr_binary, rebind.agent_id, account_id),
            cwd=None,
            is_checked=False,
            timeout=_LABEL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            reason = result.stderr.strip() or mngr_exit_summary("label", result, _LABEL_TIMEOUT_SECONDS)
            raise RebindStepError(f"could not relabel agent {rebind.agent_id} of chat {chat_id}: {reason}")
        self._deps.note_agent_relabeled(rebind.agent_id, {"account": account_id})

    def _start(self, chat_id: ChatId, rebind_id: str, agent_info: AgentInfo) -> bool:
        """Start the rebound agent; False once the failed phase has been written."""
        result = run_local_command_modern_version(
            command=start_command(self._deps.mngr_binary, agent_info.name),
            cwd=None,
            is_checked=False,
            timeout=_START_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            tail = "\n".join(result.stderr.strip().splitlines()[-_START_OUTPUT_TAIL_LINES:])
            self._fail(
                chat_id, rebind_id, failure_notice(mngr_exit_summary("start", result, _START_TIMEOUT_SECONDS), tail)
            )
            return False
        self._deps.note_agent_alive(agent_info.id)
        return True

    def _fail(self, chat_id: ChatId, rebind_id: str, error: str) -> None:
        """The failed phase: the agent is down on its new binding, the page shows why, and a retry runs the restart again."""
        logger.warning("Rebind of chat {} failed: {}", chat_id, error)
        self._update_rebind(
            chat_id,
            rebind_id,
            lambda current: current.model_copy_update(
                to_update(current.field_ref().phase, HandoffPhase.FAILED),
                to_update(current.field_ref().error, error),
            ),
        )

    def _deliver_held_sends(self, chat_id: ChatId, rebind_id: str, agent_id: str) -> None:
        """Deliver the held sends in order, the confirming message first, then finish the rebind.

        The rebind entry is cleared only once the held list is empty, inside the same lock the
        message route appends under, so a send that arrives during delivery is delivered by
        this loop rather than overtaking one still held.
        """
        agent_info = self._deps.get_agent_info(agent_id)
        if agent_info is None:
            raise RebindStepError(
                f"agent {agent_id} of chat {chat_id} is untracked; its held sends stay on the record"
            )
        while (held := self._deps.take_next_held_send(chat_id, rebind_id)) is not None:
            deliver_held_send(self._deps.deliver, agent_info, held, chat_id)
        logger.info(
            "Rebind of chat {}: agent {} is back on account {}", chat_id, agent_id, agent_info.labels.get("account")
        )


@pure
def _with_rebind_changed(record: ChatRecord, change: Callable[[ChatRebindRecord], ChatRebindRecord]) -> ChatRecord:
    assert record.rebind is not None, "update_record only applies to the record carrying this rebind"
    return record.with_converging(change(record.rebind))


@pure
def _with_active_entry_rebound(record: ChatRecord, agent_id: str, account: Account) -> ChatRecord:
    """The record with its active entry naming the account and lane the agent now runs on."""
    last = record.agents[-1]
    assert last.agent_id == agent_id, "a rebind names the record's active agent"
    rebound = last.model_copy_update(
        to_update(last.field_ref().account_id, account.id), to_update(last.field_ref().lane, account.lane)
    )
    return record.model_copy_update(to_update(record.field_ref().agents, (*record.agents[:-1], rebound)))
