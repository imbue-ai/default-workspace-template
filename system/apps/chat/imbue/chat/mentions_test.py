from pathlib import Path

from workspace_layout.primitives import ParticipantKey

from imbue.chat.mentions import MentionStore
from imbue.chat.mentions import PendingMention
from imbue.chat.mentions import mentions_filename
from imbue.chat.primitives import ChatId

_BOB = ParticipantKey("user:user-bob-4471")
_JOSH = ParticipantKey("user:user-josh-0001")
_LOUNGE = ChatId("room-0123456789abcdef")
_MAIN = ChatId("agent-00000000000000000000000000000001")


def test_mentions_are_appended_per_person_and_read_back_oldest_first(tmp_path: Path) -> None:
    store = MentionStore(root=tmp_path)

    store.record(_BOB, _LOUNGE, "m-1", _JOSH)
    store.record(_BOB, _MAIN, "m-2", _JOSH)
    store.record(_JOSH, _LOUNGE, "m-3", _BOB)

    assert [mention.model_dump() for mention in store.pending(_BOB)] == [
        {"chat_id": _LOUNGE, "message_id": "m-1", "sender": _JOSH},
        {"chat_id": _MAIN, "message_id": "m-2", "sender": _JOSH},
    ]
    assert store.pending(_JOSH) == (PendingMention(chat_id=_LOUNGE, message_id="m-3", sender=_BOB),)
    assert store.pending(ParticipantKey("owner")) == ()


def test_looking_at_a_chat_clears_only_that_chats_mentions(tmp_path: Path) -> None:
    store = MentionStore(root=tmp_path)
    store.record(_BOB, _LOUNGE, "m-1", _JOSH)
    store.record(_BOB, _MAIN, "m-2", _JOSH)

    assert store.clear(_BOB, _LOUNGE) is True
    assert [mention.chat_id for mention in store.pending(_BOB)] == [_MAIN]
    assert store.clear(_BOB, _LOUNGE) is False
    assert store.clear(_BOB, _MAIN) is True
    assert store.pending(_BOB) == () and not (tmp_path / mentions_filename(_BOB)).exists()


def test_the_file_is_named_after_the_key_with_no_path_separator() -> None:
    assert mentions_filename(ParticipantKey("email:a/b@example.com")) == "email:a_b@example.com.jsonl"
