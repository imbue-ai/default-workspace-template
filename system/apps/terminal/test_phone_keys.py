"""Browser: the wrapper page's phone key strip, and the script dwt adds to ttyd's page, in a real Chromium.

The wrapper and the pty page are served on two origins through request routing (the wrapper derives the pty's
origin from its own host, so ``http://terminal.test`` frames ``http://pty.terminal.test``). The pty page is a
stub that records what it is sent, a stub ``window.term`` under the added script, or the real patched ttyd client
the imbue-mngr-ttyd package ships with the script installed into it.
"""

import gzip
from collections.abc import Callable
from typing import Final

import pytest
from playwright.sync_api import BrowserContext, Frame, Locator, Page, Route, expect
from terminal_app.dispatch import load_ttyd_web_client
from terminal_app.pages import PageConfig, SessionPage, render_page
from terminal_app.primitives import TerminalTitle, TmuxSessionName
from terminal_app.pty_page import add_pty_page_script

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_WRAPPER_HOST: Final[str] = "terminal.test"
_PTY_LABEL: Final[str] = "pty"
_PTY_PATH: Final[str] = "/?arg=_&arg=session&arg=terminal-1"
_WRAPPER_URL: Final[str] = f"http://{_WRAPPER_HOST}/?session=terminal-1"
_BARE_WRAPPER_URL: Final[str] = f"http://{_WRAPPER_HOST}/"
_PTY_URL: Final[str] = f"http://{_PTY_LABEL}.{_WRAPPER_HOST}{_PTY_PATH}"

# Records every message its parent posts and every resize it sees.
_RECORDING_PTY_PAGE: Final[str] = """<!doctype html>
<html><body style="margin:0"><textarea id="input"></textarea><script>
window.received = [];
window.resizes = 0;
addEventListener("message", (event) => { if (event.source === parent) received.push(event.data); });
addEventListener("resize", () => { resizes += 1; });
</script></body></html>"""

# A stand-in for the patched client's ``window.term``, recording what the added script calls on it.
_STUB_TERM_PTY_PAGE: Final[str] = """<!doctype html>
<html><body style="margin:0;height:100vh"><script>
window.inputs = [];
window.focuses = 0;
window.term = {
  modes: { applicationCursorKeysMode: false },
  input(data, wasUserInput) { inputs.push([data, wasUserInput]); },
  focus() { focuses += 1; },
};
</script></body></html>"""


def _wrapper_html() -> str:
    session = TmuxSessionName("terminal-1")
    page = SessionPage(
        name=session,
        title=TerminalTitle("Terminal 1"),
        pty_path=_PTY_PATH,
        pty_label=_PTY_LABEL,
    )
    return render_page(PageConfig(session=session, page=page))


def _bare_wrapper_html() -> str:
    return render_page(PageConfig(session=None, page=None))


def _real_client_html() -> bytes:
    compressed_client = load_ttyd_web_client(None)
    assert compressed_client is not None
    with_script = add_pty_page_script(gzip.decompress(compressed_client))
    assert with_script is not None
    return with_script


def _serve(context: BrowserContext, pty_page: str | bytes) -> None:
    """Answer the wrapper's host with the wrapper page, the pty page's URL with ``pty_page``, and anything else
    (ttyd's token, a favicon) with a 404; ttyd's websocket finds no server and keeps retrying."""

    def answer(route: Route) -> None:
        url = route.request.url
        if url == _WRAPPER_URL:
            route.fulfill(status=200, content_type="text/html", body=_wrapper_html())
        elif url == _BARE_WRAPPER_URL:
            route.fulfill(
                status=200, content_type="text/html", body=_bare_wrapper_html()
            )
        elif url == _PTY_URL:
            route.fulfill(status=200, content_type="text/html", body=pty_page)
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", answer)


def _open(context: BrowserContext, pty_page: str | bytes) -> tuple[Page, Frame]:
    _serve(context, pty_page)
    page = context.new_page()
    page.goto(_WRAPPER_URL)
    frame = page.frame(url=_PTY_URL)
    assert frame is not None
    frame.wait_for_load_state()
    return page, frame


def _key(page: Page, name: str) -> Locator:
    return page.get_by_role("toolbar", name="Terminal keys").get_by_role(
        "button", name=name, exact=True
    )


def _wait_for(frame: Frame, expression: str) -> None:
    frame.wait_for_function(expression, timeout=5000)


def _terminal_messages(frame: Frame) -> list[dict[str, object]]:
    return frame.evaluate("received.filter((message) => message.type !== undefined)")


def test_the_strip_posts_each_key_into_the_pty_frame_and_hands_focus_back(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open(phone_context, _RECORDING_PTY_PAGE)

    for name in ["Esc", "Tab", "Left", "Up", "Down", "Right"]:
        _key(page, name).tap()
    _wait_for(frame, "received.length >= 12")

    expected: list[dict[str, object]] = []
    for key in ["Escape", "Tab", "ArrowLeft", "ArrowUp", "ArrowDown", "ArrowRight"]:
        expected += [
            {"type": "terminal:key", "key": key, "ctrl": False},
            {"type": "ttyd-focus"},
        ]
    assert _terminal_messages(frame) == expected


def test_ctrl_is_one_shot_and_releases_when_the_pty_page_used_it(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open(phone_context, _RECORDING_PTY_PAGE)
    ctrl = _key(page, "Ctrl")

    ctrl.tap()
    expect(ctrl).to_have_attribute("aria-pressed", "true")
    _key(page, "Up").tap()
    expect(ctrl).to_have_attribute("aria-pressed", "false")
    _key(page, "Up").tap()
    _wait_for(frame, "received.length >= 6")
    assert _terminal_messages(frame) == [
        {"type": "terminal:ctrl", "armed": True},
        {"type": "ttyd-focus"},
        {"type": "terminal:key", "key": "ArrowUp", "ctrl": True},
        {"type": "ttyd-focus"},
        {"type": "terminal:key", "key": "ArrowUp", "ctrl": False},
        {"type": "ttyd-focus"},
    ]

    # Armed again, then the pty page reports that a typed key took the Ctrl.
    ctrl.tap()
    expect(ctrl).to_have_attribute("aria-pressed", "true")
    frame.evaluate('parent.postMessage({ type: "terminal:ctrl", armed: false }, "*")')
    expect(ctrl).to_have_attribute("aria-pressed", "false")


def test_a_key_press_leaves_focus_in_the_terminal(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open(phone_context, _RECORDING_PTY_PAGE)
    frame.locator("#input").focus()

    _key(page, "Esc").tap()
    _key(page, "Ctrl").tap()

    assert page.evaluate("document.activeElement.id") == "pty"
    assert frame.evaluate("document.activeElement.id") == "input"


def test_the_strip_shows_only_on_a_phone_sized_page_that_frames_a_terminal(
    phone_context: BrowserContext,
) -> None:
    page, _frame = _open(phone_context, _RECORDING_PTY_PAGE)
    strip = page.locator("#keys")
    expect(strip).to_be_visible()
    frame_box = page.locator("#pty").bounding_box()
    strip_box = strip.bounding_box()
    viewport = page.viewport_size
    assert frame_box is not None and strip_box is not None and viewport is not None
    # The strip sits under the terminal at the bottom of the page, and the terminal gives it the room.
    assert frame_box["y"] + frame_box["height"] == pytest.approx(strip_box["y"])
    assert strip_box["y"] + strip_box["height"] == pytest.approx(viewport["height"])

    # A phone on its side is still a phone: the shorter side decides, as the shell's own layout does.
    page.set_viewport_size({"width": 852, "height": 393})
    expect(strip).to_be_visible()
    page.set_viewport_size({"width": 1024, "height": 768})
    expect(strip).to_be_hidden()
    assert page.locator("#pty").bounding_box() == {
        "x": 0,
        "y": 0,
        "width": 1024,
        "height": 768,
    }

    page.set_viewport_size(viewport)
    page.goto(_BARE_WRAPPER_URL)
    expect(page.get_by_text("Open a terminal from the launcher.")).to_be_visible()
    expect(strip).to_be_hidden()


def test_the_frame_is_nudged_after_it_loads_and_after_the_visual_viewport_resizes(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open(phone_context, _RECORDING_PTY_PAGE)
    # The load nudges land at 500ms and 2s; each shrinks the frame a pixel and restores it.
    _wait_for(frame, "resizes >= 4")
    settled = frame.evaluate("resizes")
    height = frame.evaluate("innerHeight")

    page.evaluate('visualViewport.dispatchEvent(new Event("resize"))')

    _wait_for(frame, f"resizes >= {settled + 2}")
    assert frame.evaluate("innerHeight") == height


def _open_with_stub_term(context: BrowserContext) -> tuple[Page, Frame]:
    with_script = add_pty_page_script(_STUB_TERM_PTY_PAGE.encode())
    assert with_script is not None
    return _open(context, with_script)


def test_the_pty_page_script_turns_strip_keys_into_the_sequences_xterm_sends(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open_with_stub_term(phone_context)

    for name in ["Esc", "Tab", "Up", "Down", "Right", "Left"]:
        _key(page, name).tap()
    _wait_for(frame, "inputs.length >= 6")
    frame.evaluate("term.modes.applicationCursorKeysMode = true")
    _key(page, "Up").tap()
    _key(page, "Ctrl").tap()
    _key(page, "Left").tap()
    _wait_for(frame, "inputs.length >= 8")

    assert frame.evaluate("inputs") == [
        ["\x1b", True],
        ["\t", True],
        ["\x1b[A", True],
        ["\x1b[B", True],
        ["\x1b[C", True],
        ["\x1b[D", True],
        ["\x1bOA", True],
        ["\x1b[1;5D", True],
    ]


def _touch(page: Page) -> Callable[[str, list[tuple[int, int]]], None]:
    session = page.context.new_cdp_session(page)

    def dispatch(kind: str, points: list[tuple[int, int]]) -> None:
        session.send(
            "Input.dispatchTouchEvent",
            {"type": kind, "touchPoints": [{"x": x, "y": y} for x, y in points]},
        )

    return dispatch


def test_a_tap_on_the_terminal_focuses_it_and_a_swipe_does_not(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open_with_stub_term(phone_context)
    touch = _touch(page)

    touch("touchStart", [(150, 300)])
    touch("touchMove", [(150, 200)])
    touch("touchEnd", [])
    touch("touchStart", [(150, 300)])
    touch("touchEnd", [])

    # Touches arrive in order, so had the swipe focused too, the tap would make it two.
    _wait_for(frame, "focuses >= 1")
    assert frame.evaluate("focuses") == 1


def _open_with_real_client(context: BrowserContext) -> tuple[Page, Frame]:
    page, frame = _open(context, _real_client_html())
    _wait_for(frame, "window.term !== undefined && window.term.element !== undefined")
    # Listeners run in the order they were added, so a Ctrl message counted here was already handled by the script.
    frame.evaluate("""
      window.sent = [];
      window.ctrlMessages = 0;
      term.onData((data) => sent.push(data));
      addEventListener("message", (event) => { if (event.data?.type === "terminal:ctrl") ctrlMessages += 1; });
    """)
    return page, frame


def test_the_real_ttyd_client_sends_the_strip_keys_to_the_pty(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open_with_real_client(phone_context)

    _key(page, "Esc").tap()
    _key(page, "Up").tap()
    frame.evaluate('new Promise((resolve) => term.write("\\x1b[?1h", resolve))')
    _key(page, "Up").tap()
    _key(page, "Ctrl").tap()
    _key(page, "Right").tap()
    _wait_for(frame, "sent.length >= 4")

    assert frame.evaluate("sent") == ["\x1b", "\x1b[A", "\x1bOA", "\x1b[1;5C"]


def test_an_armed_ctrl_applies_to_the_next_key_typed_into_the_real_client(
    phone_context: BrowserContext,
) -> None:
    page, frame = _open_with_real_client(phone_context)
    ctrl = _key(page, "Ctrl")
    textarea = frame.locator(".xterm-helper-textarea")

    ctrl.tap()
    _wait_for(frame, "ctrlMessages === 1")
    textarea.press("c")
    expect(ctrl).to_have_attribute("aria-pressed", "false")
    textarea.press("d")
    # A soft keyboard's text can arrive as an input event rather than a keydown.
    ctrl.tap()
    _wait_for(frame, "ctrlMessages === 2")
    textarea.evaluate(
        '(area) => area.dispatchEvent(new InputEvent("input", { data: "x", inputType: "insertText" }))'
    )
    expect(ctrl).to_have_attribute("aria-pressed", "false")
    _wait_for(frame, "sent.length >= 3")

    assert frame.evaluate("sent") == ["\x03", "d", "\x18"]
