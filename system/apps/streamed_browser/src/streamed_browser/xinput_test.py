"""Unit tests for the display-free parts of the input router."""

from streamed_browser.xinput import diff_button_mask


def test_button_transitions_are_diffed() -> None:
    # Left press, then right press while left held, then both release.
    assert diff_button_mask(0b000, 0b001, 0) == [("button", 1)]
    assert diff_button_mask(0b001, 0b101, 0) == [("button", 3)]
    assert sorted(diff_button_mask(0b101, 0b000, 0)) == [("button", 1), ("button", 3)]


def test_scroll_bits_fire_on_rising_edge_only() -> None:
    assert diff_button_mask(0b000, 0b01000, 3) == [("scroll", 4)]
    assert diff_button_mask(0b000, 0b10000, 1) == [("scroll", 5)]
    # Held scroll bit is not a repeat, and a zero magnitude fires nothing.
    assert diff_button_mask(0b01000, 0b01000, 3) == []
    assert diff_button_mask(0b000, 0b01000, 0) == []


def test_scroll_and_button_changes_combine() -> None:
    actions = diff_button_mask(0b001, 0b01000, 2)
    assert ("button", 1) in actions and ("scroll", 4) in actions
