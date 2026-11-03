import pytest

from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.primitives import ParticipantKey
from workspace_layout.roster import PARTICIPANT_COLORS
from workspace_layout.roster import assign_handles
from workspace_layout.roster import handle_of_name
from workspace_layout.roster import participant_color


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Josh", "Josh"),
        ("Main chat", "Main-chat"),
        ("Newsreader  Chat", "Newsreader-Chat"),
        ("  Ana María  ", "Ana-Mara"),
        ("a.b_c-d", "a.b_c-d"),
        ("!!!", "someone"),
    ],
)
def test_a_handle_is_the_name_hyphenated_and_held_to_its_alphabet(name: str, expected: str) -> None:
    assert handle_of_name(name) == expected


def test_colliding_handles_get_a_numeric_suffix_in_order_case_insensitively() -> None:
    assert assign_handles(["Josh", "josh", "Josh", "Ana"]) == ["Josh", "josh-2", "Josh-3", "Ana"]


def test_handles_already_taken_count_as_collisions() -> None:
    assert assign_handles(["Agent", "Josh"], taken=["agent"]) == ["Agent-2", "Josh"]


def test_a_participant_colour_is_one_of_the_palette_and_stable() -> None:
    key = ParticipantKey("user:user-bob-4471")
    assert participant_color(key) in PARTICIPANT_COLORS
    assert participant_color(key) == participant_color(ParticipantKey("user:user-bob-4471"))


@pytest.mark.parametrize(
    "value",
    ["owner", "workspace-agent", "user:user-bob-4471", "email:bob@example.com", "agent:va-0123", "name:Ana María"],
)
def test_every_participant_key_form_is_accepted(value: str) -> None:
    assert ParticipantKey(value) == value


@pytest.mark.parametrize("value", ["", "user:", "user:bob smith", "email:nobody", "guest", "name:"])
def test_a_malformed_participant_key_is_refused(value: str) -> None:
    with pytest.raises(InvalidLayoutValueError):
        ParticipantKey(value)
