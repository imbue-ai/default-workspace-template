"""The chat record store: round trips, the version guard, and the per-record skip on a bad file."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import ChatRecordError
from imbue.chat.chat_records import FileChatRecordStore
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.chat_records import RECORD_VERSION
from imbue.chat.primitives import ChatId
from imbue.chat.testing import make_chat_agent_entry
from imbue.chat.testing import make_two_member_chat_record as two_member_record


def _agent_id() -> str:
    return f"agent-{uuid4().hex}"


def test_the_active_entry_is_the_last_one_unless_it_has_ended() -> None:
    first, second = _agent_id(), _agent_id()
    record = two_member_record(first, second)
    assert record.active_entry is not None and record.active_entry.agent_id == second
    assert record.member_agent_ids == (first, second)
    assert [entry.agent_id for entry in record.archived_entries] == [first]
    assert record.entry_for(first) is not None and record.entry_for("agent-nobody") is None

    converging = ChatRecord(chat_id=ChatId(first), agents=(make_chat_agent_entry(1, first, is_archived=True),))
    assert converging.active_entry is None


def test_a_file_store_round_trips_a_record_and_deletes_its_folder(tmp_path: Path) -> None:
    store = FileChatRecordStore(root=tmp_path / "chats")
    first, second = _agent_id(), _agent_id()
    record = two_member_record(first, second)

    assert store.read(ChatId(first)) is None
    assert store.read_all() == {}
    store.write(record)

    assert store.read(ChatId(first)) == record
    assert store.read_all() == {ChatId(first): record}
    written = json.loads((tmp_path / "chats" / first / "record.json").read_text())
    assert written["version"] == RECORD_VERSION
    assert [entry["agent_id"] for entry in written["agents"]] == [first, second]

    store.delete(ChatId(first))
    assert not (tmp_path / "chats" / first).exists()
    assert store.read(ChatId(first)) is None
    # Deleting a chat that has no record is a no-op.
    store.delete(ChatId(second))


def test_a_file_store_refuses_a_record_from_a_newer_build(tmp_path: Path) -> None:
    store = FileChatRecordStore(root=tmp_path / "chats")
    first = _agent_id()
    store.write(two_member_record(first, _agent_id()))
    record_path = tmp_path / "chats" / first / "record.json"
    newer = json.loads(record_path.read_text())
    newer["version"] = RECORD_VERSION + 1
    record_path.write_text(json.dumps(newer))

    with pytest.raises(ChatRecordError, match="version"):
        store.read(ChatId(first))
    # Reading everything skips the bad record rather than failing the whole read.
    assert store.read_all() == {}


def test_a_file_store_skips_a_corrupt_or_misfiled_record_and_keeps_the_rest(tmp_path: Path) -> None:
    store = FileChatRecordStore(root=tmp_path / "chats")
    good_first, good_second = _agent_id(), _agent_id()
    good = two_member_record(good_first, good_second)
    store.write(good)

    corrupt_dir = tmp_path / "chats" / _agent_id()
    corrupt_dir.mkdir()
    (corrupt_dir / "record.json").write_text("{not json")

    misfiled_first = _agent_id()
    misfiled_dir = tmp_path / "chats" / _agent_id()
    misfiled_dir.mkdir()
    (misfiled_dir / "record.json").write_text(
        json.dumps(two_member_record(misfiled_first, _agent_id()).model_dump(mode="json"))
    )

    assert store.read_all() == {ChatId(good_first): good}
    with pytest.raises(ChatRecordError, match="not valid JSON"):
        store.read(ChatId(corrupt_dir.name))


def test_an_in_memory_store_behaves_like_the_file_store() -> None:
    store = InMemoryChatRecordStore()
    first = _agent_id()
    record = two_member_record(first, _agent_id())
    store.write(record)
    assert store.read(ChatId(first)) == record
    assert store.read_all() == {ChatId(first): record}
    store.delete(ChatId(first))
    assert store.read_all() == {}
