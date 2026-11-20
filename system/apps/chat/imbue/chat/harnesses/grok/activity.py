"""Grok's activity tracker."""

from typing import Any
from typing import ClassVar
from typing import Final

from imbue.chat.activity_state import ActivityState
from imbue.chat.activity_state import resolve_is_turn_in_flight
from imbue.chat.harnesses.activity import HarnessActivityTracker
from imbue.chat.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.chat.harnesses.events import SpecialEventKind
from imbue.chat.harnesses.grok.activity_state import derive
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME

_TURN_STARTED_KIND: Final[str] = SpecialEventKind.TURN_STARTED.value


class GrokActivityTracker(HarnessActivityTracker):
    """Grok: the lifecycle and the shared ``active`` marker say whether a turn runs; a top-level session's tool call
    with no result says it is running a tool."""

    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME

    # The sessions whose turns are the chat's: only a top-level session's parse emits turn_started.
    _root_session_ids: set[str]
    # Each tool call still without a result, by id, with its session and the time of the event that made it.
    _pending_tool_call_by_id: dict[str, tuple[str | None, float | None]]

    def _reset_extra(self) -> None:
        self._root_session_ids = set()
        self._pending_tool_call_by_id = {}

    def _fold_extra_event(self, event: dict[str, Any], event_at: float | None) -> None:
        event_type = event.get("type")
        if event_type == "assistant_message":
            self._fold_tool_calls(event, event_at)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                self._pending_tool_call_by_id.pop(tool_call_id, None)
        elif event_type == SPECIAL_EVENT_TYPE:
            session_id = event.get("session_id")
            if event.get("kind") == _TURN_STARTED_KIND and session_id:
                self._root_session_ids.add(session_id)
        else:
            pass

    def _fold_tool_calls(self, event: dict[str, Any], event_at: float | None) -> None:
        session_id = event.get("session_id")
        for tool_call in event.get("tool_calls") or ():
            tool_call_id = tool_call.get("tool_call_id")
            if tool_call_id and tool_call_id not in self._matched_tool_call_ids:
                self._pending_tool_call_by_id.setdefault(tool_call_id, (session_id, event_at))

    def _current_extra(self) -> tuple[Any, ...]:
        return (
            frozenset(
                (tool_call_id, called_at)
                for tool_call_id, (session_id, called_at) in self._pending_tool_call_by_id.items()
                if session_id in self._root_session_ids
            ),
        )

    def _derive_working(
        self, *, lifecycle_state: str, is_active_marker_present: bool, process_started_at: float | None
    ) -> ActivityState:
        pending_tool_calls = self._extra[0] if self._extra else frozenset()
        return derive(
            is_turn_in_flight=resolve_is_turn_in_flight(lifecycle_state, is_active_marker_present),
            pending_tool_call_ats=[called_at for _tool_call_id, called_at in pending_tool_calls],
            tail_event_at=self._tail_event_at,
            process_started_at=process_started_at,
        )
