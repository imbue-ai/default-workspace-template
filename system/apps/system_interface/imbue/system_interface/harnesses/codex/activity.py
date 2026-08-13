"""Codex's activity tracker -- the codex peer of :class:`ClaudeActivityTracker`.

Codex's transcript carries authoritative turn boundaries (``turn_started`` / ``turn_completed`` /
``turn_aborted`` from the rollout in real time), so activity is a **latch on those** rather than a
lifecycle heuristic. This class caches the turn latch + the in-flight-tool signal + the tail time, and
dispatches to :func:`harnesses.codex.activity_state.derive`. The mngr lifecycle is intentionally NOT
used (it is polled -- hence laggy -- and unreliable for codex; see the module docstring).
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.mngr_codex.codex_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.codex.activity_state import derive
from imbue.system_interface.harnesses.codex.activity_state import has_pending_codex_tool_use
from imbue.system_interface.harnesses.codex.activity_state import turn_open


class CodexActivityTracker(HarnessActivityTracker):
    """Codex: a latch on the transcript's real-time turn boundaries, refined to a tool verb."""

    # mngr_codex stamps this on every launch/resume; its mtime bounds transcript staleness so a turn
    # left open by a killed process is not mistaken for a live one.
    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME

    # Whether the latest turn marker is an open turn (``turn_started`` with no
    # ``turn_completed`` / ``turn_aborted`` after it).
    _turn_open: bool

    def reset(self) -> None:
        super().reset()
        self._turn_open = False

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_pending_codex_tool_use(events)
        new_turn_open = turn_open(events)
        new_last_type = last_event_type(events)
        if (
            new_pending == self._has_pending_tool_use
            and new_turn_open == self._turn_open
            and new_last_type == self._last_event_type
        ):
            return False
        self._has_pending_tool_use = new_pending
        self._turn_open = new_turn_open
        self._last_event_type = new_last_type
        # Refreshed with the tail so the staleness check sees the current tail's time (inside the change
        # guard: an event that moves no derived signal leaves it alone and skips the recompute).
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        # The mngr lifecycle is unreliable (and laggy) for codex, so `is_agent_running` is
        # intentionally unused here -- the transcript turn latch is authoritative.
        return derive(
            turn_open=self._turn_open,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
