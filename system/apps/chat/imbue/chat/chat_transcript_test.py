"""The chat transcript facade: one read API over a chat's segments and the switch chips between them."""

from datetime import datetime
from datetime import timezone
from typing import Any

import pytest

from imbue.chat.chat_transcript import AGENT_SWITCH_EVENT_TYPE
from imbue.chat.chat_transcript import ChatTranscript
from imbue.chat.chat_transcript import ChatTranscriptError
from imbue.chat.chat_transcript import TranscriptSegment
from imbue.chat.chat_transcript import agent_switch_event_id
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.mock_transcript_reader_test import ListTranscriptReader
from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.chat.primitives import ChatId

_CHAT_ID = ChatId("agent-first")
_SWITCH_1 = agent_switch_event_id(_CHAT_ID, 1)
_SWITCH_2 = agent_switch_event_id(_CHAT_ID, 2)


def _ids(events: list[dict[str, Any]]) -> list[str]:
    return [event["event_id"] for event in events]


def _segment(agent_id: str, seq: int, recorded_event_count: int | None, harness: HarnessType) -> TranscriptSegment:
    is_archived = recorded_event_count is not None
    return TranscriptSegment(
        agent_id=agent_id,
        harness=harness,
        seq=seq,
        recorded_event_count=recorded_event_count,
        ended_at=datetime(2026, 9, 1, 12, seq, tzinfo=timezone.utc) if is_archived else None,
    )


class _LoadRecorder:
    """Hands out readers for archived segments and remembers which were asked for."""

    def __init__(self, reader_by_agent_id: dict[str, TranscriptReader]) -> None:
        self.reader_by_agent_id = reader_by_agent_id
        self.loaded: list[str] = []

    def __call__(self, segment: TranscriptSegment) -> TranscriptReader:
        self.loaded.append(segment.agent_id)
        return self.reader_by_agent_id[segment.agent_id]


def _three_segment_transcript() -> tuple[ChatTranscript, _LoadRecorder]:
    """first: a1 a2 a3 (archived, 3 recorded) | switch:1 | second: b1 b2 (archived, 2 recorded) | switch:2 | third: c1 c2 c3 c4 (live)."""
    readers: dict[str, TranscriptReader] = {
        "agent-first": ListTranscriptReader(["a1", "a2", "a3"]),
        "agent-second": ListTranscriptReader(["b1", "b2"]),
    }
    loader = _LoadRecorder(readers)
    transcript = ChatTranscript.build(
        _CHAT_ID,
        (
            _segment("agent-first", 1, 3, HarnessType.CLAUDE),
            _segment("agent-second", 2, 2, HarnessType.CODEX),
            _segment("agent-third", 3, None, HarnessType.CLAUDE),
        ),
        {"agent-third": ListTranscriptReader(["c1", "c2", "c3", "c4"])},
        loader,
    )
    return transcript, loader


def test_a_single_segment_transcript_reads_as_its_one_agent_does() -> None:
    reader = ListTranscriptReader(["e1", "e2", "e3", "e4"])
    # The chat of one agent: its segment is the live one, and nothing is ever loaded (the
    # recorder holds no reader, so a load would raise).
    transcript = ChatTranscript.build(
        ChatId("agent-1"),
        (_segment("agent-1", 1, None, HarnessType.CLAUDE),),
        {"agent-1": reader},
        _LoadRecorder({}),
    )

    assert transcript.segments[0].agent_id == "agent-1"
    assert _ids(transcript.get_tail_events(2)) == ["e3", "e4"]
    assert _ids(transcript.get_backfill_events("e3", 5)) == ["e1", "e2"]
    assert _ids(transcript.get_forward_events("e2", 1)) == ["e3"]
    assert transcript.get_backfill_events("missing", 5) == []
    assert transcript.get_forward_events("missing", 5) == []
    assert _ids(transcript.get_events_at_offset(1, 2)) == ["e2", "e3"]
    assert transcript.get_event_offset("e4") == 3
    assert transcript.get_event_offset("missing") == -1
    assert transcript.get_total_event_count() == 4
    assert transcript.get_event_detail("e2") == {"tool_input": "e2"}
    assert transcript.get_event_detail("missing") is None


def test_a_transcript_needs_at_least_one_segment() -> None:
    with pytest.raises(ChatTranscriptError):
        ChatTranscript.build(_CHAT_ID, (), {}, lambda _segment: ListTranscriptReader([]))


def test_totals_and_offsets_come_from_the_recorded_counts_without_loading() -> None:
    transcript, loader = _three_segment_transcript()

    # 3 + 1 (switch) + 2 + 1 (switch) + 4.
    assert transcript.get_total_event_count() == 11
    assert transcript.get_event_offset("c1") == 7
    assert transcript.get_event_offset(_SWITCH_2) == 6
    assert transcript.get_event_offset(_SWITCH_1) == 3
    assert loader.loaded == []


def test_the_tail_crosses_into_earlier_segments_with_the_switch_chips_between() -> None:
    transcript, loader = _three_segment_transcript()

    assert _ids(transcript.get_tail_events(3)) == ["c2", "c3", "c4"]
    assert loader.loaded == []

    tail = transcript.get_tail_events(8)
    assert _ids(tail) == [_SWITCH_1, "b1", "b2", _SWITCH_2, "c1", "c2", "c3", "c4"]
    # The eighth event is the first chip, which needs no segment behind it.
    assert loader.loaded == ["agent-second"]
    assert _ids(transcript.get_tail_events(9)) == ["a3", _SWITCH_1, "b1", "b2", _SWITCH_2, "c1", "c2", "c3", "c4"]
    assert loader.loaded == ["agent-second", "agent-first"]
    switch = next(event for event in tail if event["event_id"] == _SWITCH_2)
    assert switch["type"] == AGENT_SWITCH_EVENT_TYPE
    assert (switch["from_agent_id"], switch["to_agent_id"]) == ("agent-second", "agent-third")
    assert (switch["from_harness"], switch["to_harness"]) == ("codex", "claude")
    assert switch["agent_id"] == "agent-third"
    assert switch["seq"] == 2
    assert switch["timestamp"] == "2026-09-01T12:02:00+00:00"


def test_a_backfill_pages_across_segments_and_from_a_switch_chip() -> None:
    transcript, _loader = _three_segment_transcript()

    assert _ids(transcript.get_backfill_events("c2", 2)) == [_SWITCH_2, "c1"]
    assert _ids(transcript.get_backfill_events("c1", 3)) == ["b1", "b2", _SWITCH_2]
    assert _ids(transcript.get_backfill_events(_SWITCH_2, 3)) == [_SWITCH_1, "b1", "b2"]
    assert _ids(transcript.get_backfill_events("b1", 10)) == ["a1", "a2", "a3", _SWITCH_1]
    assert transcript.get_backfill_events("a1", 5) == []
    assert transcript.get_backfill_events("missing", 5) == []


def test_a_forward_read_pages_across_segments_and_from_a_switch_chip() -> None:
    transcript, _loader = _three_segment_transcript()

    assert _ids(transcript.get_forward_events("a2", 3)) == ["a3", _SWITCH_1, "b1"]
    assert _ids(transcript.get_forward_events(_SWITCH_1, 4)) == ["b1", "b2", _SWITCH_2, "c1"]
    assert _ids(transcript.get_forward_events("b2", 10)) == [_SWITCH_2, "c1", "c2", "c3", "c4"]
    assert transcript.get_forward_events("c4", 5) == []
    assert transcript.get_forward_events("missing", 5) == []


def test_a_jump_to_an_offset_lands_in_the_right_segment_and_pages_onward() -> None:
    transcript, loader = _three_segment_transcript()

    assert _ids(transcript.get_events_at_offset(7, 2)) == ["c1", "c2"]
    assert loader.loaded == []
    assert _ids(transcript.get_events_at_offset(2, 5)) == ["a3", _SWITCH_1, "b1", "b2", _SWITCH_2]
    assert _ids(transcript.get_events_at_offset(3, 2)) == [_SWITCH_1, "b1"]
    assert _ids(transcript.get_events_at_offset(0, 20)) == [
        "a1",
        "a2",
        "a3",
        _SWITCH_1,
        "b1",
        "b2",
        _SWITCH_2,
        "c1",
        "c2",
        "c3",
        "c4",
    ]
    assert transcript.get_events_at_offset(11, 3) == []


def test_offsets_and_details_resolve_by_segment_and_a_chip_has_no_detail() -> None:
    transcript, loader = _three_segment_transcript()

    assert transcript.get_event_offset("b2") == 5
    assert loader.loaded == ["agent-second"]
    assert transcript.get_event_offset("a1") == 0
    assert transcript.get_event_detail("a2") == {"tool_input": "a2"}
    assert transcript.get_event_detail("c3") == {"tool_input": "c3"}
    assert transcript.get_event_detail(_SWITCH_1) is None
    assert transcript.get_event_detail("missing") is None
    # A miss loads every segment once and no more.
    assert loader.loaded == ["agent-second", "agent-first"]


def test_a_loaded_segments_parsed_count_wins_over_a_stale_recorded_one() -> None:
    readers: dict[str, TranscriptReader] = {"agent-first": ListTranscriptReader(["a1", "a2"])}
    transcript = ChatTranscript.build(
        _CHAT_ID,
        (_segment("agent-first", 1, 5, HarnessType.CLAUDE), _segment("agent-second", 2, None, HarnessType.CODEX)),
        {"agent-second": ListTranscriptReader(["b1"])},
        _LoadRecorder(readers),
    )
    assert transcript.get_total_event_count() == 7
    # Loading the segment (a backfill past the live one) corrects the total to the parsed length.
    assert _ids(transcript.get_backfill_events("b1", 5)) == ["a1", "a2", _SWITCH_1]
    assert transcript.get_total_event_count() == 4
