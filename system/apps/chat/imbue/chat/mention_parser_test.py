from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.testing import roster_participant

from imbue.chat.mention_parser import find_mentions
from imbue.chat.mention_parser import handle_table
from imbue.chat.primitives import ChatId

_JOSH = ParticipantKey("user:user-josh-0001")
_BOB = ParticipantKey("user:user-bob-4471")
_AGENT = ParticipantKey("workspace-agent")
_MAIN = ChatId("agent-00000000000000000000000000000001")
_READER = ChatId("agent-00000000000000000000000000000002")

_ROSTER = (
    roster_participant(_JOSH, display_name="Josh", handle="Josh", is_owner=True),
    roster_participant(_BOB, display_name="Josh", handle="Josh-2"),
    roster_participant(_AGENT, kind=ParticipantKind.WORKSPACE_AGENT, display_name="Atlas", handle="agent"),
)


def _table():
    return handle_table(
        _ROSTER, [(_MAIN, "Main chat"), (_READER, "Newsreader Chat"), (ChatId("room-0123456789abcdef"), "Josh")]
    )


def test_the_table_keeps_the_rosters_handles_and_adds_each_chat_by_title_after_them() -> None:
    table = _table()

    assert [(target.handle, target.kind) for target in table] == [
        ("Josh", "person"),
        ("Josh-2", "person"),
        ("agent", "workspace_agent"),
        ("Main-chat", "chat"),
        ("Newsreader-Chat", "chat"),
        # A room called like a person collides with both Joshes and takes the next free suffix.
        ("Josh-3", "chat"),
    ]
    assert table[3].chat_id == _MAIN and table[3].key is None and table[3].is_chat
    assert table[0].key == _JOSH and table[0].chat_id is None


def test_mentions_resolve_case_insensitively_once_each_in_order_and_never_inside_a_word() -> None:
    table = _table()

    found = find_mentions("@Agent, see @josh-2 and @AGENT; email me@josh.example or @@nobody", table)

    assert [target.handle for target in found] == ["agent", "Josh-2"]


def test_trailing_punctuation_is_tried_off_a_mention() -> None:
    table = _table()

    found = find_mentions("ask @Main-chat. then @Newsreader-Chat... and @Josh-", table)

    assert [target.handle for target in found] == ["Main-chat", "Newsreader-Chat", "Josh"]


def test_an_unknown_handle_and_a_self_mention_are_just_words_here() -> None:
    table = _table()

    assert find_mentions("hi @Someone", table) == ()
    # Whether a mention of oneself does anything is the actor's call; the parser reports it like any other.
    assert [target.key for target in find_mentions("I am @Josh", table)] == [_JOSH]
