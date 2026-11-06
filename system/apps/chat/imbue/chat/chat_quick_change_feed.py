"""The routed quick look-and-feel changes as events in the chat they were sent from.

A routed message never reaches the chat's own agent (``chat_quick_change.py``), so the agent's
transcript holds neither the user's words nor an answer. The chat app adds them itself, from the
quick-change log: a ``user_message`` with the user's words when the message is routed, and an
``assistant_message`` with the quick-change agent's one-line report when it lands (or a short
failure line when the request could not be handed over). The page shows them like any other turn,
and a client that watches the chat's feed for its message and a reply sees both.

The events are merged into the live agent's segment by timestamp (``QuickChangeOverlayReader``),
only for a chat that has any; their ids are derived from the request id, so they are stable across
reads.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.session_watcher import TranscriptReader
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# The ``source`` of an event the chat app adds itself, as the switch chip's (``chat_transcript.py``).
QUICK_CHANGE_EVENT_SOURCE: Final[str] = "chat"
FORWARD_FAILED_TEXT: Final[str] = "The quick look-and-feel change could not be started."


def read_quick_change_log(log_path: Path) -> list[dict[str, Any]]:
    """Every record in the quick-change log, oldest first; an unreadable line is skipped."""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Could not read the quick-change log at {}: {}", log_path, e)
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


@pure
def request_event_id(request_id: str) -> str:
    return f"quick-change:{request_id}:request"


@pure
def reply_event_id(request_id: str) -> str:
    return f"quick-change:{request_id}:reply"


@pure
def request_event(request_id: str, message: str, timestamp: str, agent_id: str) -> dict[str, Any]:
    """The user's routed message, as the chat shows it."""
    return {
        "type": "user_message",
        "event_id": request_event_id(request_id),
        "source": QUICK_CHANGE_EVENT_SOURCE,
        "role": "user",
        "content": message,
        "timestamp": timestamp,
        "agent_id": agent_id,
    }


@pure
def reply_event(request_id: str, text: str, timestamp: str, agent_id: str) -> dict[str, Any]:
    """The quick-change agent's report (or the failure to hand the request over), as the chat's reply."""
    return {
        "type": "assistant_message",
        "event_id": reply_event_id(request_id),
        "source": QUICK_CHANGE_EVENT_SOURCE,
        "role": "assistant",
        "model": None,
        "text": text,
        "tool_calls": [],
        "stop_reason": "end_turn",
        "usage": None,
        "timestamp": timestamp,
        "agent_id": agent_id,
    }


@pure
def overlay_events_for_chat(records: Sequence[Mapping[str, Any]], chat_id: str, agent_id: str) -> list[dict[str, Any]]:
    """The events the quick-change log adds to one chat, in log order: each request, then its reply once there is one."""
    request_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    has_reply: set[str] = set()
    for record in records:
        request_id = str(record.get("request_id", ""))
        if record.get("type") == "request":
            if record.get("chat_id") == chat_id and request_id not in request_ids:
                request_ids.add(request_id)
                message = str(record.get("message", ""))
                events.append(request_event(request_id, message, str(record.get("requested_at", "")), agent_id))
            continue
        if request_id not in request_ids or request_id in has_reply:
            continue
        reply = _reply_of(record)
        if reply is None:
            continue
        has_reply.add(request_id)
        events.append(reply_event(request_id, reply[0], reply[1], agent_id))
    return events


@pure
def _reply_of(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """The reply text and time a log record carries: a report's, a failed handover's, or None for any other."""
    if record.get("type") == "report":
        return str(record.get("report", "")), str(record.get("reported_at", ""))
    if record.get("type") == "forward" and record.get("status") == "forward_failed":
        return FORWARD_FAILED_TEXT, str(record.get("at", ""))
    return None


def _epoch(event: Mapping[str, Any]) -> float | None:
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, str) or timestamp == "":
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def merge_by_timestamp(base: Sequence[dict[str, Any]], overlay: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """``base`` in its own order with each overlay event placed before the first base event that is later than it.

    A base event with no readable timestamp keeps its place; an overlay event later than every base event
    goes at the end.
    """
    pending = sorted(overlay, key=lambda event: _epoch(event) or 0.0)
    merged: list[dict[str, Any]] = []
    next_index = 0
    for event in base:
        event_epoch = _epoch(event)
        if event_epoch is not None:
            while next_index < len(pending) and (_epoch(pending[next_index]) or 0.0) < event_epoch:
                merged.append(pending[next_index])
                next_index += 1
        merged.append(event)
    merged.extend(pending[next_index:])
    return merged


class QuickChangeOverlayReader(TranscriptReader):
    """The live agent's transcript with the chat's quick-change events merged in by timestamp."""

    _base: TranscriptReader
    _overlay: list[dict[str, Any]]
    _overlay_ids: frozenset[str]

    @classmethod
    def build(cls, base: TranscriptReader, overlay: Sequence[dict[str, Any]]) -> "QuickChangeOverlayReader":
        reader = cls()
        reader._base = base
        reader._overlay = list(overlay)
        reader._overlay_ids = frozenset(str(event["event_id"]) for event in overlay)
        return reader

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return merge_by_timestamp(self._base.get_all_events(session_id), self._overlay)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        events = self.get_all_events(session_id)
        return events[max(0, len(events) - limit) :]

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        events = self.get_all_events(session_id)
        index = _index_of(events, before_event_id)
        if index < 0:
            return []
        return events[max(0, index - limit) : index]

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        events = self.get_all_events(session_id)
        index = _index_of(events, after_event_id)
        if index < 0:
            return []
        return events[index + 1 : index + 1 + limit]

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        events = self.get_all_events(session_id)
        start = max(0, offset)
        return events[start : start + limit]

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return _index_of(self.get_all_events(session_id), event_id)

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return self._base.get_total_event_count(session_id) + len(self._overlay)

    def get_event_detail(self, event_id: str) -> dict[str, Any] | None:
        if event_id in self._overlay_ids:
            return None
        return self._base.get_event_detail(event_id)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return self._base.get_subagent_metadata(subagent_session_id)


def _index_of(events: Sequence[Mapping[str, Any]], event_id: str) -> int:
    for index, event in enumerate(events):
        if event.get("event_id") == event_id:
            return index
    return -1
