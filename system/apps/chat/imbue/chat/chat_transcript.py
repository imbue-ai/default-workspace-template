"""A chat's transcript as the read routes see it: the segments of its agents, in order.

A chat is a sequence of agent transcripts (``docs/system/blueprint/chat-agent-split/``).
The read routes (``/api/chats/<chat_id>/events`` and the event detail) read through this
facade rather than through one agent's watcher, so the routes are written against the
chat's transcript now and the segment concatenation lands behind them later.

Today every chat has exactly one segment, its active agent's, so each read is that
segment's read.
"""

from typing import Any

from pydantic import Field

from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel


class ChatTranscriptError(RuntimeError):
    """A read the transcript cannot answer with the segments it has."""


class TranscriptSegment(FrozenModel):
    """One agent's part of a chat's transcript."""

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}

    agent_id: str = Field(description="The agent whose transcript this segment is")
    reader: TranscriptReader = Field(description="The read side of that agent's transcript")


class ChatTranscript(FrozenModel):
    """The transcript of one chat, read segment by segment."""

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}

    chat_id: ChatId = Field(description="The chat whose transcript this is")
    segments: tuple[TranscriptSegment, ...] = Field(description="The chat's agents' transcripts, in order")

    @classmethod
    def of_single_segment(cls, chat_id: ChatId, agent_id: str, reader: TranscriptReader) -> "ChatTranscript":
        """The transcript of a chat that has run on one agent only: every chat today."""
        return cls(chat_id=chat_id, segments=(TranscriptSegment(agent_id=agent_id, reader=reader),))

    @property
    def _only_segment(self) -> TranscriptReader:
        # CLEANUP: replace with reads across the segments (recorded counts per segment, the
        # cursor and offset reads resolved to the segment they fall in) in phase 3 of the
        # chat-agent split, which is when a chat gains its second segment.
        if len(self.segments) != 1:
            raise ChatTranscriptError("a chat transcript reads across one segment only")
        return self.segments[0].reader

    def get_tail_events(self, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` events of the chat."""
        return self._only_segment.get_tail_events(limit)

    def get_backfill_events(self, before_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately preceding ``before_event_id``."""
        return self._only_segment.get_backfill_events(before_event_id, limit=limit)

    def get_forward_events(self, after_event_id: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events immediately following ``after_event_id``."""
        return self._only_segment.get_forward_events(after_event_id, limit=limit)

    def get_events_at_offset(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """``limit`` events starting at ``offset`` from the chat's beginning."""
        return self._only_segment.get_events_at_offset(offset, limit)

    def get_event_offset(self, event_id: str) -> int:
        """The index of ``event_id`` in the whole chat, or -1 when it is not present."""
        return self._only_segment.get_event_offset(event_id)

    def get_total_event_count(self) -> int:
        """How many events the whole chat holds."""
        return self._only_segment.get_total_event_count()

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        """The full deferred payloads for one event, from the segment that holds it."""
        return self._only_segment.get_event_detail(event_id)
