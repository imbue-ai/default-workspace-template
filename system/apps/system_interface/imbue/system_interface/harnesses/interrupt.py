"""Per-harness stop-button behavior: interrupt the running turn and hand the queued
messages back to the user's composer (Contract B).

HOW a turn is interrupted is per-harness. The default -- for claude and any future harness --
is the BASE restart-drain: capture the block, SIGKILL-relaunch the process, settle activity,
clear the mirror. A harness that can interrupt its live turn natively (pi here; codex and
claude in sibling plans) registers an override instead.

This mirrors the model-resolver precedent (:mod:`harnesses.model`): one abstract class, one
subclass per harness that needs it, registered on the :class:`~harnesses.registry.HarnessSpec`
and dispatched by ``AgentInfo.harness`` -- the endpoint never branches on the harness name. It
is backend-only: there is no wire-visible catalog flag, so the frontend keeps one button and
one endpoint, and an unregistered harness legitimately falls through to the base.

The endpoint binds the harness-neutral capabilities the implementation may need -- the queue
watcher, a process restart, and an activity-settle -- exactly as the switch endpoint binds its
``send`` callback. A native override that needs none of them (pi) simply ignores them.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.models import AgentRestartError

# Restart the agent process; returns ``(is_restarted, output)`` (stdout on success, stderr on
# failure). Bound by the endpoint to the specific agent.
RestartProcess = Callable[[], tuple[bool, str]]
# Settle the derived activity state after a mid-turn restart abandons the transcript. Bound by
# the endpoint to the specific agent.
SettleActivity = Callable[[], None]


class InterruptToComposer(ABC):
    """Interrupts one agent's running turn and returns its queued block to the composer.

    ``build`` takes the whole :class:`AgentInfo` (like the watcher/resolver) so each harness
    reads only the paths it needs.
    """

    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo) -> "InterruptToComposer":
        """Construct for one agent, not yet touching anything."""

    @abstractmethod
    def drain_to_composer(
        self, watcher: AgentSessionWatcher, restart_process: RestartProcess, settle_activity: SettleActivity
    ) -> str:
        """Interrupt the turn and return the queued messages as one block (``""`` = nothing queued).

        ``watcher`` is the agent's live queue mirror; ``restart_process`` / ``settle_activity``
        are the base restart-drain's capabilities (a native override may ignore them). Raises
        :class:`AgentRestartError` if a restart-based implementation cannot restart.
        """


def restart_drain(
    agent_info: AgentInfo,
    watcher: AgentSessionWatcher,
    restart_process: RestartProcess,
    settle_activity: SettleActivity,
) -> str:
    """The shared restart-drain: capture the block, restart, settle activity, clear the mirror.

    The block is captured BEFORE the restart (which drops the harness queue); the restart is then
    run, the transcript-derived activity settled (the caller's own next send re-drives it), and
    the tracked queued set the SIGKILL invalidated cleared -- which also pushes the now-empty
    group. No empty-queue short-circuit: a stop with nothing queued still interrupts the turn
    (callers wanting a no-op on an empty queue, e.g. the flush, check first). Raises
    :class:`AgentRestartError` if the restart fails.
    """
    block = watcher.get_queued_block()
    is_restarted, output = restart_process()
    if not is_restarted:
        raise AgentRestartError(f"Failed to restart agent '{agent_info.name}': {output}")
    settle_activity()
    watcher.clear_queue()
    return block


class RestartDrainInterruptToComposer(InterruptToComposer):
    """The default stop mechanism (claude, and any harness without a native override): the
    shared :func:`restart_drain`."""

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "RestartDrainInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        return self

    def drain_to_composer(
        self, watcher: AgentSessionWatcher, restart_process: RestartProcess, settle_activity: SettleActivity
    ) -> str:
        return restart_drain(self._agent_info, watcher, restart_process, settle_activity)
