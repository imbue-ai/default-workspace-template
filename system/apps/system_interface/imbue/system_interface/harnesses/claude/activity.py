"""Claude's activity tracker: the cached transcript signals its derivation needs.

The pure derivation lives beside this in ``activity_state``; this class only caches
signals and dispatches. Registered in ``harnesses.registry``.
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


class ClaudeActivityTracker(HarnessActivityTracker):
    """Claude: no turn markers in the transcript, so activity is inferred from
    the mngr lifecycle plus the transcript tail. See :func:`derive`."""

    marker_filename: ClassVar[str] = "claude_process_started"

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_last_type = last_event_type(events)
        if new_pending == self._has_pending_tool_use and new_last_type == self._last_event_type:
            return False
        self._has_pending_tool_use = new_pending
        self._last_event_type = new_last_type
        # Refreshed alongside the type so the stale-tail check sees the current
        # tail's time. Deliberately inside the change guard: an event that moves
        # no derived signal leaves the timestamp alone and skips the recompute.
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
