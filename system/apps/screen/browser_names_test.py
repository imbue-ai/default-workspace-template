import pytest

from screen.browser_names import first_free_numbered_browser_name
from screen.browser_names import is_valid_browser_name
from screen.errors import InvalidBrowserNameError
from screen.primitives import BrowserName


@pytest.mark.parametrize("name", ["alex-smith", "a1-b2", "alex", "a", "a-b-c", "x9", "browser-1"])
def test_valid_names_are_accepted(name: str) -> None:
    assert is_valid_browser_name(name) is True
    assert BrowserName(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "Bad", "a--b", "-a", "a-", "a/b", "a b", "a_b", "alex.smith", "a" * 41, "0", "12"],
)
def test_invalid_names_are_rejected(name: str) -> None:
    assert is_valid_browser_name(name) is False
    with pytest.raises(InvalidBrowserNameError):
        BrowserName(name)


def test_the_first_free_numbered_name_fills_gaps_and_counts_past_a_full_run() -> None:
    assert first_free_numbered_browser_name(set()) == "browser-1"
    assert first_free_numbered_browser_name({"browser-2", "browser-3"}) == "browser-1"
    assert first_free_numbered_browser_name({"browser-1", "browser-3"}) == "browser-2"
    assert first_free_numbered_browser_name({"browser-1", "browser-2"}) == "browser-3"
    # Legacy and near-miss names hold their own names without shifting the numbering.
    assert first_free_numbered_browser_name({"alex-smith", "browser-abc", "browser-01x"}) == "browser-1"


def test_minted_numbered_names_are_always_valid() -> None:
    taken: set[str] = set()
    for _ in range(50):
        name = first_free_numbered_browser_name(taken)
        assert is_valid_browser_name(name), name
        taken.add(name)
