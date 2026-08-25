"""agy's stop and shoulder tap.

Both are claude's shapes with agy's one structural advantage: **we hold the queue**, so neither
action has to retrieve anything from inside agy. See
``docs/design/antigravity-swallow-fix-plan.md``.

**Neither of these typists types.** The tap does not send the block; it cancels the turn and
wakes the flush worker, which is the only code in the system that delivers. Two typists meant
the tap could read a block, release the lock, press, wait out the settle -- which is exactly
the idle edge the worker is waiting for -- and then send a block the worker had already sent.

**Neither of them clears the queue.** Both take only what they accounted for. ``clear_queue``
cannot distinguish the entries captured before the chord from ones the user sent while the
chord was settling, and wiping the latter leaves them in no state at all.

The cancel key is a SINGLE ctrl+c, agy's native ``cli.escape`` action. agy reads the first as
"interrupt the active operation" and a DOUBLE press as "exit", and its documentation says that
exit valve fires regardless of how the key is remapped. A greyed button is not enough
protection for a failure that destroys the agent process, so the press goes through a shared
per-agent interlock (:meth:`TurnState.try_claim_press`) that refuses a second press inside
``MIN_PRESS_INTERVAL_SECONDS`` no matter which caller asks.
"""

import time
from collections.abc import Callable
from typing import Final

from loguru import logger

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.turn_state import TurnState
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.interrupt import InterruptToComposer
from imbue.system_interface.harnesses.interrupt import PressChord
from imbue.system_interface.harnesses.interrupt import RestartProcess
from imbue.system_interface.harnesses.interrupt import SettleActivity
from imbue.system_interface.harnesses.interrupt import restart_drain
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
_PRESS_REFUSED: Final[str] = "press_refused"


def _turn_state(agent_info: AgentInfo) -> TurnState:
    return get_turn_state(agent_info.agent_state_dir.name)


def _is_marker_present(agent_info: AgentInfo) -> bool:
    """agy's raw busy marker: present while its statusline last reported busy."""
    return (agent_info.agent_state_dir / ACTIVE_MARKER_FILENAME).exists()


def _is_turn_open(agent_info: AgentInfo) -> bool:
    """Whether a turn is open, per the bounded predicate (see turn_state).

    Every rung is freshness-bounded on purpose. Measured on agy 1.1.20: a cancelled tool call
    settles as ``status=CANCELED`` and the parser emits a ``tool_result`` for it, so the tail
    reads "open" forever afterwards. An unbounded reading of the tail would make the first stop
    an agent receives wedge its queue permanently.
    """
    return _turn_state(agent_info).is_hold_required(agent_info.agent_state_dir)


def _press_once(agent_info: AgentInfo, press_chord: PressChord) -> bool:
    """Press the cancel key, at most once per ``MIN_PRESS_INTERVAL_SECONDS`` per agent.

    The interlock is shared between stop and the tap, so two different callers racing cannot
    between them deliver the double press that exits agy.
    """
    state = _turn_state(agent_info)
    if not state.try_claim_press():
        logger.warning("antigravity: refusing a second cancel key for {} -- a double press exits agy", agent_info.name)
        return False
    state.note_cancelled()
    return press_chord()


def _wait_for_turn_to_end(agent_info: AgentInfo, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Poll until agy's busy marker clears, or the deadline passes.

    One signal, where claude needs a two-arm verdict lattice: claude strands its marker on
    interrupt and has to distinguish "aborted" from "turn ended" via transcript sentinels.
    agy's statusline clears the marker itself on the idle edge, so the marker going away IS
    the confirmation.

    Deliberately the MARKER, not :func:`_is_turn_open`. The two ask different questions and
    only look alike. "Is a turn open?" must survive the transcript looking busy after a
    cancel, so it is bounded and consults our own cancel stamp. "Did the cancel I just sent
    land?" wants exactly the marker's idle edge -- and since a cancelled chain's tail keeps
    reading open, a transcript-based wait here would never be satisfied and every stop would
    time out into the restart hammer.
    """
    deadline = time.monotonic() + _ABORT_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not _is_marker_present(agent_info):
            return True
        sleep(_ABORT_POLL_SECONDS)
    return not _is_marker_present(agent_info)


class AntigravityInterruptToComposer(InterruptToComposer):
    """Stop: end the turn, and hand back everything we were still holding."""

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
        """End the live turn and return every message that was never delivered.

        agy needs no restart to stop: nothing is parked inside it, because the queue is ours.
        The restart survives only as the bounded hammer for the cases where the cancel key
        cannot be trusted to have landed -- a refused or failed press, or a turn that will not
        settle.

        The queue is taken with ``take_unclaimed``, which removes exactly the entries no flush
        has claimed and bumps the generation so an in-flight flush's settle is discarded.
        Entries a flush HAS claimed are deliberately left alone: that send may still land, and
        handing them to the composer as well is how one message becomes both Delivered and
        Returned.
        """
        if not _is_turn_open(self._agent_info):
            # Nothing running. Still return the queue: those messages were never sent.
            block, _taken = watcher.take_unclaimed_queue()
            return _combine(block, get_in_flight_block())
        is_pressed = _press_once(self._agent_info, press_chord)
        if not is_pressed or not _wait_for_turn_to_end(self._agent_info):
            # Deliberately NOT a second press -- see the module docstring.
            logger.warning("antigravity: cancel did not settle for {}; restarting", self._agent_info.name)
            in_flight = get_in_flight_block()
            # EVERYTHING, claimed included: the restart kills the send that owned the claimed
            # entries, and the shared drain clears the queue on its way through, so anything
            # not taken here is destroyed with no accounting.
            block, _taken = watcher.take_whole_queue()
            restart_drain(self._agent_info, watcher, restart_process, settle_activity)
            return _combine(block, in_flight)
        settle_activity()
        block, _taken = watcher.take_unclaimed_queue()
        return _combine(block, get_in_flight_block())


class AntigravityAtomicShoulderTap(AtomicShoulderTap):
    """Shoulder tap: cancel the turn so the flush worker can deliver immediately."""

    _agent_info: AgentInfo

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityAtomicShoulderTap":
        self = cls.__new__(cls)
        self._agent_info = agent_info
        return self

    def tap(
        self,
        watcher: AgentSessionWatcher,
        press_chord: PressChord,
        send_recovery: Callable[[str], bool],
    ) -> ShoulderTapOutcome:
        """Cancel the turn so agy is free, then let the ONE typist deliver.

        This is codex's shape rather than claude's: claude taps by cancelling and letting the
        harness flush its own parked queue; agy has no parked queue, so ours is delivered
        instead. What it deliberately does NOT do is deliver it here. ``send_recovery`` is
        unused, and that is the fix -- a tap that sent could race the flush worker for the same
        block and deliver it twice.

        Claiming BEFORE the press is what greys the button for the whole run: the entries read
        "Sending..." from this moment, so a second tap cannot arrive and press ctrl+c again.
        """
        if not watcher.get_queued_block():
            return ShoulderTapOutcome(status=_NOTHING_QUEUED)
        if not _is_turn_open(self._agent_info):
            # No turn to interrupt; the worker will deliver on its own, imminently.
            watcher.notify_idle()
            return ShoulderTapOutcome(status=_NO_OPEN_TURN)
        block, claimed, generation = watcher.claim_queue_for_tap()
        if not claimed:
            # A flush already owns the queue. Benign no-op, as claude's.
            return ShoulderTapOutcome(status=_SEND_IN_FLIGHT)
        # From here every exit must un-claim, or the entries stay "Sending..." forever with no
        # send behind them.
        is_pressed = _press_once(self._agent_info, press_chord)
        if not is_pressed:
            watcher.release_tap_claim(claimed, generation)
            return ShoulderTapOutcome(
                status=_PRESS_REFUSED, error_detail="A cancel key was already sent to antigravity moments ago."
            )
        if not _wait_for_turn_to_end(self._agent_info):
            watcher.release_tap_claim(claimed, generation)
            return ShoulderTapOutcome(status="not_flushed", error_detail="Antigravity did not stop its turn in time.")
        watcher.release_tap_claim(claimed, generation)
        watcher.notify_idle()
        # block="" DELIBERATELY. ``ShoulderTapOutcome.block`` is a returned-to-composer
        # handback, used only when a tap FAILED to hand its text back. Returning the queue
        # here made the frontend prepend it to the composer while the worker was also
        # delivering it -- the user saw their messages both sent and drained back.
        return ShoulderTapOutcome(status=_FLUSHED)


def _combine(queued_block: str, in_flight_block: str) -> str:
    """Queued first, then in-flight: send order, which is what the composer must show."""
    return "\n".join(part for part in (queued_block, in_flight_block) if part)
