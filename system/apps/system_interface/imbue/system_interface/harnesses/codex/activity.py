"""Codex's activity tracker: the cached transcript signals its derivation needs.

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
from imbue.system_interface.harnesses.codex.activity_state import codex_turn_open
from imbue.system_interface.harnesses.codex.activity_state import derive_codex
from imbue.system_interface.harnesses.activity import HarnessActivityTracker


class CodexActivityTracker(HarnessActivityTracker):
    """Codex: the transcript carries authoritative turn boundaries, so activity
    is a latch on those rather than a lifecycle heuristic. See :func:`derive_codex`."""

    marker_filename: ClassVar[str] = "codex_process_started"

    # Whether the latest turn marker is an open turn (``turn_started`` with no
    # ``turn_completed``/``turn_aborted`` after it).
    _turn_open: bool

    def reset(self) -> None:
        super().reset()
        self._turn_open = False

    def observe(self, events: Sequence[dict[str, Any]]) -> bool:
        new_pending = has_unmatched_tool_use(events)
        new_turn_open = codex_turn_open(events)
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
        self._last_event_timestamp = last_event_timestamp(events)
        return True

    def derive(self, *, is_agent_running: bool, process_started_at: float | None) -> ActivityState:
        # The mngr lifecycle is unreliable for codex, so `is_agent_running` is
        # intentionally unused here -- the turn latch is authoritative.
        return derive_codex(
            turn_open=self._turn_open,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
