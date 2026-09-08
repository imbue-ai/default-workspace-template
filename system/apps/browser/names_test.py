import pytest

from browser import names


@pytest.mark.parametrize(
    "name",
    [
        "alex-smith",
        "a1-b2",
        "alex",
        "a",
        "a-b-c",
        "x9",
    ],
)
def test_is_valid_browser_name_accepts_good_names(name: str) -> None:
    assert names.is_valid_browser_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "Bad",  # uppercase
        "a--b",  # double dash
        "-a",  # leading dash
        "a-",  # trailing dash
        "a/b",  # slash (would break the URL path / profile dir)
        "a b",  # space
        "a_b",  # underscore not allowed
        "alex.smith",  # dot not allowed (no '.'/'..' path components)
        "a" * 41,  # too long (>40)
        "0",  # pure-numeric rejected (so legacy numeric profile dirs don't resurrect)
        "12",  # pure-numeric
    ],
)
def test_is_valid_browser_name_rejects_bad_names(name: str) -> None:
    assert names.is_valid_browser_name(name) is False


def test_first_free_numbered_browser_name_starts_at_one_on_an_empty_fleet() -> None:
    assert names.first_free_numbered_browser_name(set()) == "browser-1"


def test_first_free_numbered_browser_name_fills_the_gap_a_closed_browser_left() -> None:
    # Closing browser-1 deletes its profile and frees the slot; the next create
    # takes it rather than counting past it forever.
    assert names.first_free_numbered_browser_name({"browser-2", "browser-3"}) == "browser-1"
    assert names.first_free_numbered_browser_name({"browser-1", "browser-3"}) == "browser-2"


def test_first_free_numbered_browser_name_counts_past_a_full_run() -> None:
    assert names.first_free_numbered_browser_name({"browser-1", "browser-2"}) == "browser-3"


def test_first_free_numbered_browser_name_ignores_legacy_and_near_miss_names() -> None:
    # Random-named legacy browsers and hand-picked names hold their own names
    # without shifting the numbering.
    assert names.first_free_numbered_browser_name({"alex-smith", "browser-abc", "browser-01x"}) == "browser-1"


def test_minted_numbered_names_are_always_valid() -> None:
    # A minted name is used as a URL segment and a profile-dir suffix unchanged.
    taken: set[str] = set()
    for _ in range(50):
        name = names.first_free_numbered_browser_name(taken)
        assert names.is_valid_browser_name(name), f"minted invalid name: {name!r}"
        taken.add(name)
