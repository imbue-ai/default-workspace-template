from imbue.system_interface.naming import canonical_agent_name
from imbue.system_interface.naming import first_free_numbered_name
from imbue.system_interface.naming import is_name_conflict


def test_canonical_agent_name_dashes_spaces_and_strips_specials() -> None:
    assert canonical_agent_name("Chat 2") == "Chat-2"
    assert canonical_agent_name("hello world!") == "hello-world"
    assert canonical_agent_name("  My   planning  chat  ") == "My-planning-chat"


def test_canonical_agent_name_preserves_an_already_true_name() -> None:
    assert canonical_agent_name("Chat-2") == "Chat-2"
    assert canonical_agent_name("rich-stylish-sawfish") == "rich-stylish-sawfish"


def test_canonical_agent_name_never_starts_or_ends_with_a_dash_or_underscore() -> None:
    assert canonical_agent_name("-hello-") == "hello"
    assert canonical_agent_name("_hello_") == "hello"
    assert canonical_agent_name(" - hello - ") == "hello"


def test_canonical_agent_name_is_empty_when_nothing_usable_remains() -> None:
    assert canonical_agent_name("!!!") == ""
    assert canonical_agent_name("   ") == ""
    assert canonical_agent_name("") == ""


def test_first_free_numbered_name_starts_at_one_on_an_empty_machine() -> None:
    assert first_free_numbered_name("Chat", ()) == "Chat 1"


def test_first_free_numbered_name_fills_the_gap_a_destroyed_object_left() -> None:
    assert first_free_numbered_name("Chat", ("Chat 1", "Chat 3")) == "Chat 2"


def test_first_free_numbered_name_counts_past_a_full_run() -> None:
    assert first_free_numbered_name("Terminal", ("Terminal 1", "Terminal 2")) == "Terminal 3"


def test_first_free_numbered_name_collides_by_canonical_form() -> None:
    # An agent whose TRUE name is Chat-2 blocks "Chat 2": the display name
    # would canonicalize onto it, which is exactly the collision mngr rejects.
    assert first_free_numbered_name("Chat", ("Chat-2", "Chat 1")) == "Chat 3"


def test_first_free_numbered_name_matches_case_insensitively() -> None:
    # A user who renamed something to "chat 1" by hand still blocks the slot.
    assert first_free_numbered_name("Chat", ("chat 1", " CHAT 2 ")) == "Chat 3"


def test_first_free_numbered_name_ignores_other_words_and_near_misses() -> None:
    assert first_free_numbered_name("Chat", ("Terminal 1", "Chat", "Chat 1a", "MyChat 1")) == "Chat 1"


def test_is_name_conflict_compares_canonical_forms_case_insensitively() -> None:
    assert is_name_conflict("chat 2", ("Chat-2",))
    assert is_name_conflict("Chat 2", ("chat 2",))
    assert not is_name_conflict("Chat 2", ("Chat-21", "Chat 1"))
