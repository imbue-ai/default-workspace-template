"""A chat's transcript as the read routes see it: the segments of its agents, in order.

A chat is a sequence of agent transcripts (``docs/system/blueprint/chat-agent-split/`` 4.7).
The read routes (``/api/chats/<chat_id>/events`` and the event detail) read through this
facade rather than through one agent's watcher. Between two segments sits one synthesized
``agent_switch`` event, the chip that says which agent the chat moved to.

Offsets and totals are chat-global. An archived segment's length is the count recorded on the
chat record when its agent was archived, so the total and any offset are known without loading
anything; a segment's body loads on the first read that reaches into it (a backfill past the
live segment's start, a jump to an offset inside it, a cursor or detail fetch for an event the
live segment does not hold), through the loader the caller supplies, which caches it for the
chat's life. Once a segment is loaded its parsed count wins over the recorded one, so the two
never disagree within one transcript.
"""

from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# The chat-level event between two segments: not a harness ``special`` kind, since no harness
# emits it. Its id is derived (never random) from the chat and the retiring agent's sequence
# number, the event-id rule of ``harnesses/events``, so it is stable across reloads.
AGENT_SWITCH_EVENT_TYPE: Final[str] = "agent_switch"
AGENT_SWITCH_SOURCE: Final[str] = "chat"


class ChatTranscriptError(RuntimeError):
    """A read the transcript cannot answer with the segments it has."""


class TranscriptSegment(FrozenModel):
    """One agent's part of a chat's transcript."""

    agent_id: str = Field(description="The agent whose transcript this segment is")
    harness: HarnessType = Field(description="The harness that agent runs")
    seq: int = Field(ge=1, description="The agent's 1-based position in the chat")
    recorded_event_count: int | None = Field(
        description="The segment's length as recorded when its agent was archived; None for the live segment"
    )
    ended_at: datetime | None = Field(description="When its agent was archived; None for the live segment")


class _EventPosition(FrozenModel):
    """Where an event id sits in the chat: inside a segment, or as the switch marker after one."""

    segment_index: int = Field(ge=0, description="The segment holding the event, or the one the marker follows")
    local_offset: int | None = Field(
        description="The event's index within its segment; None for the switch marker after the segment"
    )


@pure
def agent_switch_event_id(chat_id: ChatId, retiring_seq: int) -> str:
    return f"{chat_id}:switch:{retiring_seq}"


@pure
def agent_switch_event(chat_id: ChatId, retiring: TranscriptSegment, successor: TranscriptSegment) -> dict[str, Any]:
    """The chip between two segments: the chat moved from one agent to the next."""
    return {
        "type": AGENT_SWITCH_EVENT_TYPE,
        "event_id": agent_switch_event_id(chat_id, retiring.seq),
        "timestamp": retiring.ended_at.isoformat() if retiring.ended_at is not None else "",
        "source": AGENT_SWITCH_SOURCE,
        "agent_id": successor.agent_id,
        "from_agent_id": retiring.agent_id,
        "to_agent_id": successor.agent_id,
        "from_harness": retiring.harness.value,
        "to_harness": successor.harness.value,
        "seq": retiring.seq,
    }


class ChatTranscript:
    """The transcript of one chat, read segment by segment with the switch chips between them.

    Built per request over the segments the manager names; the readers it loads are the
    caller's to cache (``load_segment`` is expected to return the same reader for the same
    segment on a later request), so this holds nothing across requests.
    """

    _chat_id: ChatId
    _segments: tuple[TranscriptSegment, ...]
    _reader_by_index: dict[int, TranscriptReader]
    _load_segment: Callable[[TranscriptSegment], TranscriptReader]

    @classmethod
    def build(
        cls,
        chat_id: ChatId,
        segments: tuple[TranscriptSegment, ...],
        # The readers already resident, by agent id: the live segment's watcher, and any
        # archived segment an earlier read loaded.
        reader_by_agent_id: Mapping[str, TranscriptReader],
        load_segment: Callable[[TranscriptSegment], TranscriptReader],
    ) -> "ChatTranscript":
        if not segments:
            raise ChatTranscriptError("a chat transcript needs at least one segment")
        transcript = cls.__new__(cls)
        transcript._chat_id = chat_id
        transcript._segments = segments
        transcript._reader_by_index = {
            index: reader_by_agent_id[segment.agent_id]
            for index, segment in enumerate(segments)
            if segment.agent_id in reader_by_agent_id
        }
        transcript._load_segment = load_segment
        return transcript

    @property
    def segments(self) -> tuple[TranscriptSegment, ...]:
        return self._segments

    # Segments and their lengths.

    def _reader(self, index: int) -> TranscriptReader:
        """The segment's reader, loading it on first use."""
        existing = self._reader_by_index.get(index)
        if existing is not None:
            return existing
        segment = self._segments[index]
        reader = self._load_segment(segment)
        self._reader_by_index[index] = reader
        if segment.recorded_event_count is not None:
            parsed = reader.get_total_event_count()
            if parsed != segment.recorded_event_count:
                logger.warning(
                    "Chat {}: agent {}'s segment holds {} events but its record says {}; the parsed count wins",
                    self._chat_id,
                    segment.agent_id,
                    parsed,
                    segment.recorded_event_count,
                )
        self._warn_on_repeated_event_ids(index, reader)
        return reader

    def _warn_on_repeated_event_ids(self, index: int, reader: TranscriptReader) -> None:
        """Event ids are stable per harness but only unique per agent; a repeat across segments
        would make a cursor ambiguous, so it is worth a line in the log (the earlier segment's
        event wins in every lookup)."""
        own_ids = {event["event_id"] for event in reader.get_all_events()}
        for other_index, other in self._reader_by_index.items():
            if other_index == index:
                continue
            repeated = own_ids.intersection(event["event_id"] for event in other.get_all_events())
            if repeated:
                logger.warning(
                    "Chat {}: agents {} and {} share {} event id(s), e.g. {}",
                    self._chat_id,
                    self._segments[min(index, other_index)].agent_id,
                    self._segments[max(index, other_index)].agent_id,
                    len(repeated),
                    sorted(repeated)[0],
                )

    def _count(self, index: int) -> int:
        """The segment's length: parsed once loaded, else as recorded, else loaded to find out."""
        reader = self._reader_by_index.get(index)
        if reader is not None:
            return reader.get_total_event_count()
        recorded = self._segments[index].recorded_event_count
        if recorded is not None:
            return recorded
        return self._reader(index).get_total_event_count()

    def _segment_start(self, index: int) -> int:
        """The chat-global offset of the segment's first event: every earlier segment plus the marker after each."""
        return sum(self._count(earlier) + 1 for earlier in range(index))

    def _marker(self, index: int) -> dict[str, Any]:
        return agent_switch_event(self._chat_id, self._segments[index], self._segments[index + 1])

    def _locate(self, event_id: str) -> _EventPosition | None:
        """Where an event id sits, or None when no segment holds it.

        The markers first (they need no loading), then the loaded segments oldest first (so
        a repeated id resolves to the earlier segment), then the unloaded ones newest first
        (a cursor is most often near the tail, and each miss costs a load).
        """
        for index in range(len(self._segments) - 1):
            if event_id == agent_switch_event_id(self._chat_id, self._segments[index].seq):
                return _EventPosition(segment_index=index, local_offset=None)
        for index in sorted(self._reader_by_index):
            local_offset = self._reader_by_index[index].get_event_offset(event_id)
            if local_offset >= 0:
                return _EventPosition(segment_index=index, local_offset=local_offset)
        for index in reversed(range(len(self._segments))):
            if index in self._reader_by_index:
                continue
            local_offset = self._reader(index).get_event_offset(event_id)
            if local_offset >= 0:
                return _EventPosition(segment_index=index, local_offset=local_offset)
        return None

    # The read API.

    def get_tail_events(self, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` events of the chat, crossing into earlier segments when the live one is short."""
        collected: list[dict[str, Any]] = []
        remaining = limit
        for index in reversed(range(len(self._segments))):
            if remaining <= 0:
                break
            events = self._reader(index).get_tail_events(remaining)
            collected = events + collected
            remaining -= len(events)
            if remaining > 0 and index > 0:
                collected = [self._marker(index - 1)] + collected
                remaining -= 1
        return collected

    def get_backfill_events(self, before_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately preceding ``before_event_id``, crossing segment boundaries."""
        position = self._locate(before_event_id)
        if position is None:
            return []
        collected: list[dict[str, Any]] = []
        remaining = limit
        if position.local_offset is None:
            # The cursor is the marker after the segment: everything before it is that
            # segment's tail onward.
            index = position.segment_index
        else:
            events = self._reader(position.segment_index).get_backfill_events(before_event_id, remaining)
            collected = events
            remaining -= len(events)
            if remaining > 0 and position.segment_index > 0:
                collected = [self._marker(position.segment_index - 1)] + collected
                remaining -= 1
            index = position.segment_index - 1
        while remaining > 0 and index >= 0:
            events = self._reader(index).get_tail_events(remaining)
            collected = events + collected
            remaining -= len(events)
            if remaining > 0 and index > 0:
                collected = [self._marker(index - 1)] + collected
                remaining -= 1
            index -= 1
        return collected

    def get_forward_events(self, after_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately following ``after_event_id``, crossing segment boundaries."""
        position = self._locate(after_event_id)
        if position is None:
            return []
        collected: list[dict[str, Any]] = []
        remaining = limit
        if position.local_offset is None:
            # The cursor is the marker after the segment: what follows is the next segment.
            index = position.segment_index + 1
        else:
            events = self._reader(position.segment_index).get_forward_events(after_event_id, remaining)
            collected.extend(events)
            remaining -= len(events)
            if remaining > 0 and position.segment_index + 1 < len(self._segments):
                collected.append(self._marker(position.segment_index))
                remaining -= 1
            index = position.segment_index + 1
        while remaining > 0 and index < len(self._segments):
            events = self._reader(index).get_events_at_offset(0, remaining)
            collected.extend(events)
            remaining -= len(events)
            if remaining > 0 and index + 1 < len(self._segments):
                collected.append(self._marker(index))
                remaining -= 1
            index += 1
        return collected

    def get_events_at_offset(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """``limit`` events starting at ``offset`` from the chat's beginning, crossing segment boundaries."""
        collected: list[dict[str, Any]] = []
        remaining = limit
        position = max(0, offset)
        for index in range(len(self._segments)):
            if remaining <= 0:
                break
            count = self._count(index)
            if position < count:
                events = self._reader(index).get_events_at_offset(position, remaining)
                collected.extend(events)
                remaining -= len(events)
                position = 0
            else:
                position -= count
            if remaining <= 0 or index + 1 >= len(self._segments):
                continue
            # The marker slot between this segment and the next.
            if position == 0:
                collected.append(self._marker(index))
                remaining -= 1
            else:
                position -= 1
        return collected

    def get_event_offset(self, event_id: str) -> int:
        """The index of ``event_id`` in the whole chat, or -1 when it is not present."""
        position = self._locate(event_id)
        if position is None:
            return -1
        if position.local_offset is None:
            return self._segment_start(position.segment_index) + self._count(position.segment_index)
        return self._segment_start(position.segment_index) + position.local_offset

    def get_total_event_count(self) -> int:
        """How many events the whole chat holds, the switch markers included."""
        last = len(self._segments) - 1
        return self._segment_start(last) + self._count(last)

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        """The full deferred payloads for one event, from the segment that holds it; a switch marker has none."""
        position = self._locate(event_id)
        if position is None or position.local_offset is None:
            return None
        return self._reader(position.segment_index).get_event_detail(event_id)
