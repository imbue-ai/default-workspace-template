"""The chat transcript facade: one segment today, read through the chat's own API."""

from typing import Any

import pytest

from imbue.chat.chat_transcript import ChatTranscript
from imbue.chat.chat_transcript import ChatTranscriptError
from imbue.chat.chat_transcript import TranscriptSegment
from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.chat.primitives import ChatId


class _ListReader(TranscriptReader):
    """A transcript held as a list of event ids."""

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
        return self._events[max(0, index - limit) : index]

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        index = self.get_event_offset(after_event_id)
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


def _ids(events: list[dict[str, Any]]) -> list[str]:
    return [event["event_id"] for event in events]


def test_a_single_segment_transcript_reads_as_its_one_agent_does() -> None:
    reader = _ListReader(["e1", "e2", "e3", "e4"])
    transcript = ChatTranscript.of_single_segment(ChatId("agent-1"), "agent-1", reader)

    assert transcript.segments[0].agent_id == "agent-1"
    assert _ids(transcript.get_tail_events(2)) == ["e3", "e4"]
    assert _ids(transcript.get_backfill_events("e3", 5)) == ["e1", "e2"]
    assert _ids(transcript.get_forward_events("e2", 1)) == ["e3"]
    assert _ids(transcript.get_events_at_offset(1, 2)) == ["e2", "e3"]
    assert transcript.get_event_offset("e4") == 3
    assert transcript.get_event_offset("missing") == -1
    assert transcript.get_total_event_count() == 4
    assert transcript.get_event_detail("e2") == {"tool_input": "e2"}
    assert transcript.get_event_detail("missing") is None


def test_a_transcript_with_several_segments_refuses_to_read_until_it_can_concatenate_them() -> None:
    segments = tuple(
        TranscriptSegment(agent_id=agent_id, reader=_ListReader([f"{agent_id}-e1"]))
        for agent_id in ("agent-1", "agent-2")
    )
    transcript = ChatTranscript(chat_id=ChatId("agent-1"), segments=segments)

    with pytest.raises(ChatTranscriptError):
        transcript.get_total_event_count()
