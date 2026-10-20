"""Shared setup for the viewer page's browser tests: the page as the daemon's own route renders it (under its
Content-Security-Policy), handed to the browser through request routing, and its stream socket answered by a stub
that records what the page sends, so no daemon server and no Chromium-under-test runs."""

from collections.abc import Callable
from typing import Final

import pytest
from app_manifest.content_security_policy import CONTENT_SECURITY_POLICY_HEADER
from playwright.sync_api import Page, Route

from browser import runner

VIEWER_URL: Final[str] = "http://localhost/?session=browser-1"
STREAM_URL: Final[str] = "ws://localhost/browsers/browser-1/stream?**"
# Longer than the viewer's 1.5s belts, which re-check the pane's size and visibility on a cadence.
BELT_SETTLE_MS: Final[int] = 1600

_POLL_MS: Final[int] = 50
_POLL_ATTEMPTS: Final[int] = 100


def fulfill_with_viewer(route: Route) -> None:
    """Answer ``route`` with the viewer page and the policy the daemon serves it under."""
    served = runner.application.test_client().get("/")
    route.fulfill(
        status=served.status_code,
        content_type=served.mimetype,
        body=served.get_data(as_text=True),
        headers={
            CONTENT_SECURITY_POLICY_HEADER: served.headers[
                CONTENT_SECURITY_POLICY_HEADER
            ]
        },
    )


def wait_until(page: Page, is_done: Callable[[], bool]) -> None:
    """Poll ``is_done``, letting Playwright deliver the stub socket's events between polls."""
    for _ in range(_POLL_ATTEMPTS):
        if is_done():
            return
        page.wait_for_timeout(_POLL_MS)
    pytest.fail("the viewer did not send what was expected")
