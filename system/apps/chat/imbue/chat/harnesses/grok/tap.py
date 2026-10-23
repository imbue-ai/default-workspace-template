"""grok's stop and shoulder tap: one Ctrl+C through mngr, then the held queue, which the chat owns.

Neither types: the flush worker is the only typist. mngr presses Ctrl+C only into a cancellable top-level turn
whose footer shows the cancel hint and whose composer is empty, and refuses otherwise.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.turn_state import get_cancellable_turns
from imbue.chat.harnesses.grok.turn_state import get_turn_state
from imbue.chat.harnesses.interrupt import InterruptToComposer
from imbue.chat.harnesses.interrupt import IsCompactionInFlight
from imbue.chat.harnesses.interrupt import PressChord
from imbue.chat.harnesses.interrupt import RestartProcess
from imbue.chat.harnesses.interrupt import SettleActivity
from imbue.chat.harnesses.session import AtomicShoulderTap
from imbue.chat.harnesses.session import ShoulderTapOutcome
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_grok.api import GrokRootTurn

# How long a cancelled turn may take to leave mngr's root-turn files before the restart hammer is used.
TURN_END_DEADLINE_SECONDS: Final[float] = 8.0
# How long a tap whose press mngr refused waits for the turn to leave mngr's root-turn files: mngr refuses a press
# into a turn grok has already ended, whose file goes 0.5 to 1.2 s after grok logs the end.
REFUSED_PRESS_TURN_END_SECONDS: Final[float] = 2.0
_TURN_END_POLL_SECONDS: Final[float] = 0.2

_NOTHING_QUEUED: Final[str] = "nothing_queued"
_NO_OPEN_TURN: Final[str] = "no_open_turn"
_SEND_IN_FLIGHT: Final[str] = "send_in_flight"
_FLUSHED: Final[str] = "flushed"
_PRESS_REFUSED: Final[str] = "press_refused"
_NOT_FLUSHED: Final[str] = "not_flushed"


def _press_once(agent_info: AgentInfo, press_chord: PressChord) -> bool:
    """One Ctrl+C through mngr, at most once per the agent's press interval; False when refused.

    A press mngr refused gives its interval back.
    """
    turn_state = get_turn_state(agent_info.agent_state_dir.name)
    claimed_at = turn_state.try_claim_press()
    if claimed_at is None:
        logger.info("grok: not pressing Ctrl+C into {} again so soon after the last press", agent_info.name)
        return False
    is_pressed = press_chord()
    if not is_pressed:
        turn_state.release_press(claimed_at)
    return is_pressed


def _wait_for_turns_to_end(agent_state_dir: Path, turns: frozenset[GrokRootTurn], deadline_seconds: float) -> bool:
    """Whether every one of ``turns`` ended within the deadline; a turn grok starts after them is not one of them."""
    return poll_until(
        lambda: turns.isdisjoint(get_cancellable_turns(agent_state_dir)),
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
        """End the turn and return every held message that was never delivered.

        The flush worker claims nothing until the stop returns, so the idle grok the cancel leaves is not sent what
        the stop hands back. A refused press waits for the turn to end on its own; only a turn that outlives the
        deadline gets the restart, which never loses the messages it took. A turn grok starts once the pressed one
        ends, such as a finished background shell's follow-up, is left running.
        """
        agent_state_dir = self._agent_info.agent_state_dir
        queue = get_tracker(agent_state_dir)
        queue.pause_flushes()
        try:
            turns = get_cancellable_turns(agent_state_dir)
            if turns:
                is_pressed = _press_once(self._agent_info, press_chord)
                if not is_pressed:
                    logger.info("grok: Ctrl+C refused for {}; waiting for its turn to end", self._agent_info.name)
                if not _wait_for_turns_to_end(agent_state_dir, turns, self.turn_end_deadline_seconds):
                    return self._restart(watcher, restart_process, settle_activity, get_in_flight_block)
                if is_pressed:
                    settle_activity()
            block, _taken = watcher.take_unclaimed_queue()
            return _combine(block, get_in_flight_block())
        finally:
            queue.resume_flushes()

    def _restart(
        self,
        watcher: AgentSessionWatcher,
        restart_process: RestartProcess,
        settle_activity: SettleActivity,
        get_in_flight_block: Callable[[], str],
    ) -> str:
        """Restart grok and return every held message, the ones sent while it restarted included."""
        logger.warning("grok: the turn of {} did not end; restarting it", self._agent_info.name)
        in_flight = get_in_flight_block()
        taken_block, _taken = watcher.take_whole_queue()
        is_restarted, output = restart_process()
        if is_restarted:
            settle_activity()
        else:
            logger.warning("grok: could not restart {}: {}", self._agent_info.name, output)
        late_block, _late_taken = watcher.take_whole_queue()
        return _combine(taken_block, late_block, in_flight)


class GrokAtomicShoulderTap(AtomicShoulderTap):
    """Shoulder tap: cancel the turn with one Ctrl+C, so the flush worker delivers the held queue at once."""

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
        if not watcher.get_queued_block():
            return ShoulderTapOutcome(status=_NOTHING_QUEUED)
        agent_state_dir = self._agent_info.agent_state_dir
        turns = get_cancellable_turns(agent_state_dir)
        if not turns:
            watcher.notify_idle()
            return ShoulderTapOutcome(status=_NO_OPEN_TURN)
        _block, claimed, generation = watcher.claim_queue_for_tap()
        if not claimed:
            return ShoulderTapOutcome(status=_SEND_IN_FLIGHT)
        try:
            is_pressed = _press_once(self._agent_info, press_chord)
            deadline_seconds = (
                self.turn_end_deadline_seconds
                if is_pressed
                else min(REFUSED_PRESS_TURN_END_SECONDS, self.turn_end_deadline_seconds)
            )
            is_turn_ended = _wait_for_turns_to_end(agent_state_dir, turns, deadline_seconds)
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
