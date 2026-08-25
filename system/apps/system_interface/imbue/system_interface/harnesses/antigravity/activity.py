"""antigravity's activity tracker: the lifecycle-plus-tail inference, with one agy quirk.

The shared base owns signal caching and the universal liveness/staleness gates; this class
supplies the working-turn question plus ONE extra cached signal agy needs and the other
harnesses do not -- see :func:`_tail_is_final_answer`. The pure derivation lives beside it
in ``activity_state``. Registered in ``harnesses.registry``.
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.antigravity.activity_state import derive


def _tail_is_final_answer(events: Sequence[dict[str, Any]]) -> bool:
    """True when the newest message event is an ``assistant_message`` carrying real answer
    text -- the signal that the turn is over. An empty planner step (agy's between-tool
    "thinking" step) returns False, so the indicator does not flicker IDLE mid-turn."""
    for event in reversed(events):
        event_type = event.get("type")
        if event_type == "assistant_message":
            return bool(event.get("text"))
        if event_type in ("user_message", "tool_result"):
            return False
    return False


class AntigravityActivityTracker(HarnessActivityTracker):
    """agy: no turn markers, so activity is inferred from the mngr lifecycle plus the
    transcript tail (with an empty planner tail read as still-working, not finished)."""

    # Written by mngr_antigravity on every launch/resume, like the peer harnesses' own
    # ``*_process_started`` markers. Load-bearing here, not decoration: agy resumes from its
    # own store, so after a mid-turn restart the PREVIOUS process's tail is still present --
    # including an unmatched tool call no later event will ever close. The base's staleness
    # gate compares the tail against this marker's mtime and settles IDLE; without it the
    # indicator latches on that dead tool call and never clears.
    marker_filename: ClassVar[str] = "antigravity_process_started"

    def _observe_extra(self, events: Sequence[dict[str, Any]]) -> tuple[Any, ...]:
        """agy's one extra signal: whether the tail is a REAL answer or an empty planner step."""
        return (_tail_is_final_answer(events),)

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        # ``_extra`` is () until the first observe(); an unobserved tracker has no tail, so
        # "the tail is a final answer" is False -- which derive reads as "not finished",
        # harmless because with no tail at all it falls through to IDLE anyway.
        tail_is_final = bool(self._extra[0]) if self._extra else False
        # The same call claude's tracker makes, plus agy's one extra signal. The staleness
        # inputs are passed even though the shared base has already applied that gate, so the
        # two harnesses' derivations stay literally comparable.
        return derive(
            # NOT resolve_is_agent_running: that folds the marker into liveness, and agy's
            # marker is absent for the whole of every tool call (its statusLine reports only
            # idle/thinking), so it would force IDLE mid-chain. agy's dot is decided by the
            # transcript alone; ``is_active_marker_present`` is deliberately unused here --
            # see activity_state.derive for why the marker must never assert THINKING.
            is_agent_alive=not is_lifecycle_dead(lifecycle_state),
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_is_final_answer=tail_is_final,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
