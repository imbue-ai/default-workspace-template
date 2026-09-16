"""The chat record store: round trips, the version guard, and the per-record skip on a bad file."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from imbue.chat.chat_records import ChatRecord
from imbue.chat.chat_records import ChatRecordError
from imbue.chat.chat_records import FileChatRecordStore
from imbue.chat.chat_records import InMemoryChatRecordStore
from imbue.chat.chat_records import RECORD_VERSION
from imbue.chat.primitives import ChatId
from imbue.chat.testing import make_chat_agent_entry
from imbue.chat.testing import make_chat_handoff_record
from imbue.chat.testing import make_chat_rebind_record
from imbue.chat.testing import make_two_member_chat_record as two_member_record
from imbue.imbue_common.model_update import to_update


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


def test_a_record_refuses_agents_that_contradict_what_it_says_about_them() -> None:
    """The manager keys a chat on its first agent, the transcript derives its switch markers from
    ``seq``, and ``active_entry`` reads only the last entry, so a record breaking any of those is
    refused at the parse boundary rather than read wrong."""
    first, second = _agent_id(), _agent_id()
    archived_first = make_chat_agent_entry(1, first, is_archived=True)
    live_second = make_chat_agent_entry(2, second, is_archived=False)

    with pytest.raises(ValidationError, match="first agent"):
        ChatRecord(chat_id=ChatId(second), agents=(archived_first, live_second))
    with pytest.raises(ValidationError, match="carries seq 3"):
        ChatRecord(chat_id=ChatId(first), agents=(archived_first, make_chat_agent_entry(3, second, is_archived=False)))
    with pytest.raises(ValidationError, match="twice"):
        ChatRecord(chat_id=ChatId(first), agents=(archived_first, make_chat_agent_entry(2, first, is_archived=False)))
    with pytest.raises(ValidationError, match="no ended_at"):
        ChatRecord(chat_id=ChatId(first), agents=(make_chat_agent_entry(1, first, is_archived=False), live_second))
    # A converging chat (every agent archived, none active yet) and a single agent are both fine.
    ChatRecord(chat_id=ChatId(first), agents=(archived_first, make_chat_agent_entry(2, second, is_archived=True)))
    ChatRecord(chat_id=ChatId(first), agents=(make_chat_agent_entry(1, first, is_archived=False),))


def test_a_records_handoff_must_retire_its_last_agent_and_name_a_new_successor() -> None:
    first, second, third = _agent_id(), _agent_id(), _agent_id()
    archived_first = make_chat_agent_entry(1, first, is_archived=True)
    live_second = make_chat_agent_entry(2, second, is_archived=False)
    handoff = make_chat_handoff_record(retiring_seq=2, next_agent_id=third)

    ChatRecord(chat_id=ChatId(first), agents=(archived_first, live_second), handoff=handoff)
    with pytest.raises(ValidationError, match="retires seq 1"):
        ChatRecord(
            chat_id=ChatId(first),
            agents=(archived_first, live_second),
            handoff=handoff.model_copy_update(to_update(handoff.field_ref().retiring_seq, 1)),
        )
    with pytest.raises(ValidationError, match="not the next"):
        ChatRecord(
            chat_id=ChatId(first),
            agents=(archived_first, live_second),
            handoff=handoff.model_copy_update(to_update(handoff.field_ref().next_seq, 4)),
        )
    with pytest.raises(ValidationError, match="already a member"):
        ChatRecord(
            chat_id=ChatId(first),
            agents=(archived_first, live_second),
            handoff=handoff.model_copy_update(to_update(handoff.field_ref().next_agent_id, first)),
        )
    assert handoff.held_send_for(handoff.trigger_message_id) is not None
    assert handoff.held_send_for("no-such-message") is None


def test_a_records_rebind_names_its_running_agent_and_never_sits_beside_a_handoff() -> None:
    first, second = _agent_id(), _agent_id()
    archived_first = make_chat_agent_entry(1, first, is_archived=True)
    live_second = make_chat_agent_entry(2, second, is_archived=False)
    rebind = make_chat_rebind_record(agent_id=second)

    record = ChatRecord(chat_id=ChatId(first), agents=(archived_first, live_second), rebind=rebind)
    assert record.converging is rebind and record.converging.transition_id == "rebind-1"
    assert record.with_converging(None).converging is None
    handoff = make_chat_handoff_record(retiring_seq=2, next_agent_id=_agent_id())
    swapped = record.with_converging(handoff)
    assert swapped.handoff is handoff and swapped.rebind is None and swapped.converging is handoff
    assert swapped.with_converging(rebind).handoff is None

    with pytest.raises(ValidationError, match="active agent"):
        ChatRecord(
            chat_id=ChatId(first), agents=(archived_first, live_second), rebind=make_chat_rebind_record(agent_id=first)
        )
    with pytest.raises(ValidationError, match="active agent"):
        ChatRecord(
            chat_id=ChatId(first),
            agents=(archived_first, make_chat_agent_entry(2, second, is_archived=True)),
            rebind=rebind,
        )
    with pytest.raises(ValidationError, match="both a handoff and a rebind"):
        ChatRecord(chat_id=ChatId(first), agents=(archived_first, live_second), handoff=handoff, rebind=rebind)


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes a folder whatever its parent's mode says")
def test_a_file_store_raises_when_a_record_cannot_be_removed(tmp_path: Path) -> None:
    """A delete that leaves the record on disk must not look like a delete: the next build would
    read the record back and resurrect a destroyed chat."""
    root = tmp_path / "chats"
    store = FileChatRecordStore(root=root)
    first = _agent_id()
    store.write(two_member_record(first, _agent_id()))

    # Nothing inside a read-only folder can be unlinked, so the record file stays where it is.
    chat_dir = root / first
    chat_dir.chmod(0o555)
    try:
        with pytest.raises(ChatRecordError, match="could not be removed"):
            store.delete(ChatId(first))
    finally:
        chat_dir.chmod(0o755)
    assert store.read(ChatId(first)) is not None


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

    # A record that contradicts itself (here, a chat named after an agent it does not start with)
    # is skipped the same way, and read one at a time it names the problem.
    inconsistent_first = _agent_id()
    inconsistent_dir = tmp_path / "chats" / inconsistent_first
    inconsistent_dir.mkdir()
    inconsistent = two_member_record(_agent_id(), _agent_id()).model_dump(mode="json")
    inconsistent["chat_id"] = inconsistent_first
    (inconsistent_dir / "record.json").write_text(json.dumps(inconsistent))

    assert store.read_all() == {ChatId(good_first): good}
    with pytest.raises(ChatRecordError, match="not valid JSON"):
        store.read(ChatId(corrupt_dir.name))
    with pytest.raises(ChatRecordError, match="first agent"):
        store.read(ChatId(inconsistent_first))


def test_an_in_memory_store_behaves_like_the_file_store() -> None:
    store = InMemoryChatRecordStore()
    first = _agent_id()
    record = two_member_record(first, _agent_id())
    store.write(record)
    assert store.read(ChatId(first)) == record
    assert store.read_all() == {ChatId(first): record}
    store.delete(ChatId(first))
    assert store.read_all() == {}
