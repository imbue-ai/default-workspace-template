"""The pages the shell serves in place of a page it cannot show, driven in a real browser: the not-built placeholder
asking an agent to repair the interface and opening a terminal, and a stopped app's could-not-start page asking the
shell that frames it for an agent. Neither needs the built frontend: both are strings the backend serves."""

from pathlib import Path

import pytest
from playwright.sync_api import FrameLocator
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect
from workspace_layout.identity import ANONYMOUS_OWNER

from imbue.system_interface.server import _NOT_BUILT_REPAIR_MESSAGE
from imbue.system_interface.shell.launches import LaunchPostOutcome
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.port_parking import ParkedPort
from imbue.system_interface.shell.port_parking import ParkingTarget
from imbue.system_interface.shell.port_parking import could_not_start_request
from imbue.system_interface.shell.port_parking import parked_page_html
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import not_built_shell
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.testing import serve_app

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
]

# A workspace host: the placeholder derives an app's origin from the coordinate label in it (``*.localhost`` resolves
# to loopback in Chromium).
_COORDINATE: str = "host-abc123.localhost"

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
        page_for=lambda answered: parked_page_html(answered, "Docs", "docs"),
    )


def _framed_by_a_stand_in_shell(page: Page, port: int) -> FrameLocator:
    """The page parked at ``port`` in a frame of a stand-in for the shell, which keeps what the page posts."""
    page.set_content(
        "<script>window.received = [];"
        "window.addEventListener('message', (event) => window.received.push(event.data));</script>"
        f"<iframe id='app' src='http://127.0.0.1:{port}/'></iframe>"
    )
    return page.frame_locator("#app")


def test_the_not_built_page_asks_an_agent_and_frames_the_main_chat_and_a_terminal_session(
    tmp_path: Path, page: Page
) -> None:
    outcomes = [
        LaunchPostOutcome(status_code=500, body={"detail": "boom"}),
        LaunchPostOutcome(status_code=200, body={"path": "/?chat=agent-1"}),
    ]
    app, posts = not_built_shell(tmp_path, outcomes)
    with serve_app(app) as served:
        # The workspace's front door stamps every request it forwards with who sent it; a request under the
        # coordinate's host that carries none is refused.
        page.set_extra_http_headers(identity_headers(ANONYMOUS_OWNER))
        terminal_launches: list[str] = []
        page.on(
            "request",
            lambda request: terminal_launches.append(request.url)
            if request.method == "POST" and '"terminal"' in (request.post_data or "")
            else None,
        )
        page.goto(f"http://shell-z.{_COORDINATE}:{served.port}/")
        # The command and the terminal wait behind "See details"; the agent is the way out on the page.
        expect(page.locator("#repair-command")).not_to_be_visible()
        ask = page.locator("#ask-agent-button")
        expect(ask).to_have_text("Ask an agent to fix this")

        # While the launch is out the button says so and takes no second press; it comes back with the failure.
        held: list[Route] = []
        page.route("**/api/not-built/launch", lambda route: held.append(route))
        ask.click()
        expect(ask).to_have_text("Asking an agent…")
        expect(ask).to_be_disabled()
        expect(page.locator("#ask-agent-failure-message")).to_be_hidden()
        # The sync API hands the route over only while it is called.
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)
        (launch,) = held
        launch.continue_()
        page.unroute("**/api/not-built/launch")
        expect(ask).to_have_text("Ask an agent to fix this")
        expect(ask).to_be_enabled()
        expect(page.locator("#ask-agent-failure-message")).to_have_text(
            "Couldn't ask an agent. Try again. If this persists, report a bug."
        )
        # The failure's reason is in the page's one "See details", at its top.
        see_details = page.get_by_text("See details", exact=True)
        expect(see_details).to_have_count(1)
        expect(page.locator("#ask-agent-failure-details")).not_to_be_visible()
        see_details.click()
        expect(page.locator("#ask-agent-failure-reason")).to_contain_text("Why the agent couldn't be asked:")
        expect(page.locator("#ask-agent-failure-details")).to_contain_text("HTTP 502")
        expect(page.locator("#ask-agent-failure-details")).to_contain_text("boom")
        # Reading the reason opens no terminal session: that waits for the terminal's own section.
        page.wait_for_timeout(500)
        assert terminal_launches == []
        see_details.click()

        ask.click()
        expect(page.locator("#agent-started")).to_have_text("An agent is on it. Follow along below.")
        expect(page.locator("#agent-chat")).to_have_attribute(
            "src", f"http://helper-y2.{_COORDINATE}:{served.port}/?chat=agent-1"
        )
        expect(ask).to_be_hidden()
        expect(page.locator("#ask-agent-failure-reason")).to_be_hidden()
        assert [post.body for post in posts] == [{"target": "main_chat", "message": _NOT_BUILT_REPAIR_MESSAGE}] * 2

        page.get_by_text("See details", exact=True).click()
        expect(page.locator("#repair-command")).to_be_visible()
        assert terminal_launches == []
        page.get_by_text("Open this workspace's terminal", exact=True).click()
        expect(page.locator("#terminal")).to_have_attribute(
            "src", f"http://terminal-x1.{_COORDINATE}:{served.port}/new"
        )
        expect(page.locator("#no-terminal")).to_be_hidden()
        assert len(terminal_launches) == 1


def test_a_could_not_start_page_asks_the_shell_that_frames_it_for_an_agent(closed_port: int, page: Page) -> None:
    parked = _parked_page(ParkedPageKind.FAILED, closed_port)
    parked.start()
    try:
        framed = _framed_by_a_stand_in_shell(page, closed_port)
        expect(framed.get_by_role("heading")).to_have_text("Docs couldn't start.")
        expect(framed.get_by_role("button", name="Try again", exact=True)).to_be_visible()
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
                "failure": {"message": "Chat couldn't open that.", "details": "HTTP 500\nboom", "isReportable": True},
            },
        )
        expect(ask).to_have_text("Ask an agent to look at it")
        expect(ask).to_be_enabled()
        expect(framed.locator("#ask-agent-failure")).to_have_text("Chat couldn't open that.")
        framed.get_by_text("See details", exact=True).click()
        expect(framed.locator("#ask-agent-failure-details")).to_have_text("HTTP 500\nboom")
        # The failure asks for a bug report and the shell can open one, so the page offers it, carrying the failure.
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
        asks = "window.received.filter((message) => message.type === 'shell:ask-agent')"
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
            "The workspace didn't answer, so Imbue Studio can't tell whether this went through."
        )
        # Not answered asks for no bug report.
        expect(report).to_be_hidden()
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
                "failure": {"message": "Chat couldn't open that.", "details": "HTTP 500\nboom", "isReportable": True},
            },
        )
        expect(framed.locator("#ask-agent-failure")).to_have_text("Chat couldn't open that.")
        expect(framed.get_by_role("button", name="Report a bug")).to_be_visible()
        framed.get_by_text("See details", exact=True).click()
        expect(framed.locator("#ask-agent-failure-details")).to_have_text("HTTP 500\nboom")
        expect(framed.get_by_text("supervisorctl tail docs stderr")).to_be_visible()
    finally:
        parked.release()
