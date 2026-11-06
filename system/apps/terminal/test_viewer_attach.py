"""Browser: a framed wrapper attaches its pty the way the shell's handshake says, from the first pointing.

A stand-in shell page frames the wrapper and greets it when the test says so (the real shell greets once the
page's contract module is up); until then the wrapper points its pty frame at nothing, since a viewed terminal
attached before the word would join as a full client and size its owner's window for a moment (the multiplayer
spec, section 8.3). A greeting that says the window is viewed points the frame with the viewer argument; one that
never comes is given up on after a grace, and the pty attached plainly. Opened in a tab of its own, the wrapper
points at once.
"""

from typing import Final

import pytest
from playwright.sync_api import BrowserContext, Frame, Page, Route
from terminal_app.primitives import VIEWER_ARGUMENT
from terminal_app.testing import (
    BROWSER_PTY_URL,
    BROWSER_WRAPPER_HOST,
    BROWSER_WRAPPER_URL,
    render_browser_wrapper_page,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_SHELL_URL: Final[str] = "http://shell.test/"
_CONTRACT_URL: Final[str] = f"http://{BROWSER_WRAPPER_HOST}/_static/app_contract.js"
_VIEWER_PTY_URL: Final[str] = f"{BROWSER_PTY_URL}&arg={VIEWER_ARGUMENT}"

_SHELL_PAGE: Final[str] = f"""<!doctype html><html><body>
<iframe id="window" src="{BROWSER_WRAPPER_URL}" style="width:600px;height:400px"></iframe>
<script>
// The wrapper loads the contract module asynchronously once framed, so the greeting is sent until it lands.
let sending = null;
window.greet = (isViewing) => {{
  clearInterval(sending);
  const send = () =>
    document.getElementById("window").contentWindow.postMessage(
      {{ type: "shell:hello", clientId: "shell-client", isViewing }}, "*");
  send();
  sending = setInterval(send, 100);
}};
window.stopGreeting = () => clearInterval(sending);
</script></body></html>"""

# The contract's shape the wrapper uses: the shell's greeting handed to ``onHandshake`` as it arrives.
_CONTRACT_MODULE: Final[str] = """export function connectToShell(handlers) {
  addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.type !== "shell:hello") return;
    handlers.onHandshake?.({ clientId: event.data.clientId, isViewing: event.data.isViewing === true });
  });
  return { location() {}, focused() {} };
}"""

_PTY_PAGE: Final[str] = "<!doctype html><html><body></body></html>"

# What the wrapper's pty frame points at: its src, empty while held.
_PTY_FRAME_SRC: Final[str] = "document.getElementById('pty').src"


def _serve(context: BrowserContext) -> None:
    def answer(route: Route) -> None:
        url = route.request.url
        if url == _SHELL_URL:
            route.fulfill(status=200, content_type="text/html", body=_SHELL_PAGE)
        elif url == BROWSER_WRAPPER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=render_browser_wrapper_page()
            )
        elif url == _CONTRACT_URL:
            route.fulfill(
                status=200, content_type="text/javascript", body=_CONTRACT_MODULE
            )
        elif url in (BROWSER_PTY_URL, _VIEWER_PTY_URL):
            route.fulfill(status=200, content_type="text/html", body=_PTY_PAGE)
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", answer)


def _framed_wrapper(context: BrowserContext) -> tuple[Page, Frame]:
    _serve(context)
    page = context.new_page()
    page.goto(_SHELL_URL)
    wrapper = page.frame(url=BROWSER_WRAPPER_URL)
    assert wrapper is not None
    wrapper.wait_for_load_state()
    return page, wrapper


def test_a_framed_wrapper_points_its_pty_at_nothing_until_the_shell_greets_it(
    phone_context: BrowserContext,
) -> None:
    page, wrapper = _framed_wrapper(phone_context)
    # The contract module is up once the wrapper has loaded, and the shell has not greeted it.
    wrapper.wait_for_function(
        "document.getElementById('empty').hidden === true", timeout=5000
    )
    assert wrapper.evaluate(_PTY_FRAME_SRC) == ""

    page.main_frame.evaluate("greet(true)")
    wrapper.wait_for_function(f"{_PTY_FRAME_SRC} !== ''", timeout=5000)
    page.main_frame.evaluate("stopGreeting()")

    assert wrapper.evaluate(_PTY_FRAME_SRC) == _VIEWER_PTY_URL


def test_a_greeting_that_says_the_window_is_not_viewed_points_the_held_pty_plainly(
    phone_context: BrowserContext,
) -> None:
    page, wrapper = _framed_wrapper(phone_context)

    page.main_frame.evaluate("greet(false)")
    wrapper.wait_for_function(f"{_PTY_FRAME_SRC} !== ''", timeout=5000)
    page.main_frame.evaluate("stopGreeting()")

    assert wrapper.evaluate(_PTY_FRAME_SRC) == BROWSER_PTY_URL


def test_a_framed_wrapper_nobody_greets_attaches_plainly_after_the_grace(
    phone_context: BrowserContext,
) -> None:
    _page, wrapper = _framed_wrapper(phone_context)

    wrapper.wait_for_function(f"{_PTY_FRAME_SRC} !== ''", timeout=10000)

    assert wrapper.evaluate(_PTY_FRAME_SRC) == BROWSER_PTY_URL


def test_a_wrapper_in_a_tab_of_its_own_points_its_pty_at_once(
    phone_context: BrowserContext,
) -> None:
    _serve(phone_context)
    page = phone_context.new_page()
    page.goto(BROWSER_WRAPPER_URL)

    assert page.evaluate(_PTY_FRAME_SRC) == BROWSER_PTY_URL
