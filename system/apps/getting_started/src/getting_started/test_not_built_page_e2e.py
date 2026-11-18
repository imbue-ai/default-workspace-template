"""The page Getting Started serves while its bundle is missing, framed by a stand-in shell in a real browser: its
"Ask an agent to rebuild it" shows a refused ask under the button, with "Report a bug" for a failure that asks for one,
offered only while the shell's handshake says Imbue Studio opens a bug report."""

import contextlib
import shutil
from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest
from flask import Flask
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect

from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from getting_started.pages import build_pages_app
from getting_started.serving import serve_in_background
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
_BUILT_CONTRACT: Final[Path] = _REPO_ROOT / SHELL_APP_CONTRACT_PATH

pytestmark = pytest.mark.browser

_FAILURE: Final[dict[str, object]] = {
    "message": "Chat couldn't open that. Try again. If this persists, report a bug.",
    "details": "HTTP 500\nboom",
    "isReportable": True,
}

# Greets the frame as the shell does; the page loads the contract module after its own load, so the stand-in greets
# it until the ask button shows (a shell greets its pages again whenever what it says changes).
_GREET: Final[str] = """(canReportBug) => document.getElementById("page").contentWindow.postMessage(
  { type: "shell:handshake", clientId: "c", windowId: "w", desktopId: "d", app: "getting-started", path: "/",
    shellId: "shell-1", canReportBug },
  "*",
)"""

# Answers the page's ask with a failure, as a shell whose launch was refused does.
_REFUSE_ASK: Final[str] = """(failure) => {
  const frame = document.getElementById("page").contentWindow;
  window.addEventListener("message", (event) => {
    if (event.source !== frame || event.data.type !== "shell:start-new-with-text") return;
    frame.postMessage({ type: "shell:start-new-with-text-result", requestId: event.data.requestId, isOpened: false,
      failure }, "*");
  });
}"""


@contextlib.contextmanager
def _framed_not_built_page(tmp_path: Path, browser: Browser, can_report_bug: bool) -> Generator[Page, None, None]:
    """Getting Started with no bundle, framed by a stand-in shell on another origin, once the shell refused its ask."""
    if not _BUILT_CONTRACT.is_file():
        pytest.fail("The shell's app contract is not built: run `npm run build` in system/ first")
    contract_path = tmp_path / "app_contract.js"
    shutil.copyfile(_BUILT_CONTRACT, contract_path)
    store = TemplateCatalogStore(
        catalog_url="https://example.test/catalog.json",
        cache_path=tmp_path / "catalog.json",
        fetcher=FakeTemplateCatalogFetcher(),
    )
    with contextlib.ExitStack() as stack:
        app_server = stack.enter_context(
            serve_in_background("127.0.0.1", 0, build_pages_app(tmp_path / "static", store, contract_path))
        )
        shell = Flask(__name__)
        shell_page = (
            "<!doctype html><html><body><script>window.posted = [];"
            "window.addEventListener('message', (event) => window.posted.push(event.data));</script>"
            f"<iframe id='page' src='http://127.0.0.1:{app_server.socket.getsockname()[1]}/'></iframe></body></html>"
        )
        shell.add_url_rule("/", "shell", lambda: shell_page)
        shell_port = stack.enter_context(serve_in_background("127.0.0.1", 0, shell)).socket.getsockname()[1]
        page = stack.enter_context(contextlib.closing(browser.new_context())).new_page()
        page.goto(f"http://localhost:{shell_port}/")
        page.evaluate(_REFUSE_ASK, _FAILURE)
        ask = page.frame_locator("#page").locator("#ask-agent")
        for _ in range(50):
            page.evaluate(_GREET, can_report_bug)
            if ask.is_visible():
                break
            page.wait_for_timeout(100)
        expect(ask).to_be_visible()
        ask.click()
        expect(page.frame_locator("#page").locator("#ask-agent-failure")).to_have_text(str(_FAILURE["message"]))
        yield page


def test_a_refused_ask_offers_a_bug_report_that_carries_its_failure(tmp_path: Path, module_browser: Browser) -> None:
    with _framed_not_built_page(tmp_path, module_browser, can_report_bug=True) as page:
        report = page.frame_locator("#page").get_by_role("button", name="Report a bug")
        expect(report).to_be_visible()
        report.click()
        page.wait_for_function("window.posted.some((message) => message.type === 'shell:report-bug')")
        reported = page.evaluate("window.posted.filter((message) => message.type === 'shell:report-bug')")
        assert reported == [
            {"type": "shell:report-bug", "message": _FAILURE["message"], "details": _FAILURE["details"]}
        ]


def test_a_refused_ask_offers_no_bug_report_where_none_can_be_opened(tmp_path: Path, module_browser: Browser) -> None:
    with _framed_not_built_page(tmp_path, module_browser, can_report_bug=False) as page:
        expect(page.frame_locator("#page").get_by_role("button", name="Report a bug")).to_be_hidden()
