from typing import Any

from imbue.chat.harnesses.session_watcher import CapturePaneCallback
from imbue.chat.harnesses.session_watcher import ReplyDraft
from imbue.chat.reply_drafts import REPLY_DRAFT_EVENT_TYPE
from imbue.chat.reply_drafts import ReplyDraftPublisher
from imbue.chat.reply_drafts import ReplyDraftReader


class _ScriptedDraftReader:
    """Answers each read with the next draft of its script, capturing the pane first."""

    def __init__(self, drafts: list[ReplyDraft | None]) -> None:
        self._drafts = drafts
        self.captured: list[str | None] = []

    def read_reply_draft(self, capture_pane: CapturePaneCallback) -> ReplyDraft | None:
        self.captured.append(capture_pane())
        return self._drafts.pop(0)


def _publisher(
    watched: list[tuple[str, str, ReplyDraftReader]], sent: list[tuple[str, dict[str, Any]]]
) -> ReplyDraftPublisher:
    return ReplyDraftPublisher(
        list_watched_agents=lambda: list(watched),
        capture_agent_pane=lambda agent_id: f"pane of {agent_id}",
        broadcast_to_chat=lambda chat_id, frame: sent.append((chat_id, frame)),
    )


def test_a_changed_draft_is_sent_once_on_its_chats_stream_and_an_unchanged_one_not_again() -> None:
    first = ReplyDraft(after_event_id="event-1", text="Tides are")
    reader = _ScriptedDraftReader([first, first, ReplyDraft(after_event_id="event-1", text="Tides are the rise")])
    sent: list[tuple[str, dict[str, Any]]] = []
    publisher = _publisher([("chat-7", "agent-9", reader)], sent)

    for _ in range(3):
        publisher.publish_once()

    assert sent == [
        ("chat-7", {"type": REPLY_DRAFT_EVENT_TYPE, "agent_id": "agent-9", "after_event_id": "event-1", "text": "Tides are"}),
        (
            "chat-7",
            {"type": REPLY_DRAFT_EVENT_TYPE, "agent_id": "agent-9", "after_event_id": "event-1", "text": "Tides are the rise"},
        ),
    ]
    assert reader.captured == ["pane of agent-9"] * 3


def test_a_cleared_draft_is_sent_once_and_no_draft_from_the_start_sends_nothing() -> None:
    reader = _ScriptedDraftReader(
        [
            ReplyDraft(after_event_id="event-1", text=""),
            ReplyDraft(after_event_id="event-1", text="Hi"),
            ReplyDraft(after_event_id="event-1", text=""),
            # The turn ended: no draft, and no event to follow either.
            ReplyDraft(after_event_id="", text=""),
        ]
    )
    sent: list[tuple[str, dict[str, Any]]] = []
    publisher = _publisher([("chat-7", "agent-9", reader)], sent)

    for _ in range(4):
        publisher.publish_once()

    assert [frame["text"] for _, frame in sent] == ["Hi", ""]


def test_a_harness_without_drafts_sends_nothing() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    publisher = _publisher([("chat-7", "agent-9", _ScriptedDraftReader([None]))], sent)

    publisher.publish_once()

    assert sent == []


def test_a_chat_looked_away_from_starts_from_nothing_when_looked_at_again() -> None:
    draft = ReplyDraft(after_event_id="event-1", text="Same draft")
    reader = _ScriptedDraftReader([draft, draft])
    watched: list[tuple[str, str, ReplyDraftReader]] = [("chat-7", "agent-9", reader)]
    sent: list[tuple[str, dict[str, Any]]] = []
    publisher = _publisher(watched, sent)

    publisher.publish_once()
    watched.clear()
    publisher.publish_once()
    watched.append(("chat-7", "agent-9", reader))
    publisher.publish_once()

    # Looking away clears the draft on the page; looking back sends it again, though it did not change.
    assert [frame["text"] for _, frame in sent] == ["Same draft", "", "Same draft"]


def test_looking_away_from_a_chat_with_no_draft_sends_nothing() -> None:
    reader = _ScriptedDraftReader([ReplyDraft(after_event_id="event-1", text="")])
    watched: list[tuple[str, str, ReplyDraftReader]] = [("chat-7", "agent-9", reader)]
    sent: list[tuple[str, dict[str, Any]]] = []
    publisher = _publisher(watched, sent)

    publisher.publish_once()
    watched.clear()
    publisher.publish_once()

    assert sent == []
