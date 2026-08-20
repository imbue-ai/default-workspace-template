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
from imbue.system_interface.activity_state import resolve_is_agent_running
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

    # mngr_antigravity does not write this marker YET -- unlike mngr_claude/codex/pi_coding,
    # it writes no startup/resume marker at all. Naming the file it SHOULD write keeps the
    # declaration honest: while it is absent the base reads ``process_started_at = None`` and
    # its staleness gate is inert, which costs agy nothing because the ``active`` marker below
    # is *removed* when agy goes idle -- so a restarted-but-idle agent already reads
    # not-running and settles IDLE without a staleness rung. Adding the marker on the mngr
    # side tightens the mid-turn-restart case; it does not unblock anything here.
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
        return derive(
            is_agent_running=resolve_is_agent_running(lifecycle_state, is_active_marker_present),
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_is_final_answer=tail_is_final,
        )
