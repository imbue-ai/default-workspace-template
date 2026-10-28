from pathlib import Path
from typing import Any

import pytest
from workspace_layout.primitives import ParticipantKey

from imbue.chat.authors import AUTHORS_FILENAME
from imbue.chat.authors import AuthorAnnotation
from imbue.chat.authors import AuthorStore
from imbue.chat.authors import author_prefix
from imbue.chat.authors import is_unprefixable_input
from imbue.chat.authors import new_author_annotation
from imbue.chat.authors import prefix_safe_name
from imbue.chat.authors import prefix_safe_text
from imbue.chat.authors import resolve_author
from imbue.chat.authors import split_author_prefix
from imbue.chat.authors import stamp_authors
from imbue.chat.authors import text_sha256
from imbue.chat.primitives import ChatId
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.model_update import to_update

_BOB = ParticipantKey("user:user-bob-4471")
_CLAUDE = ParticipantKey("agent:va-0123456789abcdef0123456789abcdef")
_CHAT = ChatId("agent-00000000000000000000000000000001")


def _annotation(
    message_id: str, author: ParticipantKey, name: str, delivered: str, timestamp: str
) -> AuthorAnnotation:
    annotation = new_author_annotation(message_id, author, name, delivered)
    return annotation.model_copy_update(to_update(annotation.field_ref().timestamp, IsoTimestamp(timestamp)))


def _user_event(content: str, timestamp: str = "2026-10-22T10:00:00.500Z") -> dict[str, Any]:
    return {
        "type": "user_message",
        "event_id": f"evt-{text_sha256(content)[:8]}",
        "content": content,
        "timestamp": timestamp,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[from: Bob] hello", ("Bob", "hello")),
        ("[from: Bob in #Lounge] hello", ("Bob", "hello")),
        ("hello", (None, "hello")),
        ("[from: ] hello", (None, "[from: ] hello")),
        ("[from:  ] hello", (None, "[from:  ] hello")),
        ("first\n[from: Ana María] second\nthird", ("Ana María", "first\nsecond\nthird")),
    ],
)
def test_the_prefix_is_read_from_the_start_of_the_text_or_of_any_merged_line(
    text: str, expected: tuple[str | None, str]
) -> None:
    assert split_author_prefix(text) == expected


def test_the_prefix_is_rendered_for_a_person_and_for_a_room() -> None:
    assert author_prefix("Bob") == "[from: Bob] "
    assert author_prefix("Bob", "Lounge") == "[from: Bob in #Lounge] "
    assert split_author_prefix(author_prefix("Bob", "Lounge") + "hi") == ("Bob", "hi")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bob", "Bob"),
        ("Bob [admin]", "Bob [admin"),
        ("Ana\nMar\u00eda  Soler", "Ana Mar\u00eda Soler"),
        ("]]", "user:user-bob-4471"),
    ],
)
def test_a_name_is_made_safe_for_the_prefix_and_reads_back_from_it(name: str, expected: str) -> None:
    safe = prefix_safe_name(name, _BOB)
    assert safe == expected
    assert split_author_prefix(author_prefix(safe) + "hi") == (safe, "hi")


@pytest.mark.parametrize(
    ("title", "expected"),
    [("Lounge", "Lounge"), ("Ops] team\nnight", "Ops team night"), ("]", "room-0123456789abcdef")],
)
def test_a_room_title_is_made_safe_for_the_prefix_and_the_name_reads_back_from_it(title: str, expected: str) -> None:
    safe = prefix_safe_text(title, "room-0123456789abcdef")
    assert safe == expected
    assert split_author_prefix(author_prefix("Bob", safe) + "hi") == ("Bob", "hi")


@pytest.mark.parametrize(
    ("text", "expected"), [("/compact", True), ("  !ls", True), ("hello /there", False), ("", False)]
)
def test_a_slash_command_or_bash_input_is_never_prefixed(text: str, expected: bool) -> None:
    assert is_unprefixable_input(text) is expected


def test_an_author_is_resolved_by_the_text_hash_first_which_keeps_an_unprefixed_command_its_author() -> None:
    annotations = [
        _annotation("m1", _BOB, "Bob", "/compact", "2026-10-22T10:00:00.000000000Z"),
        _annotation("m2", _CLAUDE, "Josh's Claude", "[from: Josh's Claude] hi", "2026-10-22T10:00:02.000000000Z"),
    ]
    assert resolve_author("/compact", "2026-10-22T10:00:00.500Z", annotations) == (_BOB, "Bob")
    assert resolve_author("[from: Josh's Claude] hi", "2026-10-22T10:00:02.100Z", annotations) == (
        _CLAUDE,
        "Josh's Claude",
    )


def test_the_same_text_from_two_senders_goes_to_the_nearer_annotation() -> None:
    annotations = [
        _annotation("m1", _BOB, "Bob", "/compact", "2026-10-22T10:00:00.000000000Z"),
        _annotation("m2", _CLAUDE, "Josh's Claude", "/compact", "2026-10-22T10:05:00.000000000Z"),
    ]
    assert resolve_author("/compact", "2026-10-22T10:00:01Z", annotations) == (_BOB, "Bob")
    assert resolve_author("/compact", "2026-10-22T10:04:59Z", annotations) == (_CLAUDE, "Josh's Claude")


def test_a_prefix_without_a_matching_hash_resolves_by_name_else_to_a_synthetic_key() -> None:
    annotations = [_annotation("m1", _BOB, "Bob", "[from: Bob] original", "2026-10-22T10:00:00.000000000Z")]
    # A harness merged two queued messages into one turn: the hash no longer matches, the name still does.
    assert resolve_author("[from: Bob] original\nand more", "2026-10-22T10:00:01Z", annotations) == (_BOB, "Bob")
    assert resolve_author("[from: Someone Else] typed by hand", "2026-10-22T10:00:01Z", annotations) == (
        ParticipantKey("name:Someone Else"),
        "Someone Else",
    )
    assert resolve_author("plain words", "2026-10-22T10:00:01Z", annotations) is None
    # A prefix naming nobody (only spaces) is not a prefix, so it is the owner's and never makes an empty key.
    assert resolve_author("[from:  ] typed by hand", "2026-10-22T10:00:01Z", annotations) is None


def test_stamping_marks_only_user_messages_with_an_author_and_never_restamps() -> None:
    annotations = [_annotation("m1", _BOB, "Bob", "[from: Bob] hello", "2026-10-22T10:00:00.000000000Z")]
    bob = _user_event("[from: Bob] hello")
    owner = _user_event("just me")
    assistant = {"type": "assistant_message", "event_id": "a1", "text": "[from: Bob] hello", "timestamp": "x"}
    stamp_authors([bob, owner, assistant], annotations)

    assert (bob["author"], bob["author_name"]) == ("user:user-bob-4471", "Bob")
    assert "author" not in owner and "author" not in assistant
    bob["author"] = "kept"
    stamp_authors([bob], annotations)
    assert bob["author"] == "kept"


def test_a_chat_with_no_annotations_still_stamps_a_hand_typed_prefix(tmp_path: Path) -> None:
    store = AuthorStore(root=tmp_path)
    typed = _user_event("[from: Someone Else] typed by hand")
    owner = _user_event("just me")
    store.stamp_events(_CHAT, [typed, owner])

    assert (typed["author"], typed["author_name"]) == ("name:Someone Else", "Someone Else")
    assert "author" not in owner


def test_the_store_appends_lines_and_rereads_them_only_when_the_file_changed(tmp_path: Path) -> None:
    store = AuthorStore(root=tmp_path)
    assert store.annotations(_CHAT) == ()
    first = new_author_annotation("m1", _BOB, "Bob", "[from: Bob] hello")
    store.record(_CHAT, first)

    (read,) = store.annotations(_CHAT)
    assert read == first
    assert (tmp_path / str(_CHAT) / AUTHORS_FILENAME).read_text().count("\n") == 1
    # A line nobody can read is skipped, and the next change is picked up.
    path = tmp_path / str(_CHAT) / AUTHORS_FILENAME
    path.write_text(path.read_text() + "not json\n")
    store.record(_CHAT, new_author_annotation("m2", _CLAUDE, "Josh's Claude", "x"))
    assert [annotation.message_id for annotation in store.annotations(_CHAT)] == ["m1", "m2"]

    event = _user_event("[from: Bob] hello")
    store.stamp_events(_CHAT, [event])
    assert event["author"] == "user:user-bob-4471"
