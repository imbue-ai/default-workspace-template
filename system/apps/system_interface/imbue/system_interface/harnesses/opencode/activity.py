"""opencode's activity tracker.

First cut: the placeholder watcher emits no events, so activity is the mngr lifecycle
plus the transcript tail -- the same derivation claude uses, reused directly rather than
duplicated. With no events this always derives IDLE; it becomes meaningful once the real
opencode transcript watcher lands (opencode's ``session.idle`` gives true turn
boundaries, so a codex-style turn-aware derivation can replace this then).
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


class OpenCodeActivityTracker(HarnessActivityTracker):
    """opencode: first cut infers activity from the mngr lifecycle plus the transcript
    tail (the same derivation claude uses)."""

    # opencode's launch script clears then writes this readiness sentinel on every
    # startup/resume once the server is up; its mtime bounds transcript staleness. Kept in
    # sync with READY_SENTINEL_FILENAME in mngr_opencode's opencode_config.py.
    marker_filename: ClassVar[str] = "opencode_ready"

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
