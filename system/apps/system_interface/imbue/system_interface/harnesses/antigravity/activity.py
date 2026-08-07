"""antigravity's activity tracker.

Caches the transcript tail signals agy's derivation needs and dispatches to the pure
:func:`antigravity.activity_state.derive`. Unlike claude/codex, agy exposes no
``*_process_started`` marker: its mngr ``active`` lifecycle marker (surfaced as
``is_agent_running``) is the authoritative turn signal, and it already forces IDLE after a
restart, so there is no stale-tail rung and ``process_started_at`` is unused here (see the
module docstring in ``activity_state``).
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
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

    # Required by the base to locate a ``*_process_started`` marker whose mtime bounds
    # staleness. agy writes no such marker and this tracker runs no staleness rung, so the
    # value is unused; it points at the real mngr lifecycle marker rather than a fictional
    # filename, and ``derive`` ignores the ``process_started_at`` the base reads from it.
    marker_filename: ClassVar[str] = "active"

    _tail_is_final: bool

    def reset(self) -> None:
        super().reset()
        self._tail_is_final = False

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        new_tail_final = _tail_is_final_answer(events)
        if (
            new_pending == self._has_pending_tool_use
            and new_last_type == self._last_event_type
            and new_tail_final == self._tail_is_final
        ):
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        self._last_event_timestamp = last_event_timestamp(events)
        self._tail_is_final = new_tail_final
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        return derive(
            is_agent_running=is_agent_running,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_is_final_answer=self._tail_is_final,
        )
