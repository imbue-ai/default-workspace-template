"""Browser: the workspace theme's palette reaches xterm (docs/system/blueprint/workspace-themes/, section 4.1).

A stand-in shell page frames the wrapper and posts ``shell:theme`` as the shell does; the wrapper loads the theme's
bundle from its own origin into an unseen frame, reads the ``--term-*`` tokens there, and posts them into the pty
frame, where the script dwt adds to ttyd's page sets them on a stub ``window.term``. The contract module the wrapper imports is a stand-in that
hands the page the shell's theme message, since the shell's built module is the shell's to test.
"""

from typing import Final

import pytest
from playwright.sync_api import BrowserContext, Frame, Route
from terminal_app.pty_page import add_pty_page_script
from terminal_app.testing import (
    BROWSER_PTY_URL,
    BROWSER_WRAPPER_HOST,
    BROWSER_WRAPPER_URL,
    render_browser_wrapper_page,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_SHELL_URL: Final[str] = "http://shell.test/"
_CONTRACT_URL: Final[str] = f"http://{BROWSER_WRAPPER_HOST}/_static/app_contract.js"
_BUNDLE_URL_PREFIX: Final[str] = (
    f"http://{BROWSER_WRAPPER_HOST}/_static/themes/paper/theme.css"
)

_SHELL_PAGE: Final[str] = f"""<!doctype html><html><body>
<iframe id="window" src="{BROWSER_WRAPPER_URL}" style="width:600px;height:400px"></iframe>
<script>
// The wrapper loads the contract module asynchronously once framed, so the message is sent until it lands.
let sending = null;
window.sendTheme = (theme, revision) => {{
  clearInterval(sending);
  const send = () =>
    document.getElementById("window").contentWindow.postMessage({{ type: "shell:theme", theme, revision }}, "*");
  send();
  sending = setInterval(send, 100);
}};
window.stopSending = () => clearInterval(sending);
</script></body></html>"""

# The contract's shape the wrapper uses: ``connectToShell`` with its handlers, here only ``onTheme`` wired.
_CONTRACT_MODULE: Final[str] = """export function connectToShell(handlers) {
  addEventListener("message", (event) => {
    if (event.source !== parent || event.data?.type !== "shell:theme") return;
    handlers.onTheme?.(event.data.theme, event.data.revision ?? "");
  });
  return { location() {}, focused() {} };
}"""

# A retro theme's bundle: its palette, and element rules that would restyle the wrapper if it wore the bundle.
_THEME_BUNDLE: Final[str] = """:root {
  --term-background: #000080;
  --term-foreground: #c0c0c0;
  --term-ansi-1: #800000;
  --term-ansi-9: #ff0000;
}
body { background: #c0c0c0; color: #000000; font-family: Chicago, serif; }
button { border-radius: 0; background: #ffffff; }"""

_WRAPPER_BODY_STYLE: Final[str] = """(() => {
  const style = getComputedStyle(document.body);
  return { background: style.backgroundColor, color: style.color, font: style.fontFamily };
})()"""

_STUB_TERM_PTY_PAGE: Final[str] = """<!doctype html>
<html><body style="margin:0;height:100vh"><script>
window.term = { options: { theme: { background: "#111111" } }, modes: {}, input() {}, focus() {} };
</script></body></html>"""


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
        elif url.startswith(_BUNDLE_URL_PREFIX):
            route.fulfill(status=200, content_type="text/css", body=_THEME_BUNDLE)
        elif url == BROWSER_PTY_URL:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=add_pty_page_script(_STUB_TERM_PTY_PAGE.encode()),
            )
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", answer)


def _pty_frame(context: BrowserContext) -> tuple[Frame, Frame]:
    _serve(context)
    page = context.new_page()
    page.goto(_SHELL_URL)
    wrapper = page.frame(url=BROWSER_WRAPPER_URL)
    assert wrapper is not None
    wrapper.wait_for_load_state()
    pty = page.frame(url=BROWSER_PTY_URL)
    assert pty is not None
    pty.wait_for_load_state()
    return page.main_frame, pty


def test_a_theme_s_palette_is_set_on_xterm_over_xterm_s_own_colors(
    phone_context: BrowserContext,
) -> None:
    shell, pty = _pty_frame(phone_context)

    shell.evaluate("sendTheme('paper', 'r1')")
    pty.wait_for_function("term.options.theme.foreground === '#c0c0c0'", timeout=5000)
    shell.evaluate("stopSending()")

    assert pty.evaluate("term.options.theme") == {
        "background": "#000080",
        "foreground": "#c0c0c0",
        "red": "#800000",
        "brightRed": "#ff0000",
    }


def test_a_theme_s_rules_leave_the_wrapper_page_as_it_is(
    phone_context: BrowserContext,
) -> None:
    shell, pty = _pty_frame(phone_context)
    wrapper = shell.page.frame(url=BROWSER_WRAPPER_URL)
    assert wrapper is not None
    unthemed = wrapper.evaluate(_WRAPPER_BODY_STYLE)

    shell.evaluate("sendTheme('paper', 'r1')")
    pty.wait_for_function("term.options.theme.foreground === '#c0c0c0'", timeout=5000)
    shell.evaluate("stopSending()")

    assert unthemed == {
        "background": "rgb(0, 0, 0)",
        "color": "rgb(221, 221, 221)",
        "font": "system-ui, sans-serif",
    }
    assert wrapper.evaluate(_WRAPPER_BODY_STYLE) == unthemed


def test_the_standard_theme_puts_back_xterm_s_own_colors(
    phone_context: BrowserContext,
) -> None:
    shell, pty = _pty_frame(phone_context)

    shell.evaluate("sendTheme('paper', 'r1')")
    pty.wait_for_function("term.options.theme.foreground === '#c0c0c0'", timeout=5000)
    shell.evaluate("sendTheme('standard', '')")
    pty.wait_for_function("term.options.theme.foreground === undefined", timeout=5000)
    shell.evaluate("stopSending()")

    assert pty.evaluate("term.options.theme") == {"background": "#111111"}


def test_a_theme_that_cannot_be_loaded_puts_back_xterm_s_own_colors(
    phone_context: BrowserContext,
) -> None:
    shell, pty = _pty_frame(phone_context)

    shell.evaluate("sendTheme('paper', 'r1')")
    pty.wait_for_function("term.options.theme.foreground === '#c0c0c0'", timeout=5000)
    # The shell serves no bundle for this theme, so its stylesheet fails to load.
    shell.evaluate("sendTheme('gone', 'r2')")
    pty.wait_for_function("term.options.theme.foreground === undefined", timeout=5000)
    shell.evaluate("stopSending()")

    assert pty.evaluate("term.options.theme") == {"background": "#111111"}
