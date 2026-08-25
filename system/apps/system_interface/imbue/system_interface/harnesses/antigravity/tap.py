"""agy's stop and shoulder tap.

Both are claude's shapes with agy's one structural advantage: **we hold the queue**, so
neither action has to retrieve anything from inside agy. See
docs/design/antigravity-message-lifecycle-plan.md.

The cancel key is a SINGLE ctrl+c, agy's native `cli.escape` action, which ends the live turn.
Unlike claude there is no binding to provision -- but also no scoping, so it is only ever
pressed when a turn is known to be open.

**One press, never two.** agy reads the first ctrl+c as "interrupt the active operation" and a
double press as "exit", and its documentation says that exit valve fires regardless of how the
key is remapped. Both actions below press once and fall back to the restart rather than
pressing again; a retry here would kill the agent instead of interrupting it.
"""

import time
from collections.abc import Callable
from typing import Final

from loguru import logger

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.session import AtomicShoulderTap
from imbue.system_interface.harnesses.session import ShoulderTapOutcome
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher

# How long to wait for agy's busy marker to clear after the cancel key. agy's statusline
# removes it on the idle edge, so this is a settle window, not a poll for something slow.
_ABORT_DEADLINE_SECONDS: Final[float] = 8.0
_ABORT_POLL_SECONDS: Final[float] = 0.2

# The 200 no-ops, mirroring claude's: an idempotent tap must not read as an error.
_NOTHING_QUEUED: Final[str] = "nothing_queued"
_NO_OPEN_TURN: Final[str] = "no_open_turn"
_SEND_IN_FLIGHT: Final[str] = "send_in_flight"
_FLUSHED: Final[str] = "flushed"


def _is_turn_open(agent_info: AgentInfo) -> bool:
    """Whether agy is mid-turn, per the marker its own statusline maintains."""
    return (agent_info.agent_state_dir / ACTIVE_MARKER_FILENAME).exists()


def _wait_for_turn_to_end(agent_info: AgentInfo, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll until agy's busy marker clears, or the deadline passes.

    One signal, where claude needs a two-arm verdict lattice: claude strands its marker on
    interrupt and has to distinguish "aborted" from "turn ended" via transcript sentinels.
    agy's statusline clears the marker itself on the idle edge, so the marker going away IS
    the confirmation.
    """
    deadline = time.monotonic() + _ABORT_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not _is_turn_open(agent_info):
            return True
        sleep(_ABORT_POLL_SECONDS)
    return not _is_turn_open(agent_info)


class AntigravityInterruptToComposer(InterruptToComposer):
    """Stop: end the turn, and hand back everything that was not delivered."""

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityInterruptToComposer":
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
        """Cancel the live turn and return the unsent messages, in send order.

        agy does not need claude's empty-vs-nonempty branch. claude MUST restart when its
        queue is non-empty, because those messages are already parked inside claude and a
        cancel would make it flush and COMMIT the very messages stop promised to retract.
        Nothing is ever parked inside agy -- the queue is ours -- so cancelling can never
        commit anything, and the queue is returned by simply reading it.

        The restart survives as the bounded hammer, for the one case that needs it: the
        cancel key travels through mngr, which takes the agent's message lock, so a stop
        landing during an in-flight send would otherwise block behind it. Failing to take
        the lock means exactly that, and stop must still win.
        """
        queued_block = watcher.get_queued_block()
        with try_hold_message_lock(self._agent_info.agent_state_dir) as is_lock_held:
            if not is_lock_held:
                # A send is in flight. Its text is not committed, so it must come back too --
                # the base restart-drain discards the in-flight block, which is why this does
                # not delegate to it.
                in_flight = get_in_flight_block()
                restart_drain(self._agent_info, watcher, restart_process, settle_activity)
                return _combine(queued_block, in_flight)
            if not _is_turn_open(self._agent_info):
                # Nothing running. Still return the queue: those messages were never sent.
                watcher.clear_queue()
                return queued_block
        # The chord is pressed with the lock RELEASED. ``press_chord`` goes through mngr, which
        # takes this same message.lock, and flock is per open-file-description -- so pressing
        # while still holding it would block this process against itself, forever. claude
        # sequences it the same way, and calls the gap between release and press its accepted
        # capture-window residual.
        is_pressed = press_chord()
        if not is_pressed or not _wait_for_turn_to_end(self._agent_info):
            # Deliberately NOT a second press -- see the module docstring.
            logger.warning("antigravity: cancel did not settle for {}; restarting", self._agent_info.name)
            in_flight = get_in_flight_block()
            restart_drain(self._agent_info, watcher, restart_process, settle_activity)
            return _combine(queued_block, in_flight)
        settle_activity()
        watcher.clear_queue()
        return queued_block


class AntigravityAtomicShoulderTap(AtomicShoulderTap):
    """Shoulder tap: end the turn, then deliver the held block immediately."""

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityAtomicShoulderTap":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        return self

    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: Callable[[], bool],
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Cancel the turn so agy is free, then send the whole held block as one turn.

        This is codex's shape rather than claude's. claude taps by cancelling and letting the
        harness flush its OWN parked queue; agy has no parked queue to flush, so we cancel and
        then deliver ours. The block is sent verbatim, which is the same text a natural flush
        would have sent -- one turn either way.
        """
        block = watcher.get_queued_block()
        if not block:
            return ShoulderTapOutcome(status=_NOTHING_QUEUED)
        if not _is_turn_open(self._agent_info):
            # No turn to interrupt; the ordinary idle flush will deliver it imminently.
            return ShoulderTapOutcome(status=_NO_OPEN_TURN)
        with try_hold_message_lock(self._agent_info.agent_state_dir) as is_lock_held:
            if not is_lock_held:
                # A send is in flight, so the queue is not settled. Benign no-op, as claude's.
                return ShoulderTapOutcome(status=_SEND_IN_FLIGHT)
        # Released before pressing -- see the note in the stop path above.
        is_pressed = press_chord()
        if not is_pressed:
            return ShoulderTapOutcome(
                status="chord_failed", error_detail="Could not send the cancel key to antigravity."
            )
        if not _wait_for_turn_to_end(self._agent_info):
            return ShoulderTapOutcome(status="not_flushed", error_detail="Antigravity did not stop its turn in time.")
        if not send_recovery(block):
            # The queue is untouched, so the idle flush will retry it. Never dropped.
            return ShoulderTapOutcome(
                status="not_flushed", error_detail="Antigravity stopped, but the queued messages could not be sent."
            )
        watcher.clear_queue()
        return ShoulderTapOutcome(status=_FLUSHED, block=block)


def _combine(queued_block: str, in_flight_block: str) -> str:
    """Queued first, then in-flight: send order, which is what the composer must show."""
    return "\n".join(part for part in (queued_block, in_flight_block) if part)
