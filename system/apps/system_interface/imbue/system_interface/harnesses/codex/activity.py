"""Codex's activity tracker -- the codex peer of :class:`ClaudeActivityTracker`.

Codex's transcript carries authoritative turn boundaries (``turn_started`` /
``turn_completed`` / ``turn_aborted`` from the rollout in real time), so activity is a
**latch on those** rather than a lifecycle heuristic. The shared base owns signal caching
(the turn latch rides :meth:`_observe_extra`) and the universal liveness/staleness gates --
the dead-lifecycle gate matters here: the working derivation deliberately ignores the mngr
lifecycle (it is polled, hence laggy, and unreliable for codex), so the base gate is the
ONLY thing that settles a dead codex agent to IDLE.
"""

from typing import Any
from typing import ClassVar

from imbue.mngr_codex.codex_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.harnesses.activity import HarnessActivityTracker
from imbue.system_interface.harnesses.codex.activity_state import derive
from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.events import SpecialEventKind

# The kinds that close a turn; ``turn_started`` opens one.
_TURN_CLOSING_KINDS = (SpecialEventKind.TURN_COMPLETED.value, SpecialEventKind.TURN_ABORTED.value)


class CodexActivityTracker(HarnessActivityTracker):
    """Codex: a latch on the transcript's real-time turn boundaries, refined to a tool verb."""

    # mngr_codex stamps this on every launch/resume; its mtime bounds transcript staleness so a turn
    # left open by a killed process is not mistaken for a live one.
    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME

    # mngr writes no `active` marker for codex (codex_config emits no marker hooks); the
    # daemon and its rollout turn markers are the turn authority.
    active_marker_filename: ClassVar[str | None] = None

    # The turn latch, folded from turn markers as they stream in: whether the newest marker
    # opened a turn, guarded by the marker's timestamp so a re-delivered old marker cannot
    # regress it.
    _is_turn_open: bool
    _latch_at: float | None

    def _reset_extra(self) -> None:
        self._is_turn_open = False
        self._latch_at = None

    def _fold_extra_event(self, event: dict[str, Any], event_at: float | None) -> None:
        if event.get("type") != SPECIAL_EVENT_TYPE:
            return
        kind = event.get("kind")
        if kind != SpecialEventKind.TURN_STARTED.value and kind not in _TURN_CLOSING_KINDS:
            return
        if not self._advances(event_at, self._latch_at):
            return
        self._is_turn_open = kind == SpecialEventKind.TURN_STARTED.value
        self._latch_at = event_at if event_at is not None else self._latch_at

    def _current_extra(self) -> tuple[Any, ...]:
        # Whether the latest turn marker is an open turn (``turn_started`` with no
        # ``turn_completed`` / ``turn_aborted`` after it).
        return (self._is_turn_open,)

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
