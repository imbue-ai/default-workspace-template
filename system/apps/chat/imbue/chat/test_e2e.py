"""End-to-end tests for the chat pages using Playwright.

These tests serve the shell and the chat app together (``running_workspace``: two threaded
Werkzeug servers over a registry holding the chat row at the chat's own URL, with mocked agent
discovery behind the chat), then use Playwright to open a chat from the shell exactly as a user
would and assert on the chat page inside its frame. The shell's own behaviour is the shell
package's suite; what is tested here is the chat document.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import free_port
from playwright.sync_api import Frame
from playwright.sync_api import FrameLocator
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import FIXTURE_CHAT_ADDRESS
from imbue.chat.testing import RunningWorkspace
from imbue.chat.testing import STARTER_PROJECT_ID
from imbue.chat.testing import STARTER_PROJECT_NAME
from imbue.chat.testing import STUB_APP_NAME
from imbue.chat.testing import is_e2e_browser_installed
from imbue.chat.testing import running_workspace
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.app_context import DEFAULT_STATIC_DIRECTORY as SHELL_STATIC_DIRECTORY
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_NAME


def _playwright_browsers_installed() -> bool:
    """Check whether a launchable browser is present (Fortress or Playwright's cache)."""
    return is_e2e_browser_installed()


def _frontends_built() -> bool:
    """Whether both bundles exist: the chat page (``static/chat.html``) and the shell that frames it."""
    return (Path(__file__).parent / "static" / "chat.html").is_file() and (
        SHELL_STATIC_DIRECTORY / "index.html"
    ).is_file()


pytestmark = [
    pytest.mark.release,
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontends_built(),
        reason="The chat or shell frontend is not built (run `npm run build` in system/); skipping e2e.",
    ),
]

_TRIGGER_TIMEOUT_MS = 20000


def _chat(page: Page, agent_id: str = FIXTURE_AGENT_ID) -> FrameLocator:
    """The chat's page, framed at the chat origin under the instance's address.

    Every chat assertion goes through it: the shell document holds no chat markup, only
    the frame.
    """
    return page.frame_locator(f'iframe[data-address="app:chat?instance={agent_id}"]')


def _chat_frame(page: Page, agent_id: str = FIXTURE_AGENT_ID) -> Frame:
    """The chat page's own frame, for the evaluate and wait calls that need its document.

    Polled: the frame is created when the pane docks and loads a beat later.
    """
    for _ in range(150):
        for frame in page.frames:
            if frame.url.rstrip("/").endswith(f"/{agent_id}"):
                return frame
        page.wait_for_timeout(100)
    raise TimeoutError(f"the chat page for {agent_id} never loaded in a frame")


def _running_e2e_server(
    tmp_path: Path,
    session_events: list[dict[str, Any]] | None = None,
    is_stub_app_offered: bool = False,
    stub_instances: tuple[str, ...] = (),
    is_account_signed_in: bool = True,
) -> AbstractContextManager[RunningWorkspace]:
    """The two-server workspace, the shell and the chat each on a free port of their own."""
    return running_workspace(
        tmp_path,
        free_port(),
        free_port(),
        session_events=session_events,
        is_stub_app_offered=is_stub_app_offered,
        stub_instances=stub_instances,
        is_account_signed_in=is_account_signed_in,
    )


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[RunningWorkspace, None, None]:
    """Start the shell and the chat with the fixture agent and the starter project."""
    with _running_e2e_server(tmp_path) as server:
        yield server


# ---------- helpers ----------


def _client_layout_files(state_dir: Path, view_id: str) -> list[Path]:
    """The per-client layout files a view holds (the seeds beside them are not counted)."""
    view_dir = state_dir / "layouts" / view_id
    if not view_dir.is_dir():
        return []
    return [path for path in view_dir.glob("*.json") if not path.name.startswith("seed.")]


def _wait_for_layout_saved(state_dir: Path, view_id: str, containing: str | None = None) -> None:
    def _saved() -> bool:
        files = _client_layout_files(state_dir, view_id)
        if containing is None:
            return bool(files)
        return any(containing in path.read_text() for path in files)

    wait_for(
        _saved,
        timeout=15.0,
        poll_interval=0.1,
        error_message=f"autosave never wrote a layout for {view_id}"
        + (f" holding {containing}" if containing else ""),
    )


def _wait_for_view(page: Page, view_id: str) -> None:
    """The dock names the view it has mounted; the active view itself lives in the shell's client record."""
    page.wait_for_selector(f'.dockview-workspace[data-view-id="{view_id}"]', state="attached", timeout=15000)


def _launcher_row(page: Page, address: str) -> Any:
    return page.locator(f'.new-tab-launcher-row[data-address="{address}"]:visible')


def _open_from_launcher(page: Page, address: str) -> None:
    """Open an instance from the New Tab page (opening the page from the "+" when none is up)."""
    expect(page.locator(".dv-default-tab-content").first).to_be_visible(timeout=15000)
    launcher = page.locator(".new-tab-launcher:visible")
    if launcher.count() == 0:
        try:
            expect(launcher.first).to_be_visible(timeout=3000)
        except AssertionError:
            page.locator(".dockview-add-tab-button:visible").first.click()
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
    row = _launcher_row(page, address)
    expect(row.first).to_be_visible(timeout=15000)
    row.first.click()


def _start_new_chat(page: Page) -> FrameLocator:
    """Run the chat app's ``new`` from the New Tab page's tile, and return the frame of the chat it docked."""
    expect(page.locator(".dv-default-tab-content").first).to_be_visible(timeout=15000)
    if page.locator(".new-tab-launcher:visible").count() == 0:
        page.locator(".dockview-add-tab-button:visible").first.click()
    tile = page.locator('.new-tab-launcher-tile[data-launch="chat:new"]:visible')
    expect(tile.first).to_be_visible(timeout=15000)
    tile.first.click()
    frame = page.frame_locator('iframe[data-address^="app:chat?instance="]')
    expect(page.locator('iframe[data-address^="app:chat?instance="]').first).to_be_attached(timeout=15000)
    return frame


def _open_fixture_chat(page: Page) -> None:
    """Open the fixture chat from the New Tab page and wait for its transcript."""
    _open_from_launcher(page, FIXTURE_CHAT_ADDRESS)
    expect(_chat(page).locator(".message-list").first).to_be_visible(timeout=15000)


def _tab(page: Page, title: str | re.Pattern[str]) -> Any:
    return page.locator(".dv-default-tab-content", has_text=title).first


def _broadcast_layout_op(base_url: str, op: str, args: dict[str, Any], view: str = STARTER_PROJECT_NAME) -> None:
    """POST a layout op to the loopback ``/api/layout/broadcast`` endpoint, retrying until the client has registered."""
    payload = json.dumps({"op": op, "args": {**args, "view": view}, "requester": FIXTURE_CHAT_ADDRESS}).encode()
    request = urllib.request.Request(
        f"{base_url}/api/layout/broadcast",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _attempt() -> bool:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return bool(response.status == 200)
        except urllib.error.HTTPError as e:
            if e.code == 412:
                return False
            raise AssertionError(
                f"layout op {op!r} refused with HTTP {e.code}: {e.read().decode(errors='replace')}"
            ) from e
        except (TimeoutError, urllib.error.URLError):
            return False

    wait_for(
        _attempt,
        timeout=15.0,
        poll_interval=0.2,
        error_message=f"layout broadcast for op {op!r} never succeeded (client registration missing?)",
    )


def _stub_address(key: str) -> str:
    return f"app:{STUB_APP_NAME}?instance={key}"


def _open_rail_switcher(page: Page) -> None:
    page.locator(".project-rail-header").click()
    expect(page.locator(".project-rail-menu")).to_be_visible(timeout=5000)


def _switch_view_via_rail(page: Page, view_name: str) -> None:
    _open_rail_switcher(page)
    page.locator(".project-rail-menu [role='menuitem']", has_text=view_name).first.click()


# A page for a stub instance's frame, served by a Playwright route rather than by the stub
# (which serves only its instances API).
_FRAMED_PAGE_HTML = "<!doctype html><html><body><input id='held' value='' /></body></html>"


def _serve_stub_pages(page: Page, server: RunningWorkspace) -> None:
    assert server.stub_url is not None
    page.route(
        f"{server.stub_url}/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_FRAMED_PAGE_HTML),
    )


# ---------- the chat page ----------


@pytest.mark.timeout(60, func_only=False)
def test_chat_transcript_area_is_pure_white(e2e_server: RunningWorkspace, page: Page) -> None:
    """The chat conversation panel renders on a pure-white background, scoped to the chat token."""
    page.goto(e2e_server.shell_url)
    _open_fixture_chat(page)

    content = _chat(page).locator(".app-content")
    expect(content).to_be_visible(timeout=15000)
    expect(content.locator(".message-list")).to_have_count(1)

    content_bg = _chat_frame(page).eval_on_selector(".app-content", "e => getComputedStyle(e).backgroundColor")
    assert content_bg == "rgb(255, 255, 255)", f"chat transcript area should be pure white, got {content_bg}"
    footer_bg = _chat_frame(page).eval_on_selector(".app-footer", "e => getComputedStyle(e).backgroundColor")
    assert footer_bg == "rgb(255, 255, 255)", f"composer footer should be pure white, got {footer_bg}"
    shell_bg = page.eval_on_selector("html", "e => getComputedStyle(e).getPropertyValue('--color-bg').trim()")
    assert shell_bg not in ("#ffffff", "#fff", "rgb(255, 255, 255)"), (
        f"shared shell --color-bg should stay off-white, got {shell_bg}"
    )


@pytest.mark.timeout(60, func_only=False)
def test_conversation_and_composer_render(e2e_server: RunningWorkspace, page: Page) -> None:
    """The opened chat shows both sides of its conversation and a composer whose send button follows the text."""
    page.goto(e2e_server.shell_url)
    _open_fixture_chat(page)

    expect(_chat(page).locator(".message-user").first).to_contain_text("Hello agent!")
    expect(_chat(page).locator(".message-assistant").first).to_contain_text("Hello! How can I help you?")

    textarea = _chat(page).locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=15000)
    send_button = _chat(page).locator(".message-input-send-button")
    expect(send_button).to_have_count(0)
    textarea.fill("test message")
    expect(send_button).to_be_visible()


@pytest.mark.timeout(60, func_only=False)
def test_composer_bar_survives_a_shorter_window(e2e_server: RunningWorkspace, page: Page) -> None:
    """A window that gets shorter keeps the whole composer on screen.

    Everything below the dock is positioned in pixels -- the panes, and the live surfaces
    mirroring them -- so a row that grows with the viewport but cannot shrink back leaves
    the chat laid out at the old height with the model bar below the bottom edge.
    """
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(e2e_server.shell_url)
    _open_fixture_chat(page)

    under_bar = _chat(page).locator(".composer-under-bar")
    expect(under_bar).to_be_visible(timeout=15000)

    page.set_viewport_size({"width": 1200, "height": 848})
    expect(under_bar).to_be_visible()
    wait_for(
        lambda: _chat_frame(page).eval_on_selector(
            ".composer-under-bar", "e => e.getBoundingClientRect().bottom <= window.innerHeight"
        ),
        timeout=10.0,
        error_message="the composer's model bar stayed below the bottom of the shortened window",
    )


_TOOL_CALL_SESSION_EVENTS: list[dict[str, Any]] = [
    {
        "type": "user",
        "uuid": "uuid-tc-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "Read test.txt"},
    },
    {
        "type": "assistant",
        "uuid": "uuid-tc-2",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {"type": "text", "text": "Let me read that file."},
                {"type": "tool_use", "id": "toolu_tc1", "name": "Read", "input": {"file": "test.txt"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    },
    {
        "type": "user",
        "uuid": "uuid-tc-3",
        "timestamp": "2026-01-01T00:00:02Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_tc1", "content": "file contents here"}],
        },
    },
]


@pytest.mark.timeout(60, func_only=False)
def test_tool_calls_render_as_collapsible(tmp_path: Path, page: Page) -> None:
    """Tool calls render as collapsible blocks that expand to show input/output."""
    with _running_e2e_server(tmp_path, session_events=_TOOL_CALL_SESSION_EVENTS) as server:
        page.goto(server.shell_url)
        _open_fixture_chat(page)

        expect(_chat(page).locator(".message-assistant").first).to_be_visible(timeout=15000)
        tool_block = _chat(page).locator(".tool-call-block").first
        expect(tool_block).to_be_visible(timeout=10000)
        expect(tool_block).to_contain_text("Read")

        tool_details = _chat(page).locator(".tool-call-details").first
        expect(tool_details).to_be_hidden()
        _chat(page).locator(".tool-call-header").first.click()
        expect(tool_details).to_be_visible()
        expect(tool_details).to_contain_text("file contents here")


@pytest.mark.timeout(60, func_only=False)
def test_live_stream_delivers_new_events(e2e_server: RunningWorkspace, page: Page) -> None:
    """New events written to the session file appear in the UI as they stream in."""
    page.goto(e2e_server.shell_url)
    _open_fixture_chat(page)
    expect(_chat(page).locator(".message-user").first).to_be_visible(timeout=15000)

    new_event = {
        "type": "user",
        "uuid": "uuid-new-1",
        "timestamp": "2026-01-01T00:01:00Z",
        "message": {"role": "user", "content": "This is a new streamed message!"},
    }
    with open(e2e_server.session_file, "a") as f:
        f.write(json.dumps(new_event) + "\n")

    expect(_chat(page).locator(".message-user", has_text="This is a new streamed message!")).to_be_visible(
        timeout=10000
    )


# A conversation whose transcript ends with an unresolved enqueue, so the Claude queue
# populator surfaces one currently-queued message while a turn is in flight.
_QUEUED_SESSION_EVENTS: list[dict[str, Any]] = [
    {
        "type": "user",
        "uuid": "uuid-q-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "Kick off the big refactor"},
    },
    {
        "type": "assistant",
        "uuid": "uuid-q-2",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "On it -- starting now."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 4},
        },
    },
    {
        "type": "user",
        "uuid": "uuid-q-3",
        "timestamp": "2026-01-01T00:00:03Z",
        "message": {"role": "user", "content": "Now run the tests"},
    },
    {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-01-01T00:00:05Z",
        "sessionId": "e2e-session-001",
        "content": "actually also update the changelog",
    },
]


@pytest.mark.timeout(60, func_only=False)
def test_queued_message_group_renders_with_actions(tmp_path: Path, page: Page) -> None:
    """A harness-queued message renders as a distinct group with the shoulder-tap action."""
    with _running_e2e_server(tmp_path, session_events=_QUEUED_SESSION_EVENTS) as server:
        page.goto(server.shell_url)
        _open_fixture_chat(page)

        expect(_chat(page).locator(".message-user", has_text="Kick off the big refactor").first).to_be_visible(
            timeout=15000
        )
        group = _chat(page).locator(".queued-group")
        expect(group).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".queued-message .message-user-bubble .message-content")).to_contain_text(
            "actually also update the changelog"
        )
        expect(_chat(page).locator(".queued-header-label")).to_contain_text("Queued messages")
        flush_button = _chat(page).locator(".queued-action--flush")
        expect(flush_button).to_be_visible()
        expect(flush_button).to_contain_text("Shoulder tap")
        expect(_chat(page).locator(".queued-action--interrupt")).to_have_count(0)


@pytest.mark.timeout(60, func_only=False)
def test_chat_recovers_from_a_failed_transcript_load(tmp_path: Path, page: Page) -> None:
    """A chat whose transcript fetch failed recovers on Refresh, without reloading the page."""
    with _running_e2e_server(tmp_path) as server:
        events_url = "**/api/agents/*/events"
        page.route(
            events_url,
            lambda route: route.fulfill(status=503, content_type="text/plain", body="Backend not yet available"),
        )
        page.goto(server.shell_url)
        _open_from_launcher(page, FIXTURE_CHAT_ADDRESS)

        error = _chat(page).locator(".message-list-error")
        expect(error).to_be_visible(timeout=15000)
        expect(error.locator("p")).to_have_text("Error: request failed (HTTP 503)")

        page.unroute(events_url)
        error.locator(".message-list-reload").click()

        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-error")).to_have_count(0)


# ---------- layout ops ----------


def _make_long_conversation_events(pair_count: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for i in range(pair_count):
        events.append(
            {
                "type": "user",
                "uuid": f"long-u-{i}",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": f"msg-{i}"},
            }
        )
        events.append(
            {
                "type": "assistant",
                "uuid": f"long-a-{i}",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": f"reply-{i}"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
    return events


def _visible_user_messages(page: Page) -> list[str]:
    return _chat_frame(page).evaluate(
        "() => Array.from(document.querySelectorAll('.message-user')).map((e) => (e.textContent || '').trim())"
    )


def _min_message_index(messages: list[str]) -> int:
    indices = [int(m[len("msg-") :]) for m in messages if m.startswith("msg-") and m[len("msg-") :].isdigit()]
    return min(indices) if indices else -1


@pytest.mark.timeout(120, func_only=False)
def test_hidden_tab_preserves_scroll_window(tmp_path: Path, page: Page) -> None:
    """Hiding a chat tab (and showing it again) must not move its loaded window.

    Regression test for the scroll-jump bug: an inactive tab stays mounted while hidden with
    ``display: none``, its scroll element reports every metric as 0, and the paging logic
    used to map that to a jump to the very start of the conversation.
    """
    events = _make_long_conversation_events(150)
    probe = _stub_address("stub-1")
    with _running_e2e_server(
        tmp_path, session_events=events, is_stub_app_offered=True, stub_instances=("stub-1",)
    ) as server:
        _serve_stub_pages(page, server)
        page.goto(server.shell_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.scrollHeight > el.clientHeight * 2; }",
            timeout=15000,
        )

        # A sibling tab in the SAME group, so hiding the chat is a pure tab switch. The shell edits the
        # client's saved arrangement, so the chat the browser just opened has to be saved first.
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=FIXTURE_CHAT_ADDRESS)
        _broadcast_layout_op(server.shell_url, "open", {"address": probe, "new_group": True})
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)
        _broadcast_layout_op(
            server.shell_url,
            "move",
            {"address": probe, "relative_to": FIXTURE_CHAT_ADDRESS, "direction": "within"},
        )
        page.wait_for_function(
            "() => document.querySelectorAll('.dv-groupview').length === 1", timeout=_TRIGGER_TIMEOUT_MS
        )
        _broadcast_layout_op(server.shell_url, "focus", {"address": FIXTURE_CHAT_ADDRESS})
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)

        _chat_frame(page).evaluate(
            "() => { const el = document.querySelector('.app-content'); el.scrollTop = el.scrollHeight - el.clientHeight - 1500; }"
        )
        page.wait_for_timeout(1000)
        before_hidden = _visible_user_messages(page)
        scroll_top_before = _chat_frame(page).evaluate("() => document.querySelector('.app-content').scrollTop")
        assert before_hidden, "expected user messages to be rendered after scrolling up"
        assert "msg-0" not in before_hidden, f"setup should not be at the start: {before_hidden[:3]}"
        anchor_message = before_hidden[0]
        assert _min_message_index(before_hidden) >= 50, f"setup should be reading mid-history: {before_hidden[:3]}"

        _broadcast_layout_op(server.shell_url, "focus", {"address": probe})
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight === 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )

        with open(server.session_file, "a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "long-u-streamed",
                        "timestamp": "2026-01-01T00:02:00Z",
                        "message": {"role": "user", "content": "streamed-while-hidden"},
                    }
                )
                + "\n"
            )
        page.wait_for_timeout(3000)

        during_hidden = _visible_user_messages(page)
        assert anchor_message in during_hidden, (
            f"hidden tab lost its place: anchor {anchor_message!r} no longer rendered ({during_hidden[:3]}...)"
        )
        assert "msg-0" not in during_hidden, f"hidden tab jumped to the start of the conversation: {during_hidden[:3]}"

        _broadcast_layout_op(server.shell_url, "focus", {"address": FIXTURE_CHAT_ADDRESS})
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)
        after_restore = _visible_user_messages(page)
        scroll_top_after = _chat_frame(page).evaluate("() => document.querySelector('.app-content').scrollTop")
        assert "msg-0" not in after_restore, (
            f"after showing the tab again the window jumped to the start: {after_restore[:3]}"
        )
        assert anchor_message in after_restore, (
            f"after showing the tab again the reader was not returned to their place: {after_restore[:3]}"
        )
        assert abs(scroll_top_after - scroll_top_before) < 50, (
            f"scroll position drifted across hide/show: {scroll_top_before} -> {scroll_top_after}"
        )


# ---------- projects and views ----------


@pytest.mark.timeout(120, func_only=False)
def test_switching_views_preserves_chat_transcript(tmp_path: Path, page: Page) -> None:
    """A chat pane restored by a view switch still shows its own transcript.

    Everything lists the machine's agent whatever project shows it, so opening it there
    leaves it open in the starter project too. Switching back restores the starter
    project's layout, whose panel must bind to the same instance.
    """
    with _running_e2e_server(tmp_path) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.shell_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=FIXTURE_CHAT_ADDRESS)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        _open_from_launcher(page, FIXTURE_CHAT_ADDRESS)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, EVERYTHING_VIEW_ID, containing=FIXTURE_CHAT_ADDRESS)

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-empty")).to_have_count(0)
        expect(_chat(page).locator(".message-list-not-found")).to_have_count(0)


# ---------- starting a chat ----------


@pytest.mark.timeout(120, func_only=False)
def test_a_new_chat_with_nothing_signed_in_offers_the_provider_chooser_in_its_own_tab(
    tmp_path: Path, page: Page
) -> None:
    """The tab opens either way: with no account the chat waits for one, and its page shows the
    chooser, so signing in happens where the chat will be rather than on the shell."""
    with _running_e2e_server(tmp_path, is_account_signed_in=False) as server:
        page.goto(server.shell_url)
        chat = _start_new_chat(page)
        expect(chat.locator(".message-list-awaiting-account")).to_contain_text(
            "Sign in to a provider to start this chat", timeout=15000
        )
        expect(chat.locator('[data-e2e="provider-chooser"]')).to_be_visible(timeout=10000)
        # The shell itself renders no chooser: the sign-in lives in the chat's page.
        assert page.locator('[data-e2e="provider-chooser"]').count() == 0
        # The chat is listed as an instance waiting on the user, so the tab is back on reload
        # (once the arrangement holding it has been saved).
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing="app:chat?instance=")
        page.reload()
        chat = page.frame_locator('iframe[data-address^="app:chat?instance="]')
        expect(chat.locator(".message-list-awaiting-account")).to_be_visible(timeout=15000)


@pytest.mark.timeout(120, func_only=False)
def test_a_new_chat_with_an_account_starts_at_once_and_shows_its_composer_when_it_lands(
    tmp_path: Path, page: Page
) -> None:
    """With an account signed in the create runs immediately on it; the page says so while the
    create runs (no creation log), and the composer arrives when the agent registers."""
    with _running_e2e_server(tmp_path) as server:
        page.goto(server.shell_url)
        chat = _start_new_chat(page)
        expect(chat.locator(".message-list-creating")).to_contain_text("Starting the chat", timeout=15000)
        expect(chat.locator(".message-input-textbox")).to_be_visible(timeout=15000)
        expect(chat.locator(".message-list-creating")).to_have_count(0, timeout=15000)
        assert chat.locator('[data-e2e="provider-chooser"]').count() == 0


@pytest.mark.timeout(120, func_only=False)
def test_a_create_that_fails_keeps_the_tab_with_the_reason_and_a_retry(
    tmp_path: Path, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``mngr create`` is a notice in the chat's own tab, with what mngr printed and a
    "Try again" on the same account, not a tab that vanishes."""
    monkeypatch.setenv("FAKE_MNGR_CREATE_EXIT_CODE", "3")
    with _running_e2e_server(tmp_path) as server:
        page.goto(server.shell_url)
        chat = _start_new_chat(page)
        failed = chat.locator(".message-list-create-failed")
        expect(failed).to_contain_text("This chat could not be started", timeout=20000)
        expect(failed).to_contain_text("exited with code 3")
        expect(failed).to_contain_text("create failed on purpose")
        expect(failed.locator(".message-list-create-retry")).to_be_visible()
        # The instance stays listed, in the error state, so the tab survives a reload.
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing="app:chat?instance=")
        page.reload()
        chat = page.frame_locator('iframe[data-address^="app:chat?instance="]')
        expect(chat.locator(".message-list-create-failed")).to_be_visible(timeout=15000)
