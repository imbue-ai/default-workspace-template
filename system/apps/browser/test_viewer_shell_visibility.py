"""Browser: the viewer pauses its stream while the workspace shell hides its window, in a real Chromium.

The shell keeps a hidden window's page at its size (it moves it out of the viewport), so the shell's
``shell:hidden`` is the only sign the viewer gets. The viewer is framed by a stand-in shell page and imports
a stand-in contract module that exposes the handlers it registers; its stream socket is answered by a stub
that records what the page sends.
"""

from typing import Final

import pytest
from browser.testing import (
    BELT_SETTLE_MS,
    STREAM_URL,
    VIEWER_PATH,
    VIEWER_URL,
    wait_until,
)
from playwright.sync_api import Browser, Route

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_SHELL_URL: Final[str] = "http://localhost/shell"
_CONTRACT_URL: Final[str] = "http://localhost/_static/app_contract.js"
_SHELL_PAGE: Final[str] = (
    f'<!doctype html><iframe src="{VIEWER_URL}" style="width:800px;height:600px;border:0"></iframe>'
)
# The handlers the viewer gives ``connectToShell``, kept where the test can call them.
_CONTRACT_MODULE: Final[str] = (
    "export function connectToShell(handlers) {\n"
    "  window.shellHandlers = handlers;\n"
    "  return { focused() {}, location() {} };\n"
    "}\n"
)


def test_the_viewer_releases_the_stream_while_the_shell_hides_its_window_and_claims_it_when_shown(
    module_browser: Browser,
) -> None:
    claims: list[str] = []

    def answer(route: Route) -> None:
        url = route.request.url
        if url == _SHELL_URL:
            route.fulfill(status=200, content_type="text/html", body=_SHELL_PAGE)
        elif url == VIEWER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=VIEWER_PATH.read_text()
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
            STREAM_URL, lambda socket: socket.on_message(record_claim)
        )
        page = context.new_page()
        page.goto(_SHELL_URL)
        viewer = page.frame(url=VIEWER_URL)
        assert viewer is not None
        # A shown pane claims the stream as soon as its socket opens.
        wait_until(page, lambda: claims[-1:] == ["i"])
        wait_until(page, lambda: viewer.evaluate("window.shellHandlers !== undefined"))

        viewer.evaluate("window.shellHandlers.onHidden()")
        wait_until(page, lambda: claims[-1:] == ["h"])
        released_at = len(claims)
        # Its size is unchanged, so only the shell's word keeps it released past the viewer's 1.5s belt.
        assert (
            viewer.evaluate(
                "document.getElementById('stage').getBoundingClientRect().width"
            )
            == 800
        )
        page.wait_for_timeout(BELT_SETTLE_MS)
        assert claims[released_at:] == []

        viewer.evaluate("window.shellHandlers.onShown()")
        wait_until(page, lambda: claims[released_at:] == ["i"])
    finally:
        context.close()
