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
watcher, a process restart, an activity-settle, and the native cancel keypress -- exactly as
the switch endpoint binds its ``send`` callback. A native override that needs only some of them
(pi, codex) simply ignores the rest.
"""

import fcntl
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Protocol

from loguru import logger

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.models import AgentRestartError

# mngr serializes every text send / key chord to an agent under an exclusive flock on this
# file in the agent's state dir (see ``BaseAgent._message_lock``). A stop that wants to read a
# durable mirror -- or write a native retract that must be ordered AFTER any in-flight send --
# takes the SAME lock, so a mid-flight ``mngr message`` send has finished its append/paste-and-
# confirm cycle (and durably parked) before the stop acts. Kept in sync with mngr's filename.
MESSAGE_LOCK_FILENAME: str = "message.lock"

# How long a native stop waits for an in-flight send to release ``message.lock`` before giving
# up and falling back to the restart-drain hammer. A mid-turn steer holds the lock only for its
# durable append/paste (sub-second on a local host -- pi/codex confirm a mid-turn steer without
# the idle-start turn-confirm poll), so this window is almost never spent; an idle-start send
# that holds the lock through its full turn-confirm is exactly the case the hammer should own.
STOP_LOCK_WAIT_SECONDS: float = 2.0


class DrainWatcher(Protocol):
    """The slice of the session watcher the restart-drain needs. A Protocol (mirroring the tap's
    ``TapWatcher``) so unit tests inject a scripted fake; the real :class:`AgentSessionWatcher`
    satisfies it structurally."""

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Read session files and return parsed events; the single point that refreshes the mirror."""
        ...

    def get_queued_block(self) -> str:
        """The queued messages as one concatenated turn (empty == nothing queued)."""
        ...

    def clear_queue(self) -> None:
        """Drop the tracked queued set (a restart invalidated it)."""
        ...


# Restart the agent process; returns ``(is_restarted, output)`` (stdout on success, stderr on
# failure). Bound by the endpoint to the specific agent.
RestartProcess = Callable[[], tuple[bool, str]]
# Settle the derived activity state after a mid-turn restart abandons the transcript. Bound by
# the endpoint to the specific agent.
SettleActivity = Callable[[], None]
# Press the harness's native cancel key chord into the agent's pane (under mngr's per-agent
# ``message.lock``); returns success. Bound by the endpoint to the specific agent -- claude's
# empty-queue chord path uses it; the base restart-drain and the other overrides ignore it.
PressChord = Callable[[], bool]


@contextmanager
def try_hold_message_lock(
    agent_state_dir: Path,
    *,
    wait_seconds: float | None = None,
    poll_interval_seconds: float = 0.05,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Generator[bool, None, None]:
    """Bounded, non-blocking acquire of the per-agent ``message.lock``.

    Yields ``True`` and HOLDS the lock for the block if it could be taken within ``wait_seconds``
    (no send was in flight, or one finished and released in time); yields ``False`` holding
    NOTHING if the deadline passed with a send still holding it. The caller runs its native
    stop under a ``True`` and falls back to the restart-drain hammer under a ``False`` -- the
    bounded wait is what turns "a send is in flight" into a fast, deterministic hammer instead
    of an unbounded stall on the send's turn-confirm. ``wait_seconds`` defaults (read at call
    time so a test can patch the module constant) to :data:`STOP_LOCK_WAIT_SECONDS`;
    ``now``/``sleep`` are injected for tests.
    """
    if wait_seconds is None:
        wait_seconds = STOP_LOCK_WAIT_SECONDS
    lock_path = agent_state_dir / MESSAGE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = now() + wait_seconds
    with open(lock_path, "w") as lock_file:
        acquired = False
        timed_out = False
        while not acquired and not timed_out:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if now() >= deadline:
                    timed_out = True
                else:
                    sleep(poll_interval_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
    ) -> str:
        """Interrupt the turn and return the queued messages as one block (``""`` = nothing queued).

        ``watcher`` is the agent's live queue mirror; ``restart_process`` / ``settle_activity``
        are the base restart-drain's capabilities; ``press_chord`` delivers the harness's native
        cancel chord under mngr's lock (claude's empty-queue path uses it, the others ignore it).
        ``get_in_flight_block`` reads the session's *Sending* records, so a send aborted
        mid-flight is folded into the returned block (contract A4/B) by the harnesses that
        guarantee ordering (claude, pi); the base restart-drain ignores it.
        Raises :class:`AgentRestartError` if a restart-based implementation cannot restart.
        """


def restart_drain(
    agent_info: AgentInfo,
    watcher: DrainWatcher,
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


def restart_drain_under_message_lock(
    agent_info: AgentInfo,
    watcher: DrainWatcher,
    restart_process: RestartProcess,
    settle_activity: SettleActivity,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """:func:`restart_drain` with its capture made durable against a concurrent send (U1).

    Takes mngr's per-agent ``message.lock`` with the shared bounded wait, then refreshes the
    mirror (``get_all_events``) and captures/restarts under it: acquiring the lock means any
    in-flight send has durably parked, so a message that parked between the caller's last mirror
    read and the SIGKILL rides the returned block instead of dying silently with the process.
    When the wait expires (an idle-start send holding the lock through its turn-confirm), stop
    must still win: refresh and hammer anyway -- that message is stopped, not recovered to the
    composer, and never runs (the pi/codex not-held posture). ``now``/``sleep`` drive the
    bounded acquire and are injected for tests.
    """
    with try_hold_message_lock(agent_info.agent_state_dir, now=now, sleep=sleep) as is_lock_held:
        if not is_lock_held:
            logger.info(
                "Stop for agent '{}': message.lock still held past the bounded wait; "
                "restarting on a best-effort re-capture",
                agent_info.name,
            )
        watcher.get_all_events()
        return restart_drain(agent_info, watcher, restart_process, settle_activity)


class RestartDrainInterruptToComposer(InterruptToComposer):
    """The default stop mechanism (any harness without a native override): the shared
    :func:`restart_drain`, refreshed and captured under the bounded message lock."""

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "RestartDrainInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
    ) -> str:
        # The base restart-drain interrupts via SIGKILL-relaunch; it has no use for the chord
        # or the in-flight fold (its bounded lock acquire is best-effort, not ordered).
        return restart_drain_under_message_lock(self._agent_info, watcher, restart_process, settle_activity)
