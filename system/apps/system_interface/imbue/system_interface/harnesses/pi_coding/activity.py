"""pi's activity tracker.

pi's transcript carries no turn-boundary markers (like claude, unlike codex), so
activity is the same lifecycle-plus-tail heuristic claude uses -- reused directly
rather than duplicated. With the placeholder watcher emitting no events this always
derives IDLE; it becomes meaningful once the real pi transcript watcher lands.
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.claude.activity_state import derive


class PiActivityTracker(HarnessActivityTracker):
    """pi: no turn markers, so activity is inferred from the mngr lifecycle plus the
    transcript tail (the same derivation claude uses)."""

    # mngr_pi_coding touches this on launch/resume; its mtime bounds transcript staleness.
    marker_filename: ClassVar[str] = "pi_process_started"

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        if new_pending == self._has_pending_tool_use and new_last_type == self._last_event_type:
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        return derive(
            is_agent_running=is_agent_running,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
