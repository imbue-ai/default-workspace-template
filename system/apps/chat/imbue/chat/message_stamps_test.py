from pathlib import Path

from imbue.chat.message_stamps import MessageStampStore
from imbue.chat.primitives import ChatId


def test_stamps_persist_across_stores_and_are_forgotten_per_agent(tmp_path: Path) -> None:
    path = tmp_path / "chat" / "last_messaged.json"
    store = MessageStampStore(path=path)
    store.record(ChatId("agent-1"), at=100.0)
    store.record(ChatId("agent-2"), at=200.0)
    store.record(ChatId("agent-1"), at=300.0)
    assert MessageStampStore(path=path).read() == {"agent-1": 300.0, "agent-2": 200.0}
    store.forget(ChatId("agent-2"))
    assert MessageStampStore(path=path).read() == {"agent-1": 300.0}


def test_a_memory_only_store_and_an_unreadable_file_start_empty(tmp_path: Path) -> None:
    memory_only = MessageStampStore(path=None)
    memory_only.record(ChatId("agent-1"))
    assert set(memory_only.read()) == {"agent-1"}

    path = tmp_path / "last_messaged.json"
    path.write_text("{not json")
    assert MessageStampStore(path=path).read() == {}


def test_stores_under_different_keys_read_only_their_own_stamps(tmp_path: Path) -> None:
    path = tmp_path / "stamps.json"
    MessageStampStore(path=path, key="replies").record(ChatId("agent-1"), at=100.0)
    assert MessageStampStore(path=path, key="replies").read() == {"agent-1": 100.0}
    assert MessageStampStore(path=path).read() == {}
