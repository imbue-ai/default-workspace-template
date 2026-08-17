"""Claude's activity tracker: the lifecycle-plus-tail inference.

The shared base owns signal caching and the universal liveness/staleness gates;
this class supplies only the working-turn question. The pure derivation lives
beside it in ``activity_state``. Registered in ``harnesses.registry``.
"""

from typing import ClassVar

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import resolve_is_agent_running
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.claude.activity_state import derive


class ClaudeActivityTracker(HarnessActivityTracker):
    """Claude: no turn markers in the transcript, so activity is inferred from
    the mngr lifecycle (with the ``active`` marker breaking the WAITING tie)
    plus the transcript tail. See :func:`derive`."""

    marker_filename: ClassVar[str] = "claude_process_started"

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        return derive(
            is_agent_running=resolve_is_agent_running(lifecycle_state, is_active_marker_present),
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_type=self._last_event_type,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
