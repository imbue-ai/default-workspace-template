"""antigravity's activity tracker: the lifecycle-plus-tail inference, with one agy quirk.

The shared base owns signal caching and the universal liveness/staleness gates; this class
supplies the working-turn question plus ONE extra cached signal agy needs and the other
harnesses do not -- see :func:`_tail_is_final_answer`. The pure derivation lives beside it
in ``activity_state``. Registered in ``harnesses.registry``.
"""

from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_lifecycle_dead
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.antigravity.activity_state import derive

# The event types that count as "message" events for the tail-is-final-answer signal.
_MESSAGE_EVENT_TYPES = ("assistant_message", "user_message", "tool_result")


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

    # agy's one extra signal, folded from the stream: whether the newest MESSAGE event is
    # an ``assistant_message`` carrying real answer text -- the signal that the turn is
    # over. An empty planner step (agy's between-tool "thinking" step) reads as
    # still-working, so the indicator does not flicker IDLE mid-turn. Timestamp-guarded so
    # a re-delivered old event cannot regress the tail.
    _tail_message_type: str | None
    _tail_has_answer_text: bool
    _tail_message_at: float | None

    def _reset_extra(self) -> None:
        self._tail_message_type = None
        self._tail_has_answer_text = False
        self._tail_message_at = None

    def _fold_extra_event(self, event: dict[str, Any], event_at: float | None) -> None:
        event_type = event.get("type")
        if event_type not in _MESSAGE_EVENT_TYPES:
            return
        if not self._advances(event_at, self._tail_message_at):
            return
        self._tail_message_type = str(event_type)
        self._tail_has_answer_text = event_type == "assistant_message" and bool(event.get("text"))
        self._tail_message_at = event_at if event_at is not None else self._tail_message_at

    def _current_extra(self) -> tuple[Any, ...]:
        return (self._tail_message_type == "assistant_message" and self._tail_has_answer_text,)

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
