"""Browser: the Screen's built viewer, framed by a stand-in for the shell, says why the Screen stopped in our words and
offers what can help through the shell: "Ask an agent to fix this" always under a shell, "Report a bug" only where
the shell's handshake says Imbue Studio opens one, and a refused Start under the overlay's title. A state that got no
answer names the Screen, offers Try again, and shows no control for a Screen it knows nothing about.

The service is stood in for by routes answering a crashed stack's state and a refused start; the viewer's event
socket finds nothing and keeps retrying, which changes nothing here.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import expect

from app_manifest.testing import SHELL_HANDSHAKE_COUNTER_SCRIPT
from app_manifest.testing import shell_stand_in_page

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

_STATIC_DIR: Final[Path] = Path(__file__).parent / "src" / "screen" / "static"
_SCREEN_ORIGIN: Final[str] = "http://screen.test"
_SHELL_URL: Final[str] = "http://shell.test/"
_CRASH_REASON: Final[str] = "screen-neko exited unexpectedly"

_CRASHED_STATE: Final[dict[str, Any]] = {
    "lifecycle": "stopped",
    "install_reason": None,
    "install_failure": None,
    "stop_reason": _CRASH_REASON,
    "stop_cause": "crashed",
    "title": "Screen",
    "video_id": "watch",
    "control": {
        "controller": "nobody",
        "human_pinned": False,
        "human_session_id": None,
        "human_name": None,
        "agent_id": None,
        "agent_name": None,
        "waiting": [],
        "idle_seconds": None,
        "idle_release_seconds": None,
    },
    "screen_size": None,
    "chromium": {"state": "absent", "name": None, "pending_url": None},
}


@pytest.fixture
def desktop_context(module_browser: Browser) -> Iterator[BrowserContext]:
    if not (_STATIC_DIR / "index.html").is_file():
        pytest.skip(
            f"the Screen's viewer is not built: no {_STATIC_DIR / 'index.html'} (`cd system && npm run build`)"
        )
    context = module_browser.new_context(viewport={"width": 1024, "height": 768})
    yield context
    context.close()


def _serve(context: BrowserContext, can_report_bug: bool, unanswered_state_reads: int = 0) -> None:
    """The shell stand-in saying ``can_report_bug``, the built viewer, a crashed stack's state (after the first
    ``unanswered_state_reads`` reads of it get the front door's own 503), and a start the service refuses."""
    stand_in = shell_stand_in_page(f"{_SCREEN_ORIGIN}/", can_report_bug)
    state_reads = 0

    def answer(route: Route) -> None:
        url = route.request.url
        if url == _SHELL_URL:
            route.fulfill(content_type="text/html", body=stand_in)
        elif url == f"{_SCREEN_ORIGIN}/":
            route.fulfill(content_type="text/html", body=(_STATIC_DIR / "index.html").read_text())
        elif url.startswith(f"{_SCREEN_ORIGIN}/assets/"):
            asset = _STATIC_DIR / url.removeprefix(f"{_SCREEN_ORIGIN}/")
            content_type = "text/css" if asset.suffix == ".css" else "text/javascript"
            route.fulfill(content_type=content_type, body=asset.read_bytes())
        elif url == f"{_SCREEN_ORIGIN}/api/state":
            nonlocal state_reads
            state_reads += 1
            if state_reads <= unanswered_state_reads:
                route.fulfill(status=503, content_type="text/plain", body="upstream connect error")
            else:
                route.fulfill(content_type="application/json", body=json.dumps(_CRASHED_STATE))
        elif url == f"{_SCREEN_ORIGIN}/api/start":
            route.fulfill(
                status=409, content_type="application/json", body=json.dumps({"detail": "the stack is stopping"})
            )
        else:
            route.fulfill(status=404, body="")

    context.route("**/*", answer)
    context.add_init_script(SHELL_HANDSHAKE_COUNTER_SCRIPT)


def _open_unsettled(context: BrowserContext) -> tuple[Page, Frame]:
    page = context.new_page()
    page.goto(_SHELL_URL)
    viewer = page.frame(url=f"{_SCREEN_ORIGIN}/")
    assert viewer is not None
    # The handshake has been handled once the counter moved: what the overlay hides for want of it is hidden for that.
    viewer.wait_for_function("window.shellHandshakes > 0", timeout=10000)
    return page, viewer


def _open(context: BrowserContext) -> tuple[Page, Frame]:
    page, viewer = _open_unsettled(context)
    expect(viewer.locator("[data-screen-overlay-title]")).to_have_text("The Screen stopped unexpectedly.")
    return page, viewer


def _shell_messages(page: Page, message_type: str) -> list[dict[str, object]]:
    return [message for message in page.evaluate("window.shellMessages") if message["type"] == message_type]


def test_a_crash_offers_an_agent_and_a_bug_report_through_the_shell(desktop_context: BrowserContext) -> None:
    _serve(desktop_context, can_report_bug=True)
    page, viewer = _open(desktop_context)
    overlay = viewer.locator("[data-screen-overlay]")
    expect(overlay.locator("[data-screen-overlay-details] pre")).to_have_text(_CRASH_REASON)
    expect(overlay.locator("[data-screen-start]")).to_be_visible()
    viewer.locator("[data-screen-ask-agent]").click()
    viewer.locator("[data-screen-report-bug]").click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    started = _shell_messages(page, "shell:start-new-with-text")
    assert len(started) == 1
    assert "The Screen stopped unexpectedly." in str(started[0]["text"])
    assert _CRASH_REASON in str(started[0]["text"])
    [reported] = _shell_messages(page, "shell:report-bug")
    assert (reported["message"], reported["details"]) == ("The Screen stopped unexpectedly.", _CRASH_REASON)


def test_no_bug_report_where_the_shell_cannot_open_one_and_a_refused_start_is_said(
    desktop_context: BrowserContext,
) -> None:
    _serve(desktop_context, can_report_bug=False)
    _page, viewer = _open(desktop_context)
    expect(viewer.locator("[data-screen-ask-agent]")).to_be_visible()
    expect(viewer.locator("[data-screen-report-bug]")).to_be_hidden()
    viewer.locator("[data-screen-start]").click()
    expect(viewer.locator("[data-screen-overlay-body]")).to_have_text("The Screen couldn't start.")
    expect(viewer.locator("[data-screen-overlay-details] pre")).to_contain_text("the stack is stopping")


def test_a_state_nothing_answered_names_the_screen_and_try_again_loads_it(desktop_context: BrowserContext) -> None:
    _serve(desktop_context, can_report_bug=True, unanswered_state_reads=1)
    _page, viewer = _open_unsettled(desktop_context)
    expect(viewer.locator("[data-screen-overlay-title]")).to_have_text("The Screen couldn't load.")
    expect(viewer.locator("[data-screen-overlay-body]")).to_have_text(
        "The workspace didn't answer. Try again in a moment."
    )
    expect(viewer.locator("[data-screen-report-bug]")).to_be_hidden()
    expect(viewer.locator("[data-screen-agent-overlay]")).to_be_hidden()
    expect(viewer.locator("[data-screen-take-control]")).to_be_hidden()
    expect(viewer.locator("[data-screen-return-control]")).to_be_hidden()

    viewer.locator("[data-screen-reload]").click()

    expect(viewer.locator("[data-screen-overlay-title]")).to_have_text("The Screen stopped unexpectedly.")
    expect(viewer.locator("[data-screen-reload]")).to_be_hidden()
