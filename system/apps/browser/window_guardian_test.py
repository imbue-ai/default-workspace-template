"""Unit tests for the window guardian's browser-wide close signal. The X-driven methods
(_tick/_repin/_close/_is_browser_window/run) need a live Xvfb and are exercised end-to-end."""

from browser.window_guardian import signal_extra_closed
from browser.window_guardian import take_extra_closed


def test_extra_closed_signal_is_edge_triggered() -> None:
    browser_id = "guardian-test-edge"
    assert take_extra_closed(browser_id) is False  # nothing signalled yet
    signal_extra_closed(browser_id)
    assert take_extra_closed(browser_id) is True
    assert take_extra_closed(browser_id) is False  # drained -- only fires once per signal


def test_extra_closed_signal_is_per_browser() -> None:
    signal_extra_closed("guardian-test-a")
    assert take_extra_closed("guardian-test-b") is False  # a different browser is unaffected
    assert take_extra_closed("guardian-test-a") is True
