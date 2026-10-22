"""The main chat's choice among the listed chats, and the file that keeps it."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.chat.main_chat import MainChatCandidate
from imbue.chat.main_chat import MainChatStore
from imbue.chat.main_chat import resolved_main_chat_id
from imbue.chat.primitives import ChatId


def _candidate(
    chat_id: str, last_messaged_at: float | None = None, seeded_minute: int | None = None
) -> MainChatCandidate:
    return MainChatCandidate(
        chat_id=ChatId(chat_id),
        seeded_at=None if seeded_minute is None else datetime(2026, 10, 1, 12, seeded_minute, tzinfo=timezone.utc),
        last_messaged_at=last_messaged_at,
    )


def test_the_main_chat_stays_on_its_chat_while_that_chat_is_listed() -> None:
    candidates = [_candidate("agent-welcome", seeded_minute=1), _candidate("agent-busy", last_messaged_at=500.0)]

    assert resolved_main_chat_id(ChatId("agent-busy"), candidates) == ChatId("agent-busy")


def test_with_no_main_chat_the_earliest_welcome_chat_takes_it_over_a_more_recent_chat() -> None:
    candidates = [
        _candidate("agent-busy", last_messaged_at=500.0),
        _candidate("agent-later-seed", seeded_minute=9),
        _candidate("agent-welcome", seeded_minute=1),
    ]

    assert resolved_main_chat_id(None, candidates) == ChatId("agent-welcome")


def test_a_main_chat_pointer_whose_chat_is_gone_moves_to_the_chat_messaged_most_recently() -> None:
    candidates = [
        _candidate("agent-never"),
        _candidate("agent-older", last_messaged_at=100.0),
        _candidate("agent-newer", last_messaged_at=200.0),
    ]

    assert resolved_main_chat_id(ChatId("agent-deleted"), candidates) == ChatId("agent-newer")


def test_with_no_chat_there_is_no_main_chat() -> None:
    assert resolved_main_chat_id(ChatId("agent-deleted"), []) is None


def test_the_store_keeps_the_main_chat_across_a_reload_and_forgets_it_when_cleared(tmp_path: Path) -> None:
    path = tmp_path / "main_chat.json"
    MainChatStore(path=path).write(ChatId("agent-welcome"))
    assert MainChatStore(path=path).read() == ChatId("agent-welcome")

    MainChatStore(path=path).write(None)
    assert MainChatStore(path=path).read() is None


def test_the_store_ignores_a_file_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "main_chat.json"
    path.write_text("not json", encoding="utf-8")
    assert MainChatStore(path=path).read() is None

    path.write_text('{"chat_id": "not an id"}', encoding="utf-8")
    assert MainChatStore(path=path).read() is None
