"""Codex's activity tracker -- the codex peer of :class:`ClaudeActivityTracker`.

Codex's transcript carries authoritative turn boundaries (``turn_started`` /
``turn_completed`` / ``turn_aborted`` from the rollout in real time), so activity is a
**latch on those** rather than a lifecycle heuristic. The shared base owns signal caching
(the turn latch rides :meth:`_observe_extra`) and the universal liveness/staleness gates --
the dead-lifecycle gate matters here: the working derivation deliberately ignores the mngr
lifecycle (it is polled, hence laggy, and unreliable for codex), so the base gate is the
ONLY thing that settles a dead codex agent to IDLE.
"""

from collections.abc import Sequence
from typing import Any
from typing import ClassVar

from imbue.mngr_codex.codex_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.codex.activity_state import derive
from imbue.system_interface.harnesses.codex.activity_state import turn_open


class CodexActivityTracker(HarnessActivityTracker):
    """Codex: a latch on the transcript's real-time turn boundaries, refined to a tool verb."""

    # mngr_codex stamps this on every launch/resume; its mtime bounds transcript staleness so a turn
    # left open by a killed process is not mistaken for a live one.
    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME

    # mngr writes no `active` marker for codex (codex_config emits no marker hooks); the
    # daemon and its rollout turn markers are the turn authority.
    active_marker_filename: ClassVar[str | None] = None

    def _observe_extra(self, events: Sequence[dict[str, Any]]) -> tuple[Any, ...]:
        # Whether the latest turn marker is an open turn (``turn_started`` with no
        # ``turn_completed`` / ``turn_aborted`` after it).
        return (turn_open(events),)

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        # The mngr lifecycle and marker are intentionally unused -- the transcript turn
        # latch is authoritative (consistent with ``active_marker_filename = None``); the
        # base's dead gate already settled a positively-dead process.
        return derive(
            turn_open=bool(self._extra[0]) if self._extra else False,
            has_pending_tool_use=self._has_pending_tool_use,
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
