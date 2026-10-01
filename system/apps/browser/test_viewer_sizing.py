"""Browser: the viewer page sizes its pane to a phone's window and reports it to the stream, in a real Chromium.

The page is served through request routing and its stream socket is answered by a stub that records what the page
sends, so no daemon and no Chromium-under-test runs.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import BrowserContext, Page, Route, WebSocketRoute

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_VIEWER_PATH: Final[Path] = (
    Path(__file__).parent / "src" / "browser" / "assets" / "index.html"
)
_VIEWER_URL: Final[str] = "http://localhost/?session=browser-1"
_STREAM_URL: Final[str] = "ws://localhost/browsers/browser-1/stream?**"
_POLL_MS: Final[int] = 50
_POLL_ATTEMPTS: Final[int] = 100
# Longer than the viewer's resize debounce, so a size it was going to send has been sent.
_SETTLE_MS: Final[int] = 600
# Longer than the viewer's 1.5s size belt. The pane's first visibility clears the last size sent, and when the socket
# is not open yet the belt is what sends it again; once it has, it sends nothing more for an unchanged pane.
_BELT_SETTLE_MS: Final[int] = 1600


def _wait_until(page: Page, is_done: Callable[[], bool]) -> None:
    """Poll ``is_done``, letting Playwright deliver the stub socket's events between polls."""
    for _ in range(_POLL_ATTEMPTS):
        if is_done():
            return
        page.wait_for_timeout(_POLL_MS)
    pytest.fail("the viewer did not send what was expected")


def test_the_pane_fills_a_phone_and_a_rotation_reports_its_size_again(
    phone_context: BrowserContext,
) -> None:
    stream_urls: list[str] = []
    resizes: list[str] = []

    def answer(route: Route) -> None:
        if route.request.url == _VIEWER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=_VIEWER_PATH.read_text()
            )
        else:
            route.fulfill(status=404, body="")

    def record_resize(message: str | bytes) -> None:
        if isinstance(message, str) and message.startswith("r,"):
            resizes.append(message)

    def stream(socket: WebSocketRoute) -> None:
        stream_urls.append(socket.url)
        socket.on_message(record_resize)

    phone_context.route("**/*", answer)
    page = phone_context.new_page()
    page.route_web_socket(_STREAM_URL, stream)
    page.goto(_VIEWER_URL)
    _wait_until(page, lambda: len(stream_urls) == 1)

    # The pane is the whole phone window, and the stream is opened at its size (widths are sent even).
    assert page.locator("#stage").bounding_box() == {
        "x": 0,
        "y": 0,
        "width": 393,
        "height": 852,
    }
    assert stream_urls == ["ws://localhost/browsers/browser-1/stream?w=392&h=852"]

    # The viewer may re-report its size once after it first becomes visible; let that pass.
    page.wait_for_timeout(_BELT_SETTLE_MS)
    settled = len(resizes)

    # A rotation that settles on the size already reported reports it again.
    page.evaluate('window.dispatchEvent(new Event("orientationchange"))')
    _wait_until(page, lambda: len(resizes) > settled)

    assert resizes[settled:] == ["r,392,852"]

    # A real rotation reports the new size and nothing else. The resize can be reported before the rotation
    # event too, which then reports it again.
    page.set_viewport_size({"width": 852, "height": 393})
    page.evaluate('window.dispatchEvent(new Event("orientationchange"))')
    _wait_until(page, lambda: len(resizes) > settled + 1)
    page.wait_for_timeout(_SETTLE_MS)
    assert set(resizes[settled + 1 :]) == {"r,852,392"}
