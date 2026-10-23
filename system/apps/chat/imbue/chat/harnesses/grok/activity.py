"""Grok's activity tracker: a latch per top-level session on the transcript's turn markers.

mngr_grok's ``active`` marker stays on while a background subagent runs, after the turn that
started it has ended, so the tracker declares none and the chat reads such a wait as
``background`` rather than ``working``. The working derivation ignores the mngr lifecycle; the
base class's dead-lifecycle gate settles a grok that is gone.
"""

from typing import Any
from typing import ClassVar
from typing import Final

from imbue.chat.activity_state import ActivityState
from imbue.chat.harnesses.activity import HarnessActivityTracker
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.grok.activity_state import derive
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME

_TURN_STARTED_KIND: Final[str] = SpecialEventKind.TURN_STARTED.value
_TURN_CLOSING_KINDS: Final[frozenset[str]] = frozenset(
    {SpecialEventKind.TURN_COMPLETED.value, SpecialEventKind.TURN_ABORTED.value}
)


class GrokActivityTracker(HarnessActivityTracker):
    """Grok: whether a top-level session has a turn open, from its turn markers, refined to a tool verb."""

    # mngr_grok's launch script touches this on every launch; its mtime bounds transcript staleness.
    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME

    active_marker_filename: ClassVar[str | None] = None

    # Each top-level session's open turn, by session id, with the time its turn_started marker carries.
    _open_turn_started_at_by_session: dict[str | None, float | None]
    # Each session's newest turn marker time, so a re-delivered older marker cannot move its latch.
    _latch_at_by_session: dict[str | None, float | None]
    # Each tool call still without a result, by id, with the time of the event that made it.
    _pending_tool_call_at_by_id: dict[str, float | None]
    # Every tool call a result has answered, so a re-delivered call event cannot make it pending again.
    _answered_tool_call_ids: set[str]

    def _reset_extra(self) -> None:
        self._open_turn_started_at_by_session = {}
        self._latch_at_by_session = {}
        self._pending_tool_call_at_by_id = {}
        self._answered_tool_call_ids = set()

    def _fold_extra_event(self, event: dict[str, Any], event_at: float | None) -> None:
        event_type = event.get("type")
        if event_type == "assistant_message":
            self._fold_tool_calls(event, event_at)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                self._answered_tool_call_ids.add(tool_call_id)
                self._pending_tool_call_at_by_id.pop(tool_call_id, None)
        elif event_type == SPECIAL_EVENT_TYPE:
            self._fold_turn_marker(event, event_at)
        else:
            pass

    def _fold_tool_calls(self, event: dict[str, Any], event_at: float | None) -> None:
        for tool_call in event.get("tool_calls") or ():
            tool_call_id = tool_call.get("tool_call_id")
            if tool_call_id and tool_call_id not in self._answered_tool_call_ids:
                self._pending_tool_call_at_by_id.setdefault(tool_call_id, event_at)

    def _fold_turn_marker(self, event: dict[str, Any], event_at: float | None) -> None:
        kind = event.get("kind")
        if kind != _TURN_STARTED_KIND and kind not in _TURN_CLOSING_KINDS:
            return
        session_id = event.get("session_id")
        if not self._advances(event_at, self._latch_at_by_session.get(session_id)):
            return
        if event_at is not None:
            self._latch_at_by_session[session_id] = event_at
        if kind == _TURN_STARTED_KIND:
            self._open_turn_started_at_by_session[session_id] = event_at
        else:
            self._open_turn_started_at_by_session.pop(session_id, None)

    def _current_extra(self) -> tuple[Any, ...]:
        return (
            frozenset(self._open_turn_started_at_by_session.items()),
            frozenset(self._pending_tool_call_at_by_id.items()),
        )

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        open_turns, pending_tool_calls = self._extra if self._extra else (frozenset(), frozenset())
        return derive(
            open_turn_started_ats=[started_at for _session_id, started_at in open_turns],
            pending_tool_call_ats=[called_at for _tool_call_id, called_at in pending_tool_calls],
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
