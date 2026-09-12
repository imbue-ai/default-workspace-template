"""A ``TranscriptReader`` held in memory, for tests that read a chat's transcript through a segment."""

from typing import Any

from imbue.chat.harnesses.session_watcher import TranscriptReader


class ListTranscriptReader(TranscriptReader):
    """A transcript held as a list of event ids, in order; the detail of an event is its id."""

    def __init__(self, event_ids: list[str]) -> None:
        self._events = [{"event_id": event_id} for event_id in event_ids]

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._events[-limit:]

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        index = self.get_event_offset(before_event_id)
        if index < 0:
            return []
        return self._events[max(0, index - limit) : index]

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        index = self.get_event_offset(after_event_id)
        if index < 0:
            return []
        return self._events[index + 1 : index + 1 + limit]

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._events[offset : offset + limit]

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return next((index for index, event in enumerate(self._events) if event["event_id"] == event_id), -1)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return len(self._events)

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        return {"tool_input": event_id} if self.get_event_offset(event_id) >= 0 else None

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None
