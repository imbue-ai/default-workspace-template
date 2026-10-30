"""The page the chat origin serves when its bundle is missing, framed by a stand-in shell in a real browser: its
"Report a bug" shows only while the shell's handshake says Imbue Studio opens a bug report, and asks the shell for one.

The stand-in greets the frame on its load event, as the shell does, so the page must be listening by then.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from flask import Flask
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.chat.agent_manager import AgentManager
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state
from imbue.chat.testing import is_e2e_browser_installed
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.testing import serve_app

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
_BUILT_CONTRACT: Final[Path] = _REPO_ROOT / SHELL_APP_CONTRACT_PATH

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _BUILT_CONTRACT.is_file(),
        reason="The shell's app contract is not built (run `npm run build` in system/); skipping e2e.",
    ),
]

# Under the suite's per-test timeout, so a page that never reports fails here rather than in a timeout's teardown.
_WAIT_MS: Final[int] = 5000

_GREET_FRAME: Final[str] = """(canReportBug) => document.getElementById("chat").contentWindow.postMessage(
  { type: "shell:handshake", clientId: "client-1", windowId: "win-1", desktopId: "home", app: "chat", canReportBug },
  "*",
)"""


def _stand_in_shell(chat_url: str, can_report_bug: bool) -> Flask:
    """A page that frames ``chat_url``, greets it on every load, and records what the page posts to it."""
    shell = Flask(__name__)
    page = f"""<!doctype html><html><body>
<iframe id="chat" src="{chat_url}"></iframe>
<script>
window.posted = [];
const frame = document.getElementById("chat");
window.addEventListener("message", (event) => {{
  if (event.source === frame.contentWindow) window.posted.push(event.data);
}});
frame.addEventListener("load", () => ({_GREET_FRAME})({"true" if can_report_bug else "false"}));
</script></body></html>"""
    shell.add_url_rule("/", "shell", lambda: page)
    return shell


@contextlib.contextmanager
def _framed_chat_without_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page, can_report_bug: bool
) -> Generator[None, None, None]:
    """A chat app whose static directory is empty, run from a repo root holding the shell's built contract, framed by
    a stand-in shell that ``page`` shows."""
    monkeypatch.chdir(tmp_path)
    served_contract = tmp_path / SHELL_APP_CONTRACT_PATH
    served_contract.parent.mkdir(parents=True)
    shutil.copyfile(_BUILT_CONTRACT, served_contract)
    state = build_test_state(agent_manager=AgentManager.build(WebSocketBroadcaster()))
    state.static_directory = tmp_path / "static"
    with serve_app(create_application(state)) as chat:
        with serve_app(_stand_in_shell(f"{chat.http_url}/", can_report_bug)) as shell:
            # Another origin than the chat's, as the shell's is.
            page.goto(shell.http_url.replace("127.0.0.1", "localhost"))
            expect(page.frame_locator("#chat").locator("p")).to_contain_text(
                "Chat isn't available in this workspace right now."
            )
            yield


def test_the_not_built_page_asks_the_shell_for_a_bug_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page
) -> None:
    with _framed_chat_without_a_bundle(tmp_path, monkeypatch, page, can_report_bug=True):
        report = page.frame_locator("#chat").get_by_role("button", name="Report a bug")
        expect(report).to_be_visible()
        report.click()

        page.wait_for_function(
            "window.posted.some((message) => message.type === 'shell:report-bug')", timeout=_WAIT_MS
        )


def test_the_not_built_page_offers_a_bug_report_only_while_the_shell_can_open_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page
) -> None:
    with _framed_chat_without_a_bundle(tmp_path, monkeypatch, page, can_report_bug=True):
        report = page.frame_locator("#chat").get_by_role("button", name="Report a bug")
        expect(report).to_be_visible()

        # The shell greets its pages again when whether Imbue Studio opens a bug report changes.
        page.evaluate(_GREET_FRAME, False)

        expect(report).to_be_hidden()
