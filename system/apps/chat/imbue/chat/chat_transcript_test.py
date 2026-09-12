"""The chat transcript facade: one segment today, read through the chat's own API."""

from typing import Any

import pytest

from imbue.chat.chat_transcript import ChatTranscript
from imbue.chat.chat_transcript import ChatTranscriptError
from imbue.chat.chat_transcript import TranscriptSegment
from imbue.chat.harnesses.mock_transcript_reader_test import ListTranscriptReader
from imbue.chat.primitives import ChatId


def _ids(events: list[dict[str, Any]]) -> list[str]:
    return [event["event_id"] for event in events]


def test_a_single_segment_transcript_reads_as_its_one_agent_does() -> None:
    reader = ListTranscriptReader(["e1", "e2", "e3", "e4"])
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
        TranscriptSegment(agent_id=agent_id, reader=ListTranscriptReader([f"{agent_id}-e1"]))
        for agent_id in ("agent-1", "agent-2")
    )
    transcript = ChatTranscript(chat_id=ChatId("agent-1"), segments=segments)

    with pytest.raises(ChatTranscriptError):
        transcript.get_total_event_count()
