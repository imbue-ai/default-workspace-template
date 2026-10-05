"""Browser: the viewer pauses its stream while the workspace shell hides its window, in a real Chromium.

The shell no longer collapses a hidden window's frame to 0x0 (it keeps the page's size and moves it out of
the viewport), so the shell's ``shell:hidden`` is the only sign the viewer gets. The viewer is framed by a
stand-in shell page and imports a stand-in contract module that exposes the handlers it registers; its stream
socket is answered by a stub that records what the page sends.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import Browser, Route

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_VIEWER_PATH: Final[Path] = (
    Path(__file__).parent / "src" / "browser" / "assets" / "index.html"
)
_SHELL_URL: Final[str] = "http://localhost/shell"
_VIEWER_URL: Final[str] = "http://localhost/?session=browser-1"
_CONTRACT_URL: Final[str] = "http://localhost/_static/app_contract.js"
_STREAM_URL: Final[str] = "ws://localhost/browsers/browser-1/stream?**"
_SHELL_PAGE: Final[str] = (
    f'<!doctype html><iframe src="{_VIEWER_URL}" style="width:800px;height:600px;border:0"></iframe>'
)
# The handlers the viewer gives ``connectToShell``, kept where the test can call them.
_CONTRACT_MODULE: Final[str] = (
    "export function connectToShell(handlers) {\n"
    "  window.shellHandlers = handlers;\n"
    "  return { focused() {}, location() {} };\n"
    "}\n"
)
_POLL_MS: Final[int] = 50
_POLL_ATTEMPTS: Final[int] = 100
# Longer than the viewer's 1.5s visibility belt, which re-checks the pane on a cadence.
_BELT_SETTLE_MS: Final[int] = 1600


def _wait_until(is_done: Callable[[], bool], wait: Callable[[int], None]) -> None:
    for _ in range(_POLL_ATTEMPTS):
        if is_done():
            return
        wait(_POLL_MS)
    pytest.fail("the viewer did not send what was expected")


def test_the_viewer_releases_the_stream_while_the_shell_hides_its_window_and_claims_it_when_shown(
    module_browser: Browser,
) -> None:
    claims: list[str] = []

    def answer(route: Route) -> None:
        url = route.request.url
        if url == _SHELL_URL:
            route.fulfill(status=200, content_type="text/html", body=_SHELL_PAGE)
        elif url == _VIEWER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=_VIEWER_PATH.read_text()
            )
        elif url == _CONTRACT_URL:
            route.fulfill(
                status=200, content_type="text/javascript", body=_CONTRACT_MODULE
            )
        else:
            route.fulfill(status=404, body="")

    def record_claim(message: str | bytes) -> None:
        if isinstance(message, str) and message in ("i", "h"):
            claims.append(message)

    context = module_browser.new_context(viewport={"width": 1000, "height": 800})
    try:
        context.route("**/*", answer)
        context.route_web_socket(
            _STREAM_URL, lambda socket: socket.on_message(record_claim)
        )
        page = context.new_page()
        page.goto(_SHELL_URL)
        viewer = page.frame(url=_VIEWER_URL)
        assert viewer is not None
        # A shown pane claims the stream as soon as its socket opens.
        _wait_until(lambda: claims[-1:] == ["i"], page.wait_for_timeout)
        _wait_until(
            lambda: viewer.evaluate("window.shellHandlers !== undefined"),
            page.wait_for_timeout,
        )

        viewer.evaluate("window.shellHandlers.onHidden()")
        _wait_until(lambda: claims[-1:] == ["h"], page.wait_for_timeout)
        released_at = len(claims)
        # Its size is unchanged, so only the shell's word keeps it released past the viewer's 1.5s belt.
        assert (
            viewer.evaluate(
                "document.getElementById('stage').getBoundingClientRect().width"
            )
            == 800
        )
        page.wait_for_timeout(_BELT_SETTLE_MS)
        assert claims[released_at:] == []

        viewer.evaluate("window.shellHandlers.onShown()")
        _wait_until(lambda: claims[released_at:] == ["i"], page.wait_for_timeout)
    finally:
        context.close()
