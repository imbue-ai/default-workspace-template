"""Shared setup for the viewer page's browser tests: the page served through request routing, and its stream socket
answered by a stub that records what the page sends, so no daemon and no Chromium-under-test runs."""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import Page

VIEWER_PATH: Final[Path] = Path(__file__).parent / "assets" / "index.html"
VIEWER_URL: Final[str] = "http://localhost/?session=browser-1"
STREAM_URL: Final[str] = "ws://localhost/browsers/browser-1/stream?**"
# Longer than the viewer's 1.5s belts, which re-check the pane's size and visibility on a cadence.
BELT_SETTLE_MS: Final[int] = 1600

_POLL_MS: Final[int] = 50
_POLL_ATTEMPTS: Final[int] = 100


def wait_until(page: Page, is_done: Callable[[], bool]) -> None:
    """Poll ``is_done``, letting Playwright deliver the stub socket's events between polls."""
    for _ in range(_POLL_ATTEMPTS):
        if is_done():
            return
        page.wait_for_timeout(_POLL_MS)
    pytest.fail("the viewer did not send what was expected")
