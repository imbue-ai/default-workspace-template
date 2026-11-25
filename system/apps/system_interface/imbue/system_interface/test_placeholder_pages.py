"""The pages the shell serves in place of a page it cannot show, driven in a real browser: a stopped app's
could-not-start page asking the shell that frames it for an agent. None needs the built frontend: each is a string the
backend serves."""

import pytest
from playwright.sync_api import FrameLocator
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.port_parking import ParkedPort
from imbue.system_interface.shell.port_parking import ParkingTarget
from imbue.system_interface.shell.port_parking import could_not_start_request
from imbue.system_interface.shell.port_parking import parked_page_html
from imbue.system_interface.testing import is_e2e_browser_installed

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
]

# The stand-in shell's greeting to the page it frames, called with the shell's id.
_GREET_FRAMED_PAGE: str = (
    "(shellId) => document.getElementById('app').contentWindow.postMessage("
    "{ type: 'shell:handshake', windowId: 'w', shellId, canReportBug: true }, '*')"
)
# The stand-in shell's answer to an ask, called with the answer's fields.
_ANSWER_FRAMED_PAGE: str = (
    "(answer) => document.getElementById('app').contentWindow.postMessage("
    "Object.assign({ type: 'shell:ask-agent-result' }, answer), '*')"
)


def _parked_page(kind: ParkedPageKind, port: int) -> ParkedPort:
    return ParkedPort(
        app="docs",
        target=ParkingTarget(host="127.0.0.1", port=port),
        on_first_connection=lambda: kind,
        page_for=lambda answered: parked_page_html(answered, "Docs", "docs", None),
    )


def _framed_by_a_stand_in_shell(page: Page, port: int) -> FrameLocator:
    """The page parked at ``port`` in a frame of a stand-in for the shell, which keeps what the page posts."""
    page.set_content(
        "<script>window.received = [];"
        "window.addEventListener('message', (event) => window.received.push(event.data));</script>"
        f"<iframe id='app' src='http://127.0.0.1:{port}/'></iframe>"
    )
    return page.frame_locator("#app")


def test_a_could_not_start_page_asks_the_shell_that_frames_it_for_an_agent(closed_port: int, page: Page) -> None:
    parked = _parked_page(ParkedPageKind.FAILED, closed_port)
    parked.start()
    try:
        framed = _framed_by_a_stand_in_shell(page, closed_port)
        expect(framed.get_by_role("heading")).to_have_text("Docs couldn't start.")
        expect(framed.get_by_role("button", name="Try again", exact=True)).to_be_visible()
        # With no error log to show, "See details" waits for something to hold.
        expect(framed.get_by_text("See details", exact=True)).to_be_hidden()
        # Only a shell can ask an agent, so the button waits for its greeting.
        ask = framed.locator("#ask-agent")
        expect(ask).to_have_text("Ask an agent to look at it")
        expect(ask).to_be_hidden()
        page.evaluate(_GREET_FRAMED_PAGE, "shell-1")
        ask.click()
        page.wait_for_function("window.received.length > 0")
        (asked,) = page.evaluate("window.received")
        assert asked["type"] == "shell:ask-agent"
        assert asked["text"] == could_not_start_request("Docs", "docs")
        # The page shows the answer itself, so the shell is told not to toast it too.
        assert asked["showsOutcome"] is True
        # The button says the ask is out and takes no second press until the shell answers this request.
        expect(ask).to_have_text("Asking an agent…")
        expect(ask).to_be_disabled()
        page.evaluate(_ANSWER_FRAMED_PAGE, {"requestId": "some-other-request", "isOpened": True})
        expect(ask).to_be_disabled()
        page.evaluate(
            _ANSWER_FRAMED_PAGE,
            {
                "requestId": asked["requestId"],
                "isOpened": False,
                "failure": {"message": "Chat couldn't open that.", "details": "HTTP 500\nboom", "isHelpOwed": True},
            },
        )
        expect(ask).to_have_text("Ask an agent to look at it")
        expect(ask).to_be_enabled()
        expect(framed.locator("#ask-agent-failure")).to_have_text("Chat couldn't open that.")
        framed.get_by_text("See details", exact=True).click()
        expect(framed.locator("#ask-agent-failure-details")).to_have_text("HTTP 500\nboom")
        # The refused ask owes help, so the page offers the same request as a draft for the person to send.
        draft = framed.get_by_role("button", name="Draft it in a chat")
        expect(draft).to_be_visible()
        draft.click()
        page.wait_for_function("window.received.some((message) => message.delivery === 'draft')")
        (drafted,) = page.evaluate("window.received.filter((message) => message.delivery === 'draft')")
        assert drafted["type"] == "shell:ask-agent" and drafted["text"] == asked["text"]
        assert drafted["showsOutcome"] is True
        # The shell can open a bug report, so the page offers one beside the draft, carrying the failure.
        report = framed.get_by_role("button", name="Report a bug")
        expect(report).to_be_visible()
        report.click()
        page.wait_for_function("window.received.some((message) => message.type === 'shell:report-bug')")
        reported = page.evaluate("window.received.filter((message) => message.type === 'shell:report-bug')")
        assert reported == [
            {"type": "shell:report-bug", "message": "Chat couldn't open that.", "details": "HTTP 500\nboom"}
        ]
        # The shell greeting the page again to say it can no longer open one takes the offer away.
        page.evaluate(
            "document.getElementById('app').contentWindow.postMessage("
            "{ type: 'shell:handshake', windowId: 'w', shellId: 'shell-1', canReportBug: false }, '*')"
        )
        expect(report).to_be_hidden()
        page.evaluate(_GREET_FRAMED_PAGE, "shell-1")
        expect(report).to_be_visible()

        # The same shell greeting the page again (a window moved, Imbue Studio said it opens help) leaves the ask out.
        ask.click()
        expect(framed.locator("#ask-agent-failure")).to_be_hidden()
        expect(report).to_be_hidden()
        expect(draft).to_be_hidden()
        asks = (
            "window.received.filter((message) => message.type === 'shell:ask-agent' && message.delivery !== 'draft')"
        )
        page.wait_for_function(f"{asks}.length === 2")
        second = page.evaluate(f"{asks}[1]")
        page.evaluate(_GREET_FRAMED_PAGE, "shell-1")
        page.wait_for_timeout(300)
        expect(ask).to_be_disabled()
        expect(ask).to_have_text("Asking an agent…")
        # A greeting from a restarted shell settles it as not answered; the old shell's answer, if it still comes,
        # replaces that.
        page.evaluate(_GREET_FRAMED_PAGE, "shell-2")
        expect(ask).to_be_enabled()
        expect(framed.locator("#ask-agent-failure")).to_have_text(
            "The workspace didn't answer. This may not have gone through."
        )
        # Not answered owes no help: trying again is the whole remedy.
        expect(report).to_be_hidden()
        expect(draft).to_be_hidden()
        page.evaluate(_ANSWER_FRAMED_PAGE, {"requestId": second["requestId"], "isOpened": True})
        expect(framed.locator("#ask-agent-failure")).to_be_hidden()
    finally:
        parked.release()


def test_a_page_whose_wake_budget_is_spent_offers_the_agent_and_no_try_again(closed_port: int, page: Page) -> None:
    """A press cannot start an app whose wake budget is spent, so the page keeps everything but Try again."""
    parked = _parked_page(ParkedPageKind.BUDGET_SPENT, closed_port)
    parked.start()
    try:
        framed = _framed_by_a_stand_in_shell(page, closed_port)
        expect(framed.get_by_role("heading")).to_have_text("Docs couldn't start.")
        expect(
            framed.get_by_text(
                "Docs failed to start several times in the last few minutes, so it wasn't started again. "
                "Try again in a few minutes.",
                exact=True,
            )
        ).to_be_visible()
        expect(framed.get_by_role("button", name="Try again", exact=True)).to_have_count(0)

        ask = framed.get_by_role("button", name="Ask an agent to look at it")
        expect(ask).to_be_hidden()
        page.evaluate(_GREET_FRAMED_PAGE, "shell-1")
        ask.click()
        page.wait_for_function("window.received.length > 0")
        (asked,) = page.evaluate("window.received")
        assert asked["type"] == "shell:ask-agent"
        assert asked["text"] == could_not_start_request("Docs", "docs")
        page.evaluate(
            _ANSWER_FRAMED_PAGE,
            {
                "requestId": asked["requestId"],
                "isOpened": False,
                "failure": {"message": "Chat couldn't open that.", "details": "HTTP 500\nboom", "isHelpOwed": True},
            },
        )
        expect(framed.locator("#ask-agent-failure")).to_have_text("Chat couldn't open that.")
        expect(framed.get_by_role("button", name="Report a bug")).to_be_visible()
        framed.get_by_text("See details", exact=True).click()
        expect(framed.locator("#ask-agent-failure-details")).to_have_text("HTTP 500\nboom")
    finally:
        parked.release()
