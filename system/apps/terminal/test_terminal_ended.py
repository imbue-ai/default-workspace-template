"""Browser: what a terminal window says when its terminal ended for a reason, or its page could not be served, in a
real Chromium.

The wrapper page frames the real patched ttyd client with the script dwt adds to it; with no ttyd behind it, the test
writes what a dispatch script would print straight into the client's terminal. The wrapper is itself framed by a
stand-in for the shell, and speaks to it through the shell's built app contract; the stand-in keeps what the page
posted.
"""

import base64
import gzip
import json
from collections.abc import Iterator
from typing import Final

import pytest
from app_manifest.testing import (
    SHELL_HANDSHAKE_COUNTER_SCRIPT,
    answer_ask_agent,
    built_app_contract_source,
    resend_shell_handshake,
    shell_messages,
    shell_stand_in_page,
)
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Locator,
    Page,
    Route,
    expect,
)
from terminal_app.dispatch import (
    ENDED_MISSING_FOLDER,
    ENDED_NOT_STARTED,
    load_ttyd_web_client,
)
from terminal_app.errors import TerminalAppError, TerminalStoreError, TmuxCommandError
from terminal_app.pages import (
    PageConfig,
    SessionPage,
    page_failure,
    render_failure_page,
    render_page,
)
from terminal_app.primitives import TerminalTitle, TmuxSessionName
from terminal_app.pty_page import TERMINAL_ENDED_OSC, add_pty_page_script

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_WRAPPER_HOST: Final[str] = "terminal.test"
_PTY_LABEL: Final[str] = "pty"
_SHELL_URL: Final[str] = "http://shell.test/"
_WRAPPER_URL: Final[str] = f"http://{_WRAPPER_HOST}/?session=terminal-1"


def _wrapper_html(session: str) -> str:
    name = TmuxSessionName(session)
    page = SessionPage(
        name=name,
        title=TerminalTitle(f"Terminal {session.removeprefix('terminal-')}"),
        pty_path=f"/?arg=_&arg=session&arg={session}",
        pty_label=_PTY_LABEL,
    )
    return render_page(PageConfig(session=name, page=page))


@pytest.fixture
def desktop_context(module_browser: Browser) -> Iterator[BrowserContext]:
    context = module_browser.new_context(viewport={"width": 1024, "height": 768})
    yield context
    context.close()


def _serve(context: BrowserContext, wrapper_html: str, can_report_bug: bool = False) -> list[str]:
    """Serve a shell stand-in saying ``can_report_bug`` and framing ``wrapper_html``, the real ttyd client, the shell's
    built contract, and the wrapper's ``new`` and session routes; answers the methods ``new`` was asked with."""
    client = add_pty_page_script(gzip.decompress(load_ttyd_web_client(None) or b""))
    contract_source = built_app_contract_source()
    stand_in = shell_stand_in_page(_WRAPPER_URL, can_report_bug)
    launched: list[str] = []

    def answer(route: Route) -> None:
        url = route.request.url
        if url == _SHELL_URL:
            route.fulfill(content_type="text/html", body=stand_in)
        elif url == _WRAPPER_URL:
            route.fulfill(content_type="text/html", body=wrapper_html)
        elif url.startswith(f"http://{_PTY_LABEL}.{_WRAPPER_HOST}/?arg="):
            route.fulfill(content_type="text/html", body=client)
        elif url == f"http://{_WRAPPER_HOST}/_static/app_contract.js":
            route.fulfill(content_type="text/javascript", body=contract_source)
        elif url == f"http://{_WRAPPER_HOST}/new":
            launched.append(route.request.method)
            route.fulfill(content_type="application/json", body=json.dumps({"path": "/?session=terminal-2"}))
        elif url == f"http://{_WRAPPER_HOST}/api/sessions/terminal-2":
            body = {
                "name": "terminal-2",
                "title": "Terminal 2",
                "pty_path": "/?arg=_&arg=session&arg=terminal-2",
                "pty_label": _PTY_LABEL,
            }
            route.fulfill(content_type="application/json", body=json.dumps(body))
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", answer)
    context.add_init_script(SHELL_HANDSHAKE_COUNTER_SCRIPT)
    return launched


def _open(context: BrowserContext) -> tuple[Page, Frame, Frame]:
    page = context.new_page()
    pty_url = f"http://{_PTY_LABEL}.{_WRAPPER_HOST}/?arg=_&arg=session&arg=terminal-1"
    # A framed wrapper points its pty only once the shell's handshake has said whether the window is viewed.
    with page.expect_event("framenavigated", predicate=lambda frame: frame.url == pty_url) as navigated:
        page.goto(_SHELL_URL)
    pty = navigated.value
    wrapper = page.frame(url=_WRAPPER_URL)
    assert wrapper is not None
    pty.wait_for_function("window.term !== undefined && window.term.element !== undefined", timeout=5000)
    return page, wrapper, pty


def _print_end(pty: Frame, kind: str, detail: str) -> None:
    encoded = base64.b64encode(detail.encode()).decode()
    pty.evaluate("(text) => new Promise((resolve) => term.write(text, resolve))", f"\x1b]{TERMINAL_ENDED_OSC};{kind};{encoded}\x07")


def test_a_terminal_tmux_would_not_start_says_so_and_offers_try_again_an_agent_and_a_bug_report(
    desktop_context: BrowserContext,
) -> None:
    _serve(desktop_context, _wrapper_html("terminal-1"), can_report_bug=True)
    page, wrapper, pty = _open(desktop_context)

    _print_end(pty, ENDED_NOT_STARTED, "no server running on /tmp/tmux-1000/default")

    ended = wrapper.locator("#ended")
    expect(ended.locator("#ended-message")).to_have_text(
        "This terminal couldn't start. Try again. If this persists, report a bug."
    )
    expect(wrapper.locator("#pty")).to_be_hidden()
    ended.locator("#ended-details").get_by_text("See details").click()
    expect(ended.locator("#ended-details-text")).to_have_text("no server running on /tmp/tmux-1000/default")
    expect(ended.get_by_role("button", name="Start a new terminal")).to_be_hidden()

    ended.get_by_role("button", name="Ask an agent to fix this").click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:ask-agent')")
    [asked] = shell_messages(page, "shell:ask-agent")
    # The page shows the outcome under the button, so the shell toasts nothing of its own.
    assert asked["showsOutcome"] is True
    task = str(asked["text"])
    assert task.startswith("The terminal terminal-1 couldn't start.\n\nDetails:\n```\n")
    assert "no server running on /tmp/tmux-1000/default\n```" in task
    _expect_asking_then_told_why(page, ended)
    # The ending's own "See details" stays with its sentence, above what the ask came back with.
    assert wrapper.evaluate(
        "document.getElementById('ended-details').compareDocumentPosition("
        "document.getElementById('ended-ask-failure')) & Node.DOCUMENT_POSITION_FOLLOWING"
    )

    ended.get_by_role("button", name="Report a bug").click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    [reported] = shell_messages(page, "shell:report-bug")
    assert reported["message"] == "This terminal couldn't start. Try again. If this persists, report a bug."
    # The report also carries the failed ask still shown under the agent button.
    assert reported["details"] == (
        "no server running on /tmp/tmux-1000/default\n\n"
        "Asking an agent to fix this failed too:\nChat couldn't open that.\nHTTP 400"
    )

    ended.get_by_role("button", name="Try again").click()
    expect(ended).to_be_hidden()
    expect(wrapper.locator("#pty")).to_be_visible()


def test_a_terminal_whose_folder_is_gone_says_so_and_starts_a_new_one_in_the_same_window(
    desktop_context: BrowserContext,
) -> None:
    launched = _serve(desktop_context, _wrapper_html("terminal-1"))
    page, wrapper, pty = _open(desktop_context)

    _print_end(pty, ENDED_MISSING_FOLDER, "/data/gone")

    ended = wrapper.locator("#ended")
    expect(ended.locator("#ended-message")).to_have_text("This terminal closed.")
    expect(ended.locator("#ended-cause")).to_have_text("This terminal's folder no longer exists, so it can't open.")
    expect(ended.get_by_role("button", name="Try again")).to_be_hidden()
    expect(ended.get_by_role("button", name="Ask an agent to fix this")).to_be_hidden()

    ended.get_by_role("button", name="Start a new terminal").click()

    expect(ended).to_be_hidden()
    expect(wrapper.locator("#pty")).to_have_attribute(
        "src", f"http://{_PTY_LABEL}.{_WRAPPER_HOST}/?arg=_&arg=session&arg=terminal-2"
    )
    assert launched == ["POST"]
    page.wait_for_function("window.shellMessages.some((message) => message.path === '/?session=terminal-2')")
    locations = shell_messages(page, "shell:location")
    assert locations[-1] == {"type": "shell:location", "path": "/?session=terminal-2", "title": "Terminal 2"}


def test_an_unknown_kind_from_the_pty_reads_as_a_terminal_that_closed(desktop_context: BrowserContext) -> None:
    _serve(desktop_context, _wrapper_html("terminal-1"))
    _page, wrapper, pty = _open(desktop_context)

    # Anything in the pty can print the sequence; a kind naming an Object member is not one of the endings.
    _print_end(pty, "constructor", "printed by a program")

    ended = wrapper.locator("#ended")
    expect(ended.locator("#ended-message")).to_have_text("This terminal closed.")
    expect(ended.get_by_role("button", name="Start a new terminal")).to_be_visible()


def _expect_asking_then_told_why(page: Page, surface: Locator) -> None:
    """After a press of "Ask an agent to fix this": it reads "Asking an agent…" and is disabled until the shell
    answers, and a failed answer brings it back with the failure's sentence and details beside it."""
    asking = surface.get_by_role("button", name="Asking an agent…")
    expect(asking).to_be_disabled()
    answer_ask_agent(
        page,
        {"isOpened": False, "failure": {"message": "Chat couldn't open that.", "details": "HTTP 400", "isReportable": False}},
    )
    expect(surface.get_by_role("button", name="Ask an agent to fix this")).to_be_enabled()
    told = surface.get_by_role("alert")
    expect(told).to_contain_text("Chat couldn't open that.")
    told.get_by_text("See details").click()
    expect(told.locator("pre")).to_have_text("HTTP 400")


def test_a_pending_ask_survives_the_shell_greeting_again_and_takes_its_late_answer_after_a_restart(
    desktop_context: BrowserContext,
) -> None:
    _serve(desktop_context, _wrapper_html("terminal-1"), can_report_bug=True)
    page, wrapper, pty = _open(desktop_context)
    _print_end(pty, ENDED_NOT_STARTED, "no server running on /tmp/tmux-1000/default")
    ended = wrapper.locator("#ended")
    ended.get_by_role("button", name="Ask an agent to fix this").click()
    expect(ended.get_by_role("button", name="Asking an agent…")).to_be_disabled()

    # The same shell greeting again (a window moved, viewing started) leaves the ask pending.
    resend_shell_handshake(page)
    wrapper.wait_for_function("window.shellHandshakes > 1")
    expect(ended.get_by_role("button", name="Asking an agent…")).to_be_disabled()

    # A shell that started again settles it as not answered, and its real answer still replaces that.
    resend_shell_handshake(page, "restarted-shell")
    expect(ended.get_by_role("alert")).to_contain_text("The workspace didn't answer")
    expect(ended.get_by_role("button", name="Ask an agent to fix this")).to_be_enabled()
    answer_ask_agent(
        page,
        {"isOpened": False, "failure": {"message": "The workspace couldn't do that.", "details": "boom", "isReportable": True}},
    )
    told = ended.get_by_role("alert")
    expect(told).to_contain_text("The workspace couldn't do that.")
    # One Report a bug, the page's, files the ending and the failed ask together.
    expect(told.get_by_role("button", name="Report a bug")).to_have_count(0)
    report = ended.get_by_role("button", name="Report a bug")
    expect(report).to_have_count(1)
    report.click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    [reported] = shell_messages(page, "shell:report-bug")
    assert reported["message"] == "This terminal couldn't start. Try again. If this persists, report a bug."
    assert reported["details"] == (
        "no server running on /tmp/tmux-1000/default\n\n"
        "Asking an agent to fix this failed too:\nThe workspace couldn't do that.\nboom"
    )


def test_an_ask_about_an_earlier_ending_never_shows_under_a_later_one(desktop_context: BrowserContext) -> None:
    _serve(desktop_context, _wrapper_html("terminal-1"), can_report_bug=True)
    page, wrapper, pty = _open(desktop_context)
    _print_end(pty, ENDED_NOT_STARTED, "first failure")
    ended = wrapper.locator("#ended")
    ended.get_by_role("button", name="Ask an agent to fix this").click()

    ended.get_by_role("button", name="Try again").click()
    _print_end(pty, ENDED_NOT_STARTED, "second failure")
    expect(ended.get_by_role("button", name="Ask an agent to fix this")).to_be_enabled()
    answer_ask_agent(
        page,
        {"isOpened": False, "failure": {"message": "Chat couldn't open that.", "details": None, "isReportable": False}},
    )
    expect(ended.get_by_role("button", name="Ask an agent to fix this")).to_be_enabled()
    expect(ended.get_by_role("alert")).to_be_hidden()


def _open_failure_page(context: BrowserContext, error: TerminalAppError, can_report_bug: bool) -> tuple[Page, Frame]:
    _serve(context, render_failure_page(page_failure(error, "terminal-1")), can_report_bug)
    page = context.new_page()
    page.goto(_SHELL_URL)
    wrapper = page.frame(url=_WRAPPER_URL)
    assert wrapper is not None
    wrapper.wait_for_function("window.shellHandshakes > 0")
    return page, wrapper


def test_a_failure_page_for_unreadable_saved_terminals_asks_an_agent_through_the_shell(
    desktop_context: BrowserContext,
) -> None:
    error = TerminalStoreError("the terminal store /x/instances.json is not valid JSON: x")
    page, wrapper = _open_failure_page(desktop_context, error, can_report_bug=True)

    # Saved terminals an agent can repair do not ask for a bug report.
    expect(wrapper.get_by_role("button", name="Try again")).to_be_hidden()
    expect(wrapper.get_by_role("button", name="Report a bug")).to_be_hidden()
    wrapper.get_by_role("button", name="Ask an agent to fix this").click()

    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:ask-agent')")
    [asked] = shell_messages(page, "shell:ask-agent")
    # The page shows the outcome under the button, so the shell toasts nothing of its own.
    assert asked["showsOutcome"] is True
    task = str(asked["text"])
    assert task.startswith("The Terminal app can't open terminals: it can't read this workspace's saved terminals.")
    assert "the terminal store /x/instances.json is not valid JSON: x\n```" in task
    _expect_asking_then_told_why(page, wrapper.locator("body"))
    expect(wrapper.get_by_role("button", name="Report a bug")).to_be_hidden()

    # An ask that fails in a way to report brings the page's one Report a bug, which files both failures.
    wrapper.get_by_role("button", name="Ask an agent to fix this").click()
    page.wait_for_function(
        "window.shellMessages.filter((message) => message.type === 'shell:ask-agent').length === 2"
    )
    answer_ask_agent(
        page,
        {"isOpened": False, "failure": {"message": "The workspace couldn't do that.", "details": "boom", "isReportable": True}},
    )
    report = wrapper.get_by_role("button", name="Report a bug")
    expect(report).to_have_count(1)
    report.click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    [reported] = shell_messages(page, "shell:report-bug")
    assert reported["message"] == "This workspace's saved terminals can't be read, so terminals can't open."
    assert str(reported["details"]).endswith(
        "\n\nAsking an agent to fix this failed too:\nThe workspace couldn't do that.\nboom"
    )


def test_a_failure_page_while_tmux_does_not_answer_offers_only_try_again(desktop_context: BrowserContext) -> None:
    error = TmuxCommandError("tmux did not finish list-sessions within 5.0s")
    _page, wrapper = _open_failure_page(desktop_context, error, can_report_bug=True)

    expect(wrapper.get_by_role("button", name="Try again")).to_be_visible()
    expect(wrapper.get_by_role("button", name="Ask an agent to fix this")).to_be_hidden()
    expect(wrapper.get_by_role("button", name="Report a bug")).to_be_hidden()


@pytest.mark.parametrize("can_report_bug", [True, False], ids=["embedder-opens-reports", "no-bug-report"])
def test_a_failure_page_that_asks_for_a_bug_report_offers_one_only_where_the_shell_can_open_it(
    desktop_context: BrowserContext, can_report_bug: bool
) -> None:
    page, wrapper = _open_failure_page(desktop_context, TerminalAppError("unexpected"), can_report_bug)

    expect(wrapper.get_by_role("button", name="Try again")).to_be_visible()
    expect(wrapper.get_by_role("button", name="Ask an agent to fix this")).to_be_hidden()
    report = wrapper.get_by_role("button", name="Report a bug")
    if not can_report_bug:
        expect(report).to_be_hidden()
        return
    report.click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    [reported] = shell_messages(page, "shell:report-bug")
    assert reported["message"] == "This terminal couldn't be opened. Try again. If this persists, report a bug."
    assert reported["details"] == "unexpected"


def test_a_failure_page_opened_on_its_own_offers_no_agent(desktop_context: BrowserContext) -> None:
    error = TerminalStoreError("the terminal store /x/instances.json is not valid JSON: x")
    _serve(desktop_context, render_failure_page(page_failure(error, "terminal-1")), can_report_bug=True)
    page = desktop_context.new_page()

    page.goto(_WRAPPER_URL)

    expect(page.locator(".failure-message")).to_have_text(
        "This workspace's saved terminals can't be read, so terminals can't open."
    )
    expect(page.get_by_role("button", name="Ask an agent to fix this")).to_be_hidden()
