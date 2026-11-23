from pathlib import Path

from imbue.chat.primitives import ChatId
from imbue.chat.suggestion_store import ChatSuggestionStore
from imbue.chat.suggestion_store import MAX_REMEMBERED_SETS_PER_CHAT
from imbue.chat.suggestion_store import with_set_remembered
from imbue.chat.suggestions import SUGGESTIONS_FILENAME
from imbue.chat.suggestions import read_chat_suggestions
from imbue.chat.suggestions import write_chat_suggestions
from imbue.chat.testing import make_generated_suggestions

_CHAT_ID = ChatId("agent-1")
_OTHER_CHAT_ID = ChatId("agent-2")


def test_a_set_is_remembered_when_it_is_stored_and_stays_once_the_file_holds_another(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)

    store.write(_CHAT_ID, make_generated_suggestions("reply-1"))
    store.write(_CHAT_ID, make_generated_suggestions("reply-2"))

    assert store.read(_CHAT_ID) == make_generated_suggestions("reply-2")
    assert read_chat_suggestions(tmp_path / _CHAT_ID) == make_generated_suggestions("reply-2")
    assert store.remembered(_CHAT_ID) == (make_generated_suggestions("reply-1"), make_generated_suggestions("reply-2"))


def test_a_removed_set_leaves_the_file_gone_and_stays_remembered(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)
    store.write(_CHAT_ID, make_generated_suggestions("reply-1"))

    assert store.remove(_CHAT_ID) is True

    assert store.read(_CHAT_ID) is None
    assert not (tmp_path / _CHAT_ID / SUGGESTIONS_FILENAME).exists()
    assert store.remembered(_CHAT_ID) == (make_generated_suggestions("reply-1"),)
    assert store.remove(_CHAT_ID) is False


def test_the_set_a_start_finds_stored_is_remembered_once_it_is_read(tmp_path: Path) -> None:
    """A store built over a folder another run wrote knows nothing of the chat until it reads its file."""
    write_chat_suggestions(tmp_path / _CHAT_ID, make_generated_suggestions("reply-1"))
    store = ChatSuggestionStore(chat_files_root=tmp_path)
    remembered_before_the_read = store.remembered(_CHAT_ID)

    stored = store.read(_CHAT_ID)
    store.read(_CHAT_ID)

    assert remembered_before_the_read == ()
    assert stored == make_generated_suggestions("reply-1")
    assert store.remembered(_CHAT_ID) == (make_generated_suggestions("reply-1"),)


def test_a_chat_with_nothing_stored_has_nothing_remembered(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)

    assert store.read(_CHAT_ID) is None
    assert store.remembered(_CHAT_ID) == ()


def test_each_chat_has_its_own_sets_and_a_deleted_chats_are_forgotten(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)
    store.write(_CHAT_ID, make_generated_suggestions("reply-1"))
    store.write(_OTHER_CHAT_ID, make_generated_suggestions("reply-9"))

    store.forget(_CHAT_ID)
    store.forget(ChatId("agent-never"))

    assert store.remembered(_CHAT_ID) == ()
    assert store.remembered(_OTHER_CHAT_ID) == (make_generated_suggestions("reply-9"),)
    # Forgetting is the memory's alone: the chat's folder goes with the chat, by whoever deletes it.
    assert read_chat_suggestions(tmp_path / _CHAT_ID) == make_generated_suggestions("reply-1")


def test_a_set_stored_again_for_a_reply_replaces_the_one_remembered_as_the_newest(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)
    store.write(_CHAT_ID, make_generated_suggestions("reply-1", "Yes"))
    store.write(_CHAT_ID, make_generated_suggestions("reply-2"))

    store.write(_CHAT_ID, make_generated_suggestions("reply-1", "No"))

    assert store.remembered(_CHAT_ID) == (
        make_generated_suggestions("reply-2"),
        make_generated_suggestions("reply-1", "No"),
    )


def test_only_the_newest_sets_of_a_chat_are_remembered(tmp_path: Path) -> None:
    store = ChatSuggestionStore(chat_files_root=tmp_path)
    stored_count = MAX_REMEMBERED_SETS_PER_CHAT + 3

    for index in range(stored_count):
        store.write(_CHAT_ID, make_generated_suggestions(f"reply-{index}"))

    remembered = store.remembered(_CHAT_ID)
    assert MAX_REMEMBERED_SETS_PER_CHAT == 200
    assert len(remembered) == MAX_REMEMBERED_SETS_PER_CHAT
    assert remembered[0] == make_generated_suggestions("reply-3")
    assert remembered[-1] == make_generated_suggestions(f"reply-{stored_count - 1}")


def test_remembering_a_set_keeps_the_newest_up_to_the_limit_and_leaves_what_it_was_given_alone() -> None:
    remembered = {"reply-1": make_generated_suggestions("reply-1"), "reply-2": make_generated_suggestions("reply-2")}

    with_a_third = with_set_remembered(remembered, make_generated_suggestions("reply-3"), 2)
    with_the_first_again = with_set_remembered(remembered, make_generated_suggestions("reply-1", "No"), 2)

    assert list(with_a_third) == ["reply-2", "reply-3"]
    assert list(with_the_first_again) == ["reply-2", "reply-1"]
    assert with_the_first_again["reply-1"] == make_generated_suggestions("reply-1", "No")
    assert list(remembered) == ["reply-1", "reply-2"]
