"""Browser: the viewer streams only while the workspace shell shows its window, in a real Chromium.

The shell keeps a hidden window's page at its size (it moves it out of the viewport), so the shell's
``shell:shown`` and ``shell:hidden`` are the only sign the viewer gets. A stand-in shell frames the viewer and
talks to it as the shell does: a handshake and the window's visibility after every load of the frame, and a
message whenever the visibility changes. The viewer imports a stand-in contract module that hands those
messages to its handlers as the real one does. Its stream socket is answered by a stub that records what the
page opens and sends.
"""

import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import pytest
from browser.testing import (
    BELT_SETTLE_MS,
    STREAM_URL,
    VIEWER_PATH,
    VIEWER_URL,
    wait_until,
)
from playwright.sync_api import (
    Browser,
    Frame,
    Page,
    Route,
    WebSocketRoute,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_SHELL_URL: Final[str] = "http://localhost/shell"
_CONTRACT_URL: Final[str] = "http://localhost/_static/app_contract.js"
_SHELL_PAGE: Final[str] = f"""<!doctype html>
<iframe id="page" src="{VIEWER_URL}" style="width:800px;height:600px;border:0"></iframe>
<script>
  const frame = document.getElementById("page");
  window.tell = (type) => frame.contentWindow.postMessage({{ type }}, "*");
  const atLoad = new URLSearchParams(location.search).get("visibility");
  frame.addEventListener("load", () => {{
    window.tell("shell:handshake");
    window.tell(atLoad === "hidden" ? "shell:hidden" : "shell:shown");
  }});
</script>"""
_CONTRACT_MODULE: Final[str] = """export function connectToShell(handlers) {
  addEventListener("message", (event) => {
    if (event.source !== parent) return;
    if (event.data.type === "shell:shown") handlers.onShown?.();
    if (event.data.type === "shell:hidden") handlers.onHidden?.();
  });
  parent.postMessage({ type: "shell:capabilities", ...handlers.capabilities }, "*");
  return { focused() {}, location() {} };
}
"""
_STAGE_SIZE: Final[str] = (
    "(() => { const box = document.getElementById('stage').getBoundingClientRect();"
    " return [box.width, box.height]; })()"
)


class _Viewer:
    """The framed viewer's stream as the stub saw it: the URLs it opened and the claims it sent."""

    def __init__(self) -> None:
        self.stream_urls: list[str] = []
        self.claims: list[str] = []

    def stream(self, socket: WebSocketRoute) -> None:
        self.stream_urls.append(socket.url)
        socket.on_message(self._record)

    def _record(self, message: str | bytes) -> None:
        if isinstance(message, str) and message in ("i", "h"):
            self.claims.append(message)


@contextmanager
def _framed_viewer(
    module_browser: Browser, visibility_at_load: str, is_contract_served: bool
) -> Iterator[tuple[Page, Frame, _Viewer]]:
    viewer = _Viewer()

    def answer(route: Route) -> None:
        url = route.request.url
        if url.startswith(_SHELL_URL):
            route.fulfill(status=200, content_type="text/html", body=_SHELL_PAGE)
        elif url == VIEWER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=VIEWER_PATH.read_text()
            )
        elif url == _CONTRACT_URL and is_contract_served:
            route.fulfill(
                status=200, content_type="text/javascript", body=_CONTRACT_MODULE
            )
        else:
            route.fulfill(status=404, body="")

    context = module_browser.new_context(viewport={"width": 1000, "height": 800})
    try:
        context.route("**/*", answer)
        context.route_web_socket(STREAM_URL, viewer.stream)
        page = context.new_page()
        page.goto(f"{_SHELL_URL}?visibility={visibility_at_load}")
        frame = page.frame(url=VIEWER_URL)
        assert frame is not None
        yield page, frame, viewer
    finally:
        context.close()


def test_the_viewer_releases_the_stream_while_the_shell_hides_its_window_and_claims_it_when_shown(
    module_browser: Browser,
) -> None:
    with _framed_viewer(
        module_browser, visibility_at_load="shown", is_contract_served=True
    ) as (page, frame, viewer):
        # A shown pane claims the stream as soon as its socket opens.
        wait_until(page, lambda: viewer.claims[-1:] == ["i"])
        shown_size = frame.evaluate(_STAGE_SIZE)

        page.evaluate("window.tell('shell:hidden')")
        wait_until(page, lambda: viewer.claims[-1:] == ["h"])
        released_at = len(viewer.claims)
        # Its size is unchanged, so only the shell's word keeps it released past the viewer's 1.5s belt.
        assert frame.evaluate(_STAGE_SIZE) == shown_size
        page.wait_for_timeout(BELT_SETTLE_MS)
        assert viewer.claims[released_at:] == []

        page.evaluate("window.tell('shell:shown')")
        wait_until(page, lambda: viewer.claims[released_at:] == ["i"])


def test_a_viewer_loaded_in_a_hidden_window_opens_no_stream_until_the_shell_shows_it(
    module_browser: Browser,
) -> None:
    """A window reloaded while minimized must neither claim the stream from the viewer in front nor size the
    browser to its own pane: it connects only once shown, at its size, and claims then."""
    with _framed_viewer(
        module_browser, visibility_at_load="hidden", is_contract_served=True
    ) as (page, frame, viewer):
        page.wait_for_timeout(BELT_SETTLE_MS)
        assert viewer.stream_urls == []

        page.evaluate("window.tell('shell:shown')")
        wait_until(page, lambda: viewer.claims == ["i"])
        # Sized like every connect: the pane's box rounded to whole pixels, then down to an even count.
        width, height = (
            math.floor(side + 0.5) & ~1 for side in frame.evaluate(_STAGE_SIZE)
        )
        assert viewer.stream_urls == [
            f"ws://localhost/browsers/browser-1/stream?w={width}&h={height}"
        ]


def test_a_framed_viewer_whose_contract_cannot_load_streams_as_a_page_on_its_own(
    module_browser: Browser,
) -> None:
    with _framed_viewer(
        module_browser, visibility_at_load="shown", is_contract_served=False
    ) as (
        page,
        _frame,
        viewer,
    ):
        wait_until(page, lambda: viewer.claims == ["i"])
        assert len(viewer.stream_urls) == 1
