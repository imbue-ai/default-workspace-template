"""Is one of agy's turns open right now? -- shared between the watcher, the session and the tap.

agy's ``active`` marker cannot answer this on its own. Its statusLine reports exactly two
states, ``idle`` and ``thinking`` (there is no ``tool_calling``), so the marker is REMOVED
for the whole of every tool call. Anything that reads the marker alone concludes "no turn is
open" in the middle of a tool chain. For the activity dot that is a flicker; for
:meth:`AntigravityHarnessSession.send` it is a swallow -- the message is typed into a live
turn, agy merges it into that turn, and it never gets one of its own (contract A1a).

The transcript answers it correctly, because a tool chain is exactly what the tail shows: an
unmatched tool call, or a ``tool_result``/``user_message`` tail, or agy's empty
PLANNER_RESPONSE. That is the same evidence :func:`activity_state.derive` climbs, kept here
as one pure function so the dot and the send decision can never disagree.

The marker is still worth OR-ing in HERE, and deliberately nowhere else. This answers "should
I hold this message?", where a wrong answer is asymmetric: a stale marker makes us hold a
message (late, recoverable), while missing an open turn types into it (a swallow). The
activity ladder answers a different question -- "what does the dot say?" -- and there the same
OR is a bug: no watcher notifies on a marker-only change, so a dot the marker alone can turn
on has no edge that turns it off, and it latches. See ``activity_state.derive``.

The asymmetry is the point: this reads the marker live off disk on every call, so it is never
stale in the way a cached activity state is.

Published by the watcher because it is the component that reads the transcript; read through
the module registry by the session and the tap, exactly as ``queue_tracker`` is shared.
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME

_OPEN_TAIL_TYPES: Final[frozenset[str]] = frozenset({"user_message", "tool_result"})


@pure
def is_turn_open_by_tail(events: Sequence[dict[str, Any]]) -> bool:
    """Whether the transcript tail says a turn is still in flight.

    Mirrors ``activity_state.derive`` rungs 2/3/3a, in the same order and for the same
    reasons: an unmatched ``tool_call`` is a running tool; a ``user_message`` or
    ``tool_result`` tail is agy about to speak next; an ``assistant_message`` with no text is
    agy's between-tools planner step, NOT a finished answer.
    """
    open_tool_call_ids: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "tool_call":
            open_tool_call_ids.add(str(event.get("tool_call_id")))
        elif event_type == "tool_result":
            open_tool_call_ids.discard(str(event.get("tool_call_id")))
        else:
            # Messages carry no tool bookkeeping; the tail scan below reads them.
            pass
    if open_tool_call_ids:
        return True
    for event in reversed(events):
        event_type = event.get("type")
        if event_type in _OPEN_TAIL_TYPES:
            return True
        if event_type == "assistant_message":
            # Empty == agy's planner step mid-chain; real text == the turn finished.
            return not bool(event.get("text"))
    return False


class TurnState:
    """One agent's latest turn-open reading, published by its watcher."""

    _lock: threading.Lock
    _is_open_by_tail: bool

    @classmethod
    def build(cls) -> "TurnState":
        self = cls.__new__(cls)
        self._lock = threading.Lock()
        self._is_open_by_tail = False
        return self

    def publish(self, *, is_open_by_tail: bool) -> None:
        with self._lock:
            self._is_open_by_tail = is_open_by_tail

    def is_turn_open(self, state_dir: Path) -> bool:
        """The transcript's answer, OR the marker for a turn that committed between polls."""
        with self._lock:
            if self._is_open_by_tail:
                return True
        return (state_dir / ACTIVE_MARKER_FILENAME).exists()


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
