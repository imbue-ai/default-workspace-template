import json
from pathlib import Path

from workspace_layout.primitives import ParticipantKey

from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ROOM_ID_PATTERN
from imbue.chat.primitives import is_room_id
from imbue.chat.rooms import LOUNGE_TITLE
from imbue.chat.rooms import ROOM_MESSAGES_FILENAME
from imbue.chat.rooms import ROOM_RECORD_FILENAME
from imbue.chat.rooms import RoomStore
from imbue.chat.rooms import new_room_message
from imbue.chat.rooms import room_message_as_page_event

_OWNER = ParticipantKey("owner")
_BOB = ParticipantKey("user:user-bob-4471")


def test_a_room_is_minted_with_a_room_shaped_id_and_the_first_free_title(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)

    first = store.create("", _OWNER)
    second = store.create("  ", _OWNER)
    named = store.create("Design", _BOB)

    assert ROOM_ID_PATTERN.fullmatch(str(first.id)) and is_room_id(str(first.id))
    assert (first.title, second.title, named.title) == ("Room 1", "Room 2", "Design")
    assert named.created_by == _BOB
    assert [record.id for record in store.read_all()] == [first.id, second.id, named.id]
    assert store.is_room(named.id) and not store.is_room(ChatId("agent-00000000000000000000000000000001"))
    assert json.loads((tmp_path / str(named.id) / ROOM_RECORD_FILENAME).read_text())["version"] == 1


def test_a_converted_awaiting_chat_keeps_its_agent_shaped_id(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)
    chat_id = ChatId("agent-00000000000000000000000000000007")

    record = store.create("Chat 3", _OWNER, room_id=chat_id)

    assert record.id == chat_id and store.is_room(chat_id)
    assert store.snapshot(record).model_dump(mode="json") == {
        "id": chat_id,
        "kind": "room",
        "title": "Chat 3",
        "last_messaged_at": None,
        "awaiting_chat_ids": [],
    }


def test_messages_are_appended_read_back_and_windowed_after_an_event(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)
    room = store.create("Lounge", _OWNER)
    first = new_room_message("m-1", _OWNER, "Owner", "hello @Bob", ["user:user-bob-4471"])
    second = new_room_message("m-2", _BOB, "Bob", "hi")
    store.append(room.id, first)
    store.append(room.id, second)

    assert store.messages(room.id) == (first, second)
    assert store.messages(room.id, after=str(first.event_id)) == (second,)
    assert store.messages(room.id, after="no-such-event") == (first, second)
    assert store.last_messaged_at(room.id) is not None
    # An unreadable line is skipped, and the next append is seen past the cache.
    path = tmp_path / str(room.id) / ROOM_MESSAGES_FILENAME
    path.write_text(path.read_text() + "not json\n")
    store.append(room.id, new_room_message("m-3", _BOB, "Bob", "again"))
    assert [message.message_id for message in store.messages(room.id)] == ["m-1", "m-2", "m-3"]


def test_a_message_reads_on_the_page_as_its_authors_user_turn() -> None:
    message = new_room_message("m-1", _BOB, "Bob", "ping @agent", ["workspace-agent"])

    event = room_message_as_page_event(message)

    assert event["type"] == "user_message" and event["role"] == "user" and event["source"] == "room"
    assert (event["content"], event["author"], event["author_name"]) == ("ping @agent", "user:user-bob-4471", "Bob")
    assert (event["event_id"], event["message_id"], event["mentions"]) == (
        str(message.event_id),
        "m-1",
        ["workspace-agent"],
    )


def test_rename_delete_and_the_lounge(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)
    room = store.create("Old", _OWNER)
    store.append(room.id, new_room_message("m-1", _OWNER, "Owner", "x"))

    renamed = store.rename(room.id, " New ")
    assert renamed is not None and renamed.title == "New"
    reread = store.read(room.id)
    assert reread is not None and reread.title == "New"
    assert store.rename(ChatId("room-0000000000000000"), "x") is None
    assert store.find_by_title("new") is not None and store.find_by_title(LOUNGE_TITLE) is None

    lounge = store.ensure_lounge(_BOB)
    assert lounge.title == LOUNGE_TITLE and store.ensure_lounge(_OWNER).id == lounge.id

    assert store.delete(room.id) is True and store.delete(room.id) is False
    assert store.read(room.id) is None and store.messages(room.id) == ()
    assert not (tmp_path / str(room.id)).exists()


def test_a_room_remembers_which_chats_it_asked_until_an_agent_answers(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)
    room = store.create("Lounge", _OWNER)
    main = ChatId("agent-00000000000000000000000000000001")

    store.note_agent_asked(room.id, main)
    store.note_agent_asked(room.id, main)
    assert store.snapshot(room).awaiting_chat_ids == (main,)

    store.note_agent_answered(room.id)
    assert store.snapshot(room).awaiting_chat_ids == ()


def test_an_unreadable_or_newer_record_is_skipped(tmp_path: Path) -> None:
    store = RoomStore(root=tmp_path)
    good = store.create("Good", _OWNER)
    (tmp_path / "room-aaaaaaaaaaaaaaaa").mkdir()
    (tmp_path / "room-aaaaaaaaaaaaaaaa" / ROOM_RECORD_FILENAME).write_text("{not json")
    newer = tmp_path / "room-bbbbbbbbbbbbbbbb"
    newer.mkdir()
    (newer / ROOM_RECORD_FILENAME).write_text(json.dumps({"version": 99, "id": "room-bbbbbbbbbbbbbbbb", "title": "x"}))

    assert [record.id for record in store.read_all()] == [good.id]
