"""Unit tests for the quick-change events a chat's feed carries (``chat_quick_change_feed.py``)."""

from typing import Any

from imbue.chat.chat_quick_change_feed import FORWARD_FAILED_TEXT
from imbue.chat.chat_quick_change_feed import QuickChangeOverlayReader
from imbue.chat.chat_quick_change_feed import merge_by_timestamp
from imbue.chat.chat_quick_change_feed import overlay_events_for_chat
from imbue.chat.harnesses.session_watcher import TranscriptReader

_CHAT = "agent-1"
_AGENT = "agent-1"


def _request(request_id: str, at: str, chat_id: str = _CHAT) -> dict[str, Any]:
    return {"type": "request", "request_id": request_id, "chat_id": chat_id, "requested_at": at, "message": "Blue"}


def _event(event_id: str, at: str) -> dict[str, Any]:
    return {"type": "user_message", "event_id": event_id, "timestamp": at}


class _ListReader(TranscriptReader):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._events[-limit:]

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._events[offset : offset + limit]

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None


def test_a_request_becomes_the_users_message() -> None:
    [event] = overlay_events_for_chat([_request("r1", "2026-01-01T00:00:05+00:00")], _CHAT, _AGENT)
    assert (event["type"], event["content"], event["agent_id"]) == ("user_message", "Blue", _AGENT)


def test_a_report_becomes_the_reply() -> None:
    records = [
        _request("r1", "2026-01-01T00:00:05+00:00"),
        {"type": "report", "request_id": "r1", "report": "Done.", "reported_at": "2026-01-01T00:00:09+00:00"},
    ]
    events = overlay_events_for_chat(records, _CHAT, _AGENT)
    assert [(event["type"], event.get("text")) for event in events] == [
        ("user_message", None),
        ("assistant_message", "Done."),
    ]


def test_a_failed_handover_becomes_a_failure_reply() -> None:
    records = [
        _request("r1", "2026-01-01T00:00:05+00:00"),
        {"type": "forward", "request_id": "r1", "status": "forward_failed", "at": "2026-01-01T00:00:06+00:00"},
    ]
    assert overlay_events_for_chat(records, _CHAT, _AGENT)[-1]["text"] == FORWARD_FAILED_TEXT


def test_another_chats_requests_are_left_out() -> None:
    assert overlay_events_for_chat([_request("r1", "2026-01-01T00:00:05+00:00", "agent-2")], _CHAT, _AGENT) == []


def test_overlay_events_land_between_the_events_around_their_time() -> None:
    base = [_event("a", "2026-01-01T00:00:00Z"), _event("b", "2026-01-01T00:00:10Z")]
    overlay = [_event("q", "2026-01-01T00:00:05+00:00")]
    assert [event["event_id"] for event in merge_by_timestamp(base, overlay)] == ["a", "q", "b"]


def test_overlay_events_later_than_every_event_go_last() -> None:
    base = [_event("a", "2026-01-01T00:00:00Z")]
    overlay = [_event("q", "2026-01-01T00:01:00+00:00")]
    assert [event["event_id"] for event in merge_by_timestamp(base, overlay)] == ["a", "q"]


def test_the_overlay_reader_counts_and_pages_the_merged_feed() -> None:
    reader = QuickChangeOverlayReader.build(
        _ListReader([_event("a", "2026-01-01T00:00:00Z"), _event("b", "2026-01-01T00:00:10Z")]),
        [_event("q", "2026-01-01T00:00:05+00:00")],
    )
    assert reader.get_total_event_count() == 3
    assert [event["event_id"] for event in reader.get_backfill_events("b", 5)] == ["a", "q"]
    assert reader.get_event_offset("q") == 1
