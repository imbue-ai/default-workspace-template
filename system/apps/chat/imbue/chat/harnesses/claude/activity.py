"""Claude's activity tracker: the lifecycle-plus-tail inference.

The shared base owns signal caching and the universal liveness/staleness gates;
this class supplies only the working-turn question. The pure derivation lives
beside it in ``activity_state``. Registered in ``harnesses.registry``.
"""

from typing import ClassVar
from typing import Final

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import resolve_is_agent_running
from imbue.chat.harnesses.activity import HarnessActivityTracker
from imbue.chat.harnesses.claude.activity_state import derive
from imbue.chat.harnesses.startup_readiness import StartupReadyMarker

# Written by claude's SessionStart hook; mngr's launch command deletes it first.
CLAUDE_STARTUP_READY_MARKER: Final[StartupReadyMarker] = StartupReadyMarker(
    filename="session_started", is_deleted_at_launch=True
)


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
