"""grok's stop and shoulder tap: one Ctrl+C through mngr, then the messages the chat holds.

Neither types: the flush worker is the only typist. mngr presses Ctrl+C only into a cancellable top-level turn and
spaces two presses itself, refusing otherwise.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.turn_gate import is_active_marker_present
from imbue.chat.harnesses.grok.turn_gate import is_turn_in_flight
from imbue.chat.harnesses.interrupt import InterruptToComposer
from imbue.chat.harnesses.interrupt import IsCompactionInFlight
from imbue.chat.harnesses.interrupt import PressChord
from imbue.chat.harnesses.interrupt import RestartProcess
from imbue.chat.harnesses.interrupt import SettleActivity
from imbue.chat.harnesses.session import AtomicShoulderTap
from imbue.chat.harnesses.session import ShoulderTapOutcome
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.mngr.utils.polling import poll_until

# How long a pressed turn may take to clear mngr's ``active`` marker.
TURN_END_DEADLINE_SECONDS: Final[float] = 8.0
# How long a tap whose press mngr refused waits for the marker: mngr refuses a press into a turn grok has just ended,
# whose marker is removed up to about a second later.
REFUSED_PRESS_TURN_END_SECONDS: Final[float] = 2.0
_TURN_END_POLL_SECONDS: Final[float] = 0.2

_NOTHING_QUEUED: Final[str] = "nothing_queued"
_NO_OPEN_TURN: Final[str] = "no_open_turn"
_SEND_IN_FLIGHT: Final[str] = "send_in_flight"
_FLUSHED: Final[str] = "flushed"
_PRESS_REFUSED: Final[str] = "press_refused"
_NOT_FLUSHED: Final[str] = "not_flushed"


def _wait_for_turn_to_end(agent_state_dir: Path, deadline_seconds: float) -> bool:
    """Whether mngr's ``active`` marker cleared within the deadline."""
    return poll_until(
        lambda: not is_active_marker_present(agent_state_dir),
        timeout=deadline_seconds,
        poll_interval=_TURN_END_POLL_SECONDS,
    )


def _combine(*blocks: str) -> str:
    """The blocks in send order, as the composer shows them."""
    return "\n".join(block for block in blocks if block)


class GrokInterruptToComposer(InterruptToComposer):
    """Stop: cancel the turn with one Ctrl+C, and hand back everything still held."""

    _agent_info: AgentInfo
    turn_end_deadline_seconds: float

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "GrokInterruptToComposer":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        self.turn_end_deadline_seconds = TURN_END_DEADLINE_SECONDS
        return self

    def drain_to_composer(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        press_chord: PressChord,
        get_in_flight_block: Callable[[], str],
        is_compaction_in_flight: IsCompactionInFlight,
    ) -> str:
        """Cancel the turn mngr reports in flight, then return every held message the flush worker has not claimed.

        The worker claims nothing until the stop returns, so the idle grok the cancel leaves is not typed what the
        stop hands back. A turn mngr does not press into, or that outlives the deadline, is left running.
        """
        agent_state_dir = self._agent_info.agent_state_dir
        queue = get_tracker(agent_state_dir)
        queue.pause_flushes()
        try:
            if is_turn_in_flight(self._agent_info.state, agent_state_dir):
                if not press_chord():
                    logger.info("grok: mngr refused the Ctrl+C for {}; its turn runs on", self._agent_info.name)
                elif _wait_for_turn_to_end(agent_state_dir, self.turn_end_deadline_seconds):
                    settle_activity()
                else:
                    logger.warning("grok: the turn of {} outlived its Ctrl+C", self._agent_info.name)
            block, _taken = watcher.take_unclaimed_queue()
            return _combine(block, get_in_flight_block())
        finally:
            queue.resume_flushes()


class GrokAtomicShoulderTap(AtomicShoulderTap):
    """Shoulder tap: cancel the turn with one Ctrl+C, so the flush worker types the held messages at once."""

    _agent_info: AgentInfo
    turn_end_deadline_seconds: float

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "GrokAtomicShoulderTap":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        self.turn_end_deadline_seconds = TURN_END_DEADLINE_SECONDS
        return self

    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: PressChord,
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Claim the queue (greying the button), press, wait for the turn to end, release and wake the worker.

        A refused press still waits a little for the turn to end, since mngr refuses one into a turn grok has just
        ended; that turn ending is no error.
        """
        agent_state_dir = self._agent_info.agent_state_dir
        if not watcher.get_queued_block() or not get_tracker(agent_state_dir).has_deliverable_entries():
            return ShoulderTapOutcome(status=_NOTHING_QUEUED)
        if not is_turn_in_flight(self._agent_info.state, agent_state_dir):
            watcher.notify_idle()
            return ShoulderTapOutcome(status=_NO_OPEN_TURN)
        _block, claimed, generation = watcher.claim_queue_for_tap()
        if not claimed:
            return ShoulderTapOutcome(status=_SEND_IN_FLIGHT)
        try:
            is_pressed = press_chord()
            deadline_seconds = (
                self.turn_end_deadline_seconds
                if is_pressed
                else min(REFUSED_PRESS_TURN_END_SECONDS, self.turn_end_deadline_seconds)
            )
            is_turn_ended = _wait_for_turn_to_end(agent_state_dir, deadline_seconds)
        finally:
            watcher.release_tap_claim(claimed, generation)
            watcher.notify_idle()
        if is_turn_ended:
            return ShoulderTapOutcome(status=_FLUSHED if is_pressed else _NO_OPEN_TURN)
        if not is_pressed:
            return ShoulderTapOutcome(
                status=_PRESS_REFUSED, error_detail="grok's turn could not be interrupted right now."
            )
        return ShoulderTapOutcome(status=_NOT_FLUSHED, error_detail="grok did not stop its turn in time.")
