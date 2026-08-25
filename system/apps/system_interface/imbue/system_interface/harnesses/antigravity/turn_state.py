"""Is one of agy's turns open right now? -- and may we press its cancel key?

Shared by the watcher (which publishes the transcript's answer), the flush worker (the only
typist) and the tap/stop executors.

WHY THE MARKER IS ONLY EVER EVIDENCE OF BUSY. agy's statusLine writes an ``active`` marker on
every busy sample and removes it on the idle edge. A FRESH marker is therefore positive
evidence of a live turn -- but its ABSENCE proves nothing, and that asymmetry is the whole
design.

Measured on agy 1.1.20, sampling statusLine directly: it fires about every 300ms, its
vocabulary includes ``tool_use`` as well as ``idle`` and ``working``, and during a BACKGROUNDED
tool call agy reports ``idle`` and stops sampling altogether -- 33.5 seconds of silence in the
middle of a turn whose answer had not arrived. It is not lying; it genuinely has nothing to do
while the command runs. But the marker is gone for that whole window, so anything treating its
absence as "no turn is open" would type straight into that turn. (``statusline.sh``'s header
records the opposite -- 75 consecutive busy samples, zero mid-turn idle blips -- measured
against agy 1.0.6/1.0.7 across a SUBAGENT run. Both can be true: the two cases differ.)

The transcript is what carries the turn through those windows, which is why the marker is a
corroborator here and never the sole authority.

WHY EVERY RUNG IS BOUNDED. The transcript corroborates during the marker's lag windows, but a
transcript rung alone cannot terminate. Measured on agy 1.1.20: a single ctrl+c during a tool
call leaves the cancelled step ``status=CANCELED, is_terminal=True`` and the parser emits a
``tool_result`` for it -- so the tail is a ``tool_result`` forever, and a naive "tool_result
tail means the turn is open" holds every later message for the life of the agent. The first
stop would permanently wedge the agent. Every rung here therefore carries a freshness bound,
which is what guarantees the queue makes progress rather than merely conserving.
"""

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch

_OPEN_TAIL_TYPES: Final[frozenset[str]] = frozenset({"user_message", "tool_result"})

# How recently the statusline must have asserted busy for the marker to count as live.
# Calibrated against a statusLine that samples continuously through a turn; the ceiling only
# has to outlast the gap between samples, not the turn.
# ponytail: one fixed window; per-agent calibration if agy's sampling turns out to be sparse.
BUSY_ASSERT_SECONDS: Final[float] = 60.0

# Backstop for an open-looking tail with no marker, no cancel and no restart to explain it.
# Long enough not to cut a real tool chain short, short enough that a wedged agent recovers
# without operator action.
TAIL_OPEN_SECONDS: Final[float] = 1800.0

# agy treats a DOUBLE ctrl+c as EXIT, and its docs say that valve fires regardless of
# remapping. This is the hard interlock behind the greyed button: two presses inside this
# window are refused outright, because the failure destroys the agent process.
MIN_PRESS_INTERVAL_SECONDS: Final[float] = 5.0

# agy's transcript timestamps have 1-second resolution while the markers are nanosecond
# mtimes, so a row written in the same second as a restart can read as older than it. Widen
# every comparison by one second -- in the direction that keeps the turn open, never the one
# that lets us type into it.
_TIMESTAMP_GRANULARITY_SECONDS: Final[float] = 1.0


@pure
def is_turn_open_by_tail(events: Sequence[dict[str, Any]]) -> bool:
    """Whether the transcript tail LOOKS like a turn in flight, ignoring freshness.

    Never call this alone -- it cannot terminate (see the module docstring). It is the shape
    half of :meth:`TurnState.is_hold_required`, which supplies the bound.

    Mirrors ``activity_state.derive`` rungs 2/3/3a: a ``user_message`` or ``tool_result`` tail
    is agy about to speak next, and an ``assistant_message`` with no text is agy's
    between-tools planner step rather than a finished answer.
    """
    for event in reversed(events):
        event_type = event.get("type")
        if event_type in _OPEN_TAIL_TYPES:
            return True
        if event_type == "assistant_message":
            return not bool(event.get("text"))
    return False


def _tail_epoch(events: Sequence[dict[str, Any]]) -> float | None:
    """Epoch seconds of the newest event carrying a timestamp, or None.

    agy's parser stamps every event with the step's ISO-8601 ``created_at``; the shared
    converter is the same one the activity ladder's staleness gate uses.
    """
    for event in reversed(events):
        stamp = event.get("timestamp")
        if isinstance(stamp, str) and stamp:
            epoch = parse_iso_timestamp_to_epoch(stamp)
            if epoch is not None:
                return epoch
    return None


def _marker_mtime(state_dir: Path) -> float | None:
    try:
        return (state_dir / ACTIVE_MARKER_FILENAME).stat().st_mtime
    except OSError:
        return None


class TurnState:
    """One agent's turn-open reading and cancel-key interlock."""

    _lock: threading.Lock
    _events: tuple[dict[str, Any], ...]
    _is_published: bool
    _last_cancel_at: float
    _last_press_at: float
    _process_started_at: float | None

    @classmethod
    def build(cls) -> "TurnState":
        self = cls.__new__(cls)
        self._lock = threading.Lock()
        self._events = ()
        self._is_published = False
        self._last_cancel_at = 0.0
        self._last_press_at = 0.0
        self._process_started_at = None
        return self

    def publish(self, events: Sequence[dict[str, Any]], process_started_at: float | None) -> None:
        """The watcher's view of the transcript, from the same scan that emitted it."""
        with self._lock:
            self._events = tuple(events)
            self._is_published = True
            self._process_started_at = process_started_at

    def note_cancelled(self, *, now: float | None = None) -> None:
        """Record that WE just cancelled a turn.

        This is what makes the abandoned-tail case exact rather than heuristic: a cancel is the
        only in-process cause of a tail that will never be closed, and we are the ones causing
        it, so the tail's own timestamp can be compared against the moment we pressed.
        """
        with self._lock:
            self._last_cancel_at = time.time() if now is None else now

    def try_claim_press(self, *, now: float | None = None) -> bool:
        """Reserve the right to press ctrl+c. False means a press is too recent to be safe."""
        moment = time.time() if now is None else now
        with self._lock:
            if moment - self._last_press_at < MIN_PRESS_INTERVAL_SECONDS:
                return False
            self._last_press_at = moment
            return True

    def is_hold_required(self, state_dir: Path, *, is_watched: bool = True, now: float | None = None) -> bool:
        """Whether a turn is open, so a message must be held rather than typed.

        Rungs, in order, every one bounded:
          1. the marker was touched within ``BUSY_ASSERT_SECONDS`` -> a turn is open;
          2. the tail looks open AND predates our last cancel or the process start -> it was
             abandoned, so it is NOT open;
          3. the tail looks open and is younger than ``TAIL_OPEN_SECONDS`` -> a turn is open;
          4. otherwise -> not open.

        ``is_watched`` False means no watcher has ever published for this agent, so rung 2/3
        have nothing to read; the marker alone decides. Treating "unpublished" as busy would
        strand every message sent to an agent whose watcher does not exist.
        """
        moment = time.time() if now is None else now
        marker_at = _marker_mtime(state_dir)
        if marker_at is not None and moment - marker_at < BUSY_ASSERT_SECONDS:
            return True
        with self._lock:
            if not (self._is_published and is_watched):
                return False
            events = self._events
            abandoned_before = max(self._process_started_at or 0.0, self._last_cancel_at)
        if not is_turn_open_by_tail(events):
            return False
        tail_at = _tail_epoch(events)
        if tail_at is None:
            return False
        if tail_at + _TIMESTAMP_GRANULARITY_SECONDS <= abandoned_before:
            return False
        return moment - tail_at < TAIL_OPEN_SECONDS

    def user_turn_texts(self) -> tuple[str, ...]:
        """Every committed user message, oldest first -- the evidence a delivery landed.

        Reads ``content``, which is the key a ``user_message`` carries; ``text`` belongs to
        assistant messages. Getting this wrong makes every delivery unwitnessable, so the
        block is retyped until its attempts run out.
        """
        with self._lock:
            return tuple(str(e.get("content") or "") for e in self._events if e.get("type") == "user_message")

    def is_published(self) -> bool:
        with self._lock:
            return self._is_published


_STATES: Final[dict[str, TurnState]] = {}
_STATES_LOCK: Final[threading.Lock] = threading.Lock()


def get_turn_state(agent_id: str) -> TurnState:
    """The agent's shared reading, built on first use (see the module docstring)."""
    with _STATES_LOCK:
        existing = _STATES.get(agent_id)
        if existing is not None:
            return existing
        created = TurnState.build()
        _STATES[agent_id] = created
        return created


def drop_turn_state(agent_id: str) -> None:
    """Forget an agent (its watcher stopped). Safe to call for an unknown id."""
    with _STATES_LOCK:
        _STATES.pop(agent_id, None)
