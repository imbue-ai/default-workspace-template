"""End-to-end tests for System Interface using Playwright.

These tests start a real Flask server (threaded Werkzeug) with mocked agent
discovery, then use Playwright to interact with the web UI.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator
from unittest.mock import patch

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import AppEntry
from imbue.system_interface.projects import DEFAULT_PROJECT_ID
from imbue.system_interface.projects import DEFAULT_PROJECT_NAME
from imbue.system_interface.projects import EVERYTHING_VIEW_ID
from imbue.system_interface.projects import EVERYTHING_VIEW_NAME
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

try:
    from playwright.sync_api import Frame
    from playwright.sync_api import FrameLocator
    from playwright.sync_api import Page
    from playwright.sync_api import expect

    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    _PLAYWRIGHT_IMPORTABLE = False


def _playwright_browsers_installed() -> bool:
    """Check whether a launchable browser is present (Fortress or Playwright's cache)."""
    if not _PLAYWRIGHT_IMPORTABLE:
        return False
    return is_e2e_browser_installed()


def _frontend_built() -> bool:
    """Check whether the frontend has been built (``static/index.html`` exists).

    Without a build the Flask server serves a "Frontend not built" placeholder, so
    every e2e test would ``page.goto()`` and then burn its per-test timeout waiting
    for selectors that can never appear -- and because this project sets
    ``timeout_func_only = true``, a stuck browser launch/fixture is unbounded. The
    path is resolved relative to this test module (``imbue/system_interface/`` holds
    both this file and the build output) so it holds regardless of the cwd.
    """
    return (Path(__file__).parent / "static" / "index.html").is_file()


# Every test here loads the workspace, and mounting a view re-reads the machine
# so the project rail and the New Tab launcher can list its terminals -- which
# shells out to ``tmux list-sessions`` server-side. The resource_guards plugin
# fails any unmarked test that reaches tmux, so the mark belongs on the module
# rather than on the handful of tests that also create a session.
pytestmark = [
    pytest.mark.release,
    pytest.mark.tmux,
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontend_built(),
        reason=(
            "System interface frontend not built "
            "(run `cd system/apps/system_interface/frontend && npm run build`); skipping e2e."
        ),
    ),
]

_PORT = 18765
_BASE_URL = f"http://127.0.0.1:{_PORT}"

# The fixture chat's agent id (``_make_agent_fixture``'s default).
_FIXTURE_AGENT_ID = "agent-test-123"


def _chat(page: Page, agent_id: str = _FIXTURE_AGENT_ID) -> FrameLocator:
    """The chat's page, framed at the chat origin (phase 6 of the workspace app model).

    Every chat assertion goes through it: the shell document holds no chat markup any
    more, only the iframe filed under the chat's live key.
    """
    return page.frame_locator(f'iframe[data-live-key="chat:{agent_id}"]')


def _chat_frame(page: Page, agent_id: str = _FIXTURE_AGENT_ID) -> Frame:
    """The chat page's own frame, for the evaluate and wait calls that need its document.

    Polled through Playwright's own wait rather than ``wait_for``: the sync API only
    processes the events that attach a frame while a Playwright call is running, so a
    ``time.sleep`` loop would never see the frame arrive.
    """
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        for frame in page.frames:
            if frame.url.rstrip("/").endswith(f"/{agent_id}"):
                return frame
        page.wait_for_timeout(100)
    raise TimeoutError(f"the chat page for {agent_id} never loaded in a frame")


def _make_session_file(
    projects_dir: Path,
    session_id: str,
    events: list[dict[str, Any]],
) -> Path:
    """Create a session JSONL file with the given events."""
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _make_agent_fixture(
    tmp_path: Path,
    agent_id: str = "agent-test-123",
    agent_name: str = "test-agent",
    session_events: list[dict[str, Any]] | None = None,
) -> tuple[AgentInfo, Path]:
    """Set up a mock agent with session files. Returns (agent_info, session_file_path)."""
    agent_state_dir = tmp_path / "agents" / agent_id
    agent_state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    session_id = "e2e-session-001"
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")
    # The session endpoint (_find_agent) resolves an agent's CLAUDE_CONFIG_DIR
    # from this per-agent env file (step 1 of read_claude_config_dir_from_env_file),
    # so pin it at the fixture's config dir. Without this the watcher falls back to
    # the real ~/.claude and the fixture transcript never loads.
    (agent_state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={claude_config_dir}\n")

    if session_events is None:
        session_events = [
            {
                "type": "user",
                "uuid": "uuid-e2e-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello agent!"},
            },
            {
                "type": "assistant",
                "uuid": "uuid-e2e-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hello! How can I help you?"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 8},
                },
            },
        ]

    session_file = _make_session_file(projects_dir, session_id, session_events)

    agent_info = AgentInfo(
        id=agent_id,
        name=agent_name,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    return agent_info, session_file


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    port: int,
    session_events: list[dict[str, Any]] | None = None,
    primary_agent_id: str = "",
    additional_agents: tuple[tuple[str, str], ...] = (),
    apps: tuple[str, ...] = (),
) -> Generator[tuple[str, AgentInfo, Path], None, None]:
    """Run the web server with a mock primary agent (plus any ``additional_agents``), ready for Playwright + layout ops.

    Yields ``(base_url, agent_info, session_file)``. Shared by the default
    ``e2e_server`` fixture and any test that needs a bespoke conversation
    (e.g. a long transcript) or a distinct port.

    ``primary_agent_id`` controls layout persistence: empty (the default)
    clears MNGR_AGENT_ID so the layout endpoints have no primary-agent dir
    (nothing persists; the UI auto-opens the fixture chat); a non-empty id
    persists named layouts under ``tmp_path/agents/<id>/workspace_layout``.

    ``apps`` names the workspace services the machine offers. Each is seeded
    onto the manager's registry, exactly as reading ``apps.toml`` would leave
    it, so the rail's "All apps" list and its shortcuts have something real to
    show; the manager is never started here, so nothing would otherwise read
    that file. The service label is derived from the name, as
    ``forward_port.py`` mints it, because every panel origin is built from the
    label rather than the name.

    ``additional_agents`` is a tuple of ``(agent_id, agent_name)`` for extra
    agents that EXIST but whose chats are not auto-opened. They carry no
    transcript -- a bare state dir plus a manager entry is enough to surface them
    in the WebSocket agents snapshot the New Tab launcher enumerates. Used to
    exercise the launcher's agent-discovery path (nothing else opens their chat).
    """
    base_url = f"http://127.0.0.1:{port}"
    agent_info, session_file = _make_agent_fixture(tmp_path, session_events=session_events)
    extra_infos: list[AgentInfo] = []
    for extra_id, extra_name in additional_agents:
        extra_state_dir = tmp_path / "agents" / extra_id
        extra_state_dir.mkdir(parents=True, exist_ok=True)
        extra_infos.append(
            AgentInfo(
                id=extra_id,
                name=extra_name,
                state="RUNNING",
                agent_state_dir=extra_state_dir,
                claude_config_dir=agent_info.claude_config_dir,
            )
        )
    # The primary agent plus any extras, handled uniformly from here on.
    agents = [agent_info, *extra_infos]

    # A fake logged-in `claude` on PATH: the workspace UI auto-opens the
    # sign-in modal whenever the status probe reports logged-out (as it
    # would here, with no real claude binary), and the modal's overlay
    # would then swallow every click these layout tests make.
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir(exist_ok=True)
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(
        '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}\'\n'
    )
    fake_claude.chmod(0o755)

    # A fake `mngr` for the paths that shell out to it. Renaming a chat renames
    # its mngr agent before the workspace shows the new name, and the fixture's
    # agents are injected fakes with no real mngr behind them -- without this a
    # rename fails and the tab keeps its old name. Exiting 0 stands in for
    # "mngr accepted the rename"; the failure policy itself is unit-tested.
    fake_mngr = fake_bin_dir / "mngr"
    fake_mngr.write_text("#!/bin/sh\nexit 0\n")
    fake_mngr.chmod(0o755)

    # Isolate the workspace environment: point MNGR_HOST_DIR at the fixture's
    # tmp tree so the session endpoint (_find_agent) resolves the fixture agent's
    # state dir + env file, and set MNGR_AGENT_ID per ``primary_agent_id`` so
    # the layout endpoints either run unpersisted or write under the tmp tree --
    # never the real workspace's layout state. This overrides the autouse
    # _isolate_system_interface_tests fixture's env for the duration of the test.
    with (
        patch.dict(
            os.environ,
            {
                "MNGR_HOST_DIR": str(tmp_path),
                "MNGR_AGENT_ID": primary_agent_id,
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "MINDS_ACCOUNTS_ROOT": str(tmp_path / "accounts"),
            },
        ),
        patch("imbue.system_interface.server.discover_agents", return_value=agents),
    ):
        # Seed the agent into a manager and inject it; the manager is never started,
        # so no background mngr discovery runs. Its messenger is a recording fake so
        # message sends succeed without contacting mngr. The UI renders its agent
        # list from the WebSocket agents_updated snapshot, which the server sends
        # from this manager on connect.
        # A signed-in provider. Creating a chat -- including the starter chat a new project
        # gets -- opens the chooser instead when there is none, which is correct behaviour and
        # would leave a modal over the rail these tests click through.
        account_id, _ = mint_account_dir()
        commit_account(account_id, "anthropic", "Anthropic")

        broadcaster = WebSocketBroadcaster()
        manager = AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())
        with manager._lock:
            for info in agents:
                manager._agents[info.id] = AgentStateItem(
                    id=info.id,
                    name=info.name,
                    state="RUNNING",
                    labels={},
                    work_dir=str(tmp_path / "work"),
                )
        for info in agents:
            manager._ensure_activity_tracking(info.id)
        manager._apps = [
            AppEntry(name=name, url=f"http://127.0.0.1:9{index:03d}", label=f"{name}-e2elabel")
            for index, name in enumerate(apps)
        ]

        config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
        app = create_application(build_test_state(config=config, agent_manager=manager))

        server = make_threaded_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        # Wait for server to start
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{base_url}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            yield base_url, agent_info, session_file
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[tuple[str, list[AgentInfo], Path], None, None]:
    """Start the web server with mock agents for e2e testing."""
    with _running_e2e_server(tmp_path, _PORT) as (base_url, agent_info, session_file):
        yield base_url, [agent_info], session_file


@pytest.mark.timeout(30, func_only=False)
def test_page_loads_and_shows_title(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The page loads, boots the workspace, and shows the app title.

    Every other test here waits on rendered content, which keeps them inside the
    view mount; this one asserts on the document alone, so it waits explicitly
    for the fleet listing that mounting a view issues. That makes its tmux use
    (the module's mark) deterministic instead of a race against teardown.
    """
    base_url, _, _ = e2e_server
    with page.expect_response(lambda response: response.url.endswith("/api/terminals"), timeout=15000):
        page.goto(base_url)
    expect(page).to_have_title("System Interface")


@pytest.mark.timeout(30, func_only=False)
def test_chat_transcript_area_is_pure_white(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The chat conversation panel renders on a pure-white background.

    Regression test for making the chat area exactly ``#ffffff`` -- both the
    transcript (``.app-content``) and the composer footer strip (``.app-footer``),
    which were previously the shared off-white ``--color-bg``. The change is
    scoped to the chat panel via the dedicated ``--color-bg-chat`` token: the
    shared shell background token ``--color-bg`` must stay off-white, so this also
    guards against a future edit whitening the whole shell via the shared variable.
    """
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # The transcript container must exist and actually hold the message list, so
    # the assertion below cannot pass against an empty or wrong tree.
    content = _chat(page).locator(".app-content")
    expect(content).to_be_visible(timeout=15000)
    expect(content.locator(".message-list")).to_have_count(1)

    content_bg = _chat_frame(page).eval_on_selector(".app-content", "e => getComputedStyle(e).backgroundColor")
    assert content_bg == "rgb(255, 255, 255)", f"chat transcript area should be pure white, got {content_bg}"

    # The composer footer strip is now unified with the transcript -- also pure white.
    footer_bg = _chat_frame(page).eval_on_selector(".app-footer", "e => getComputedStyle(e).backgroundColor")
    assert footer_bg == "rgb(255, 255, 255)", f"composer footer should be pure white, got {footer_bg}"

    # Scoping guard: the whitening went through --color-bg-chat, so the shared
    # shell background token must stay off-white (the tab bar / other panels rely
    # on it). This catches a future edit that whitens the whole shell instead.
    shell_bg = page.eval_on_selector("html", "e => getComputedStyle(e).getPropertyValue('--color-bg').trim()")
    assert shell_bg not in ("#ffffff", "#fff", "rgb(255, 255, 255)"), (
        f"shared shell --color-bg should stay off-white, got {shell_bg}"
    )


# Marked flaky on an UNIDENTIFIED cause: it failed once inside a full-suite run
# and then passed three times out of three on its own, with and without the
# change that was in the tree at the time. That is the signature of the launcher
# 's terminal-fleet fetch -- which shells out to ``tmux list-sessions``
# server-side -- racing something under parallel load rather than of a wrong
# assertion, but the actual race has not been found, so this mark buys retries
# and does not claim to be a fix. (The tmux mark that accompanied this one lives
# on the module now, since every test here reaches the same shell-out.)
@pytest.mark.flaky
@pytest.mark.timeout(120, func_only=False)
def test_new_tab_launcher_lists_unopened_agent(tmp_path: Path, page: Page) -> None:
    """The New Tab launcher lists agents that exist but have no open chat.

    The single-dockview UI replaced the old agent sidebar: the primary agent's
    chat auto-opens as a tab, and every OTHER discoverable agent is reachable
    from the "+", which now opens a full-page New Tab launcher instead of the
    old dropdown. The launcher enumerates the whole machine, so an agent this
    view does not show yet lands in its "On this machine" table -- opening it
    from there files it into the active project as well. This is where the
    sidebar's "list the available agents" behavior now lives, so we assert an
    unopened agent shows up there as an openable row.
    """
    with _running_e2e_server(tmp_path, _PORT + 3, additional_agents=(("agent-other-999", "other-agent"),)) as (
        base_url,
        _,
        _,
    ):
        page.goto(base_url)
        # The primary agent's chat auto-opens; the extra agent stays closed.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        # Open the "+" (a launcher tab) and confirm the unopened agent is offered.
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        other_item = page.locator(
            ".new-tab-launcher-section[data-section='on-machine'] .new-tab-launcher-row", has_text="other-agent"
        )
        expect(other_item).to_have_count(1, timeout=10000)
        expect(other_item).to_be_visible()


@pytest.mark.timeout(30, func_only=False)
def test_chat_tab_shows_agent_liveness(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The chat tab shows the agent's lifecycle state via its liveness dot.

    Replaces the old sidebar state label. Each chat tab carries a process dot
    whose ``data-liveness`` / ``data-lifecycle-state`` attributes track the
    agent's effective lifecycle state. The fixture's transcript ends with an
    ``end_turn`` assistant message, so the live agent is idle and the dot resolves
    to ``waiting``/``WAITING`` -- proving the dot reflects the real activity state
    rather than a hard-coded value.
    """
    base_url, _, _ = e2e_server
    page.goto(base_url)
    expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

    dot = page.locator(".dv-tab-process-dot").first
    expect(dot).to_have_attribute("data-lifecycle-state", "WAITING", timeout=15000)
    expect(dot).to_have_attribute("data-liveness", "waiting")


@pytest.mark.timeout(30, func_only=False)
def test_agent_chat_shows_conversation(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The auto-opened chat shows the agent's conversation history."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # The chat for the primary agent auto-opens, so its first user message renders.
    user_message = _chat(page).locator(".message-user")
    expect(user_message.first).to_be_visible(timeout=15000)
    expect(user_message.first).to_contain_text("Hello agent!")


@pytest.mark.timeout(30, func_only=False)
def test_assistant_message_renders(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Assistant messages render with markdown content."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    assistant_message = _chat(page).locator(".message-assistant")
    expect(assistant_message.first).to_be_visible(timeout=15000)
    expect(assistant_message.first).to_contain_text("Hello! How can I help you?")


@pytest.mark.timeout(30, func_only=False)
def test_chat_tab_shows_agent_name(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The chat tab is titled with the agent's name (the old header's role)."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    tab = page.locator(".dv-default-tab-content", has_text="test-agent").first
    expect(tab).to_be_visible(timeout=15000)
    expect(tab).to_have_text("test-agent")


@pytest.mark.timeout(30, func_only=False)
def test_message_input_visible(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The message composer is visible in the open chat."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = _chat(page).locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=15000)


@pytest.mark.timeout(30, func_only=False)
def test_send_button_appears_on_input(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The send button appears only once the composer has text."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = _chat(page).locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=15000)

    # The send button is not rendered until the composer can send (non-empty).
    send_button = _chat(page).locator(".message-input-send-button")
    expect(send_button).to_have_count(0)

    # Type some text -- the send button now appears.
    textarea.fill("test message")
    expect(send_button).to_be_visible()


@pytest.mark.timeout(60, func_only=False)
def test_composer_bar_survives_a_shorter_window(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """A window that gets shorter keeps the whole composer on screen.

    The minds shell does this without the user touching the window: its recovery
    band takes ~50px off the top of the frame this app runs in. Everything below
    the dock is positioned in pixels -- the panes, and the live surfaces
    mirroring them -- so a row that grows with the viewport but cannot shrink
    back leaves the chat laid out at the old height, with the model bar under
    the composer hanging below the bottom edge where nothing can bring it back.
    Asserted against the viewport rather than against a pixel height: what
    matters is that the bar is inside the window, whatever the window is.
    """
    base_url, _, _ = e2e_server
    page.set_viewport_size({"width": 1200, "height": 900})
    # Waited on so the view is fully mounted before the window changes size, and
    # so the module's tmux mark is honored (mounting is what shells out to it).
    with page.expect_response(lambda response: response.url.endswith("/api/terminals"), timeout=15000):
        page.goto(base_url)

    under_bar = _chat(page).locator(".composer-under-bar")
    expect(under_bar).to_be_visible(timeout=15000)

    page.set_viewport_size({"width": 1200, "height": 848})
    expect(under_bar).to_be_visible()
    # The dock relays out on a resize observation and the surfaces follow on the
    # next frame, so the settled geometry is what is asserted -- polled rather
    # than slept on. The wrong layout is stable, not slow: it never settles.
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
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_tc1", "content": "file contents here"},
            ],
        },
    },
]


@pytest.mark.timeout(30, func_only=False)
def test_tool_calls_render_as_collapsible(tmp_path: Path, page: Page) -> None:
    """Tool calls render as collapsible blocks that expand to show input/output.

    A ``.tool-call-block`` renders collapsed (its ``.tool-call-details`` are
    ``display: none``); clicking the ``.tool-call-header`` toggles the
    ``--expanded`` class so the details -- the tool result -- become visible.
    """
    with _running_e2e_server(tmp_path, _PORT + 1, session_events=_TOOL_CALL_SESSION_EVENTS) as (base_url, _, _):
        page.goto(base_url)

        # Wait for the assistant turn (which carries the tool call) to render.
        expect(_chat(page).locator(".message-assistant").first).to_be_visible(timeout=15000)

        tool_block = _chat(page).locator(".tool-call-block").first
        expect(tool_block).to_be_visible(timeout=10000)
        # The header names the tool.
        expect(tool_block).to_contain_text("Read")

        # Collapsed by default: the details are not visible until expanded.
        tool_details = _chat(page).locator(".tool-call-details").first
        expect(tool_details).to_be_hidden()

        # Clicking the header expands the block, revealing the tool result.
        _chat(page).locator(".tool-call-header").first.click()
        expect(tool_details).to_be_visible()
        expect(tool_details).to_contain_text("file contents here")


@pytest.mark.timeout(30, func_only=False)
def test_live_stream_delivers_new_events(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """New events written to the session file appear in the UI as they stream in."""
    base_url, _, session_file = e2e_server
    page.goto(base_url)

    # Wait for initial content
    expect(_chat(page).locator(".message-user").first).to_be_visible(timeout=15000)

    # Append a new event to the session file
    new_event = {
        "type": "user",
        "uuid": "uuid-new-1",
        "timestamp": "2026-01-01T00:01:00Z",
        "message": {"role": "user", "content": "This is a new streamed message!"},
    }
    with open(session_file, "a") as f:
        f.write(json.dumps(new_event) + "\n")

    # Wait for the new message to appear (watcher polls every 1 second)
    new_message = _chat(page).locator(".message-user", has_text="This is a new streamed message!")
    expect(new_message).to_be_visible(timeout=10000)


_TRIGGER_TIMEOUT_MS = 20000


def _broadcast_layout_op(base_url: str, op: str, args: dict[str, Any], agent_id: str) -> None:
    """POST a layout op to the loopback ``/api/layout/broadcast`` endpoint.

    This is the same path ``system/scripts/layout.py`` drives, so issuing a ``split``
    here exercises the real frontend ``handleSplit`` handler (which carves the
    second group) rather than reaching into dockview internals from the test.

    Mutating ops are layout-targeted, and a client reports its active *view* as
    its active layout, so they carry the starter project -- the one a fresh
    browser lands on, since Everything is reached by choosing it rather than by
    default. They only succeed once the page's ``client_state`` registration has
    landed, so a 412 is retried until it catches up.
    """
    payload = json.dumps({"op": op, "args": {**args, "layout": DEFAULT_PROJECT_NAME}, "agent_id": agent_id}).encode()
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
            raise
        except (TimeoutError, urllib.error.URLError):
            # Not reaching the server is exactly what this loop is for, but only
            # HTTPError was being caught -- so a request that timed out under
            # load escaped the retry and failed the calling test outright,
            # despite `wait_for` still having seconds left to spend. A server
            # that never answers still fails, with this helper's own message
            # rather than a raw socket traceback.
            return False

    wait_for(
        _attempt,
        timeout=15.0,
        poll_interval=0.2,
        error_message=f"layout broadcast for op {op!r} never succeeded (client registration missing?)",
    )


# Picks the launcher's "Terminal" tile, which spawns a real tmux session (on top
# of the fleet listing every page load already does -- see the module's mark).
@pytest.mark.timeout(120, func_only=False)
def test_new_tab_opens_in_clicked_split(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The header "+" opens the new tab in the split whose header was clicked.

    Regression test for the bug where clicking "+" in a right-hand split opened
    the tab in the (active) left split instead. We split the layout into two
    groups, make the LEFT group active (so dockview's default "add to the
    active group" would land a new tab on the left), then click the RIGHT
    split's "+" and add a URL tab. It must land in the RIGHT split.
    """
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # The fixture auto-opens the chat for "test-agent" as the sole group.
    expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(
        timeout=_TRIGGER_TIMEOUT_MS
    )
    add_buttons = page.locator(".dockview-add-tab-button")
    expect(add_buttons).to_have_count(1)

    # Carve a second group to the right of the chat by opening a URL iframe in
    # a fresh column. Driven through the real layout-op broadcast path.
    _broadcast_layout_op(
        base_url,
        "split",
        {
            "ref": "https://placement-split.example/",
            "relative_to": "chat:test-agent",
            "direction": "right",
            "new_group": True,
        },
        agent_id="agent-test-123",
    )

    # Two groups now, each header carrying its own "+".
    expect(add_buttons).to_have_count(2, timeout=10000)
    expect(page.locator(".dv-default-tab-content", has_text="placement-split.example").first).to_be_visible(
        timeout=10000
    )

    # Activate the LEFT (chat) split. Without the fix, the new tab would follow
    # the active group and wrongly land here.
    chat_tab = page.locator(".dv-default-tab-content", has_text="test-agent").first
    chat_tab.click()
    left_group = page.locator(".dv-groupview", has=page.locator(".dv-default-tab-content", has_text="test-agent"))
    expect(left_group).to_have_class(re.compile(r"\bdv-active-group\b"))

    # Click the "+" in the RIGHT split's header (the geometrically rightmost one).
    boxes = [add_buttons.nth(i).bounding_box() for i in range(2)]
    assert boxes[0] is not None and boxes[1] is not None
    right_index = 0 if boxes[0]["x"] > boxes[1]["x"] else 1
    add_buttons.nth(right_index).click()

    # Pick "Terminal" from the launcher the "+" opened in the right split. The
    # old "New URL" dropdown item this test used is gone twice over -- "New
    # browser" replaced the ad-hoc-URL flow, and the launcher replaced the
    # dropdown -- but a terminal opens through the SAME openIframeTab +
    # targetGroup placement path, so it still exercises clicked-split placement.
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
    page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()

    # The new tab must render in the RIGHT split, not the left, and must tab
    # into the existing right group rather than carving a third. Matched
    # case-insensitively: the tab opens as ``terminal-N`` and repaints to the
    # auto-filed "Terminal N" whenever that title write lands, and this test
    # cares about placement rather than which of the two names is up.
    expect(page.locator(".dv-default-tab-content", has_text="terminal").first).to_be_visible(timeout=10000)
    placement = page.evaluate(
        """
        (title) => {
          const groups = Array.from(document.querySelectorAll('.dv-groupview'))
            .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
          const has = (g) => Array.from(g.querySelectorAll('.dv-default-tab-content'))
            .some((e) => (e.textContent || '').toLowerCase().includes(title));
          return {
            count: groups.length,
            inLeft: groups.length > 0 ? has(groups[0]) : false,
            inRight: groups.length > 0 ? has(groups[groups.length - 1]) : false,
          };
        }
        """,
        "terminal",
    )
    assert placement["count"] == 2, f"new tab should join the right split, not create a third group: {placement}"
    assert placement["inRight"], f"new tab should be in the right split: {placement}"
    assert not placement["inLeft"], f"new tab leaked into the left split: {placement}"


def _make_long_conversation_events(pair_count: int) -> list[dict[str, Any]]:
    """Build ``pair_count`` user/assistant pairs with content ``msg-i`` / ``reply-i``.

    Each user message is uniquely identifiable so a test can tell which slice of
    the transcript the loaded window currently covers.
    """
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
    """Text of every rendered user-message bubble, in document order."""
    return _chat_frame(page).evaluate(
        "() => Array.from(document.querySelectorAll('.message-user')).map((e) => (e.textContent || '').trim())"
    )


def _min_message_index(messages: list[str]) -> int:
    """Smallest ``i`` among rendered ``msg-i`` bubbles (proxy for the window's top).

    A jump to the start of the conversation drags this toward 0; staying in
    history keeps it high. Non-``msg-i`` bubbles (e.g. the streamed marker) are
    ignored.
    """
    indices = [int(m[len("msg-") :]) for m in messages if m.startswith("msg-") and m[len("msg-") :].isdigit()]
    return min(indices) if indices else -1


@pytest.mark.timeout(120, func_only=False)
def test_hidden_tab_preserves_scroll_window(tmp_path: Path, page: Page) -> None:
    """Hiding a chat tab (and showing it again) must not move its loaded window.

    Regression test for the scroll-jump bug. Dockview is configured with
    ``defaultRenderer: "always"``, so an inactive tab stays mounted while an
    ancestor is hidden with ``display: none``; the ChatPanel keeps receiving
    global ``m.redraw()`` calls while hidden, but its scroll element then reports
    ``scrollTop``/``scrollHeight``/``clientHeight`` all as ``0``. Before the fix,
    ``maybePage()`` mapped that zero scroll position to event 0 and fired a JUMP
    that replaced the loaded window with the very start of the conversation -- so
    a user who had scrolled up to read history came back to the beginning.

    We load a long conversation, scroll up into the middle, hide the chat by
    maximizing a sibling panel while a new event streams in (forcing redraws
    while hidden), and assert the loaded window still covers the same place --
    both while hidden and after the tab is restored.
    """
    port = _PORT + 5
    # 150 pairs -> 300 events. The initial load holds only the tail 50, so the
    # first held offset (~250) is far larger than JUMP_GAP_EVENTS (120): exactly
    # the condition under which the hidden-redraw bug fired a jump to offset 0.
    events = _make_long_conversation_events(150)

    probe_url = "https://hidden-probe.example/"
    with _running_e2e_server(tmp_path, port, session_events=events) as (base_url, _, session_file):
        page.goto(base_url)
        _chat_frame(page).wait_for_selector(".message-list", timeout=15000)
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.scrollHeight > el.clientHeight * 2; }",
            timeout=15000,
        )

        # Put a second tab in the SAME dockview group as the chat, so hiding the
        # chat is a pure tab switch (no resize): open a URL in a fresh group, then
        # move it back into the chat's group as a sibling tab. This mirrors the
        # real "switch away from a chat tab and back" scenario and, unlike
        # maximize, leaves the chat at full width so its layout never reflows.
        _broadcast_layout_op(base_url, "open", {"ref": probe_url, "new_group": True}, agent_id="agent-test-123")
        expect(page.locator(".dv-default-tab-content", has_text="hidden-probe.example").first).to_be_visible(
            timeout=_TRIGGER_TIMEOUT_MS
        )
        _broadcast_layout_op(
            base_url,
            "move",
            {"ref": probe_url, "relative_to": "chat:test-agent", "direction": "within"},
            agent_id="agent-test-123",
        )
        # One group again (the URL tabbed in beside the chat).
        page.wait_for_function(
            "() => document.querySelectorAll('.dv-groupview').length === 1",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        # Make the chat the active tab and let its full-width layout settle.
        _broadcast_layout_op(base_url, "focus", {"ref": "chat:test-agent"}, agent_id="agent-test-123")
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)

        # Scroll up into the middle of the loaded window to read history (well off
        # the live tail, but not so far that a backfill to offset 0 is triggered).
        _chat_frame(page).evaluate(
            "() => { const el = document.querySelector('.app-content'); el.scrollTop = el.scrollHeight - el.clientHeight - 1500; }"
        )
        page.wait_for_timeout(1000)
        before_hidden = _visible_user_messages(page)
        scroll_top_before = _chat_frame(page).evaluate("() => document.querySelector('.app-content').scrollTop")
        # Sanity: we are reading history, not parked at the start or the tail.
        assert before_hidden, "expected user messages to be rendered after scrolling up"
        assert "msg-0" not in before_hidden, f"setup should not be at the start: {before_hidden[:3]}"
        anchor_message = before_hidden[0]
        assert _min_message_index(before_hidden) >= 50, f"setup should be reading mid-history: {before_hidden[:3]}"

        # Hide the chat by switching to the sibling tab.
        _broadcast_layout_op(base_url, "focus", {"ref": probe_url}, agent_id="agent-test-123")
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight === 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )

        # Stream a new event in while the chat is hidden -- this drives the global
        # redraws that, before the fix, corrupted the hidden panel's window.
        with open(session_file, "a") as handle:
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
        # Give the watcher (polls ~1s) time to deliver the event and fire redraws.
        page.wait_for_timeout(3000)

        # While hidden, the loaded window must not have jumped to the start: the
        # reader's anchor row is still rendered and event 0 is nowhere in sight.
        during_hidden = _visible_user_messages(page)
        assert anchor_message in during_hidden, (
            f"hidden tab lost its place: anchor {anchor_message!r} no longer rendered ({during_hidden[:3]}...)"
        )
        assert "msg-0" not in during_hidden, f"hidden tab jumped to the start of the conversation: {during_hidden[:3]}"

        # Show the chat tab again; the user must be exactly where they left off.
        _broadcast_layout_op(base_url, "focus", {"ref": "chat:test-agent"}, agent_id="agent-test-123")
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
        # The same anchor row is rendered again and -- because the tab switch never
        # resized the chat -- the native scroll position is preserved exactly (no
        # re-pin churn to a different offset).
        assert anchor_message in after_restore, (
            f"after showing the tab again the reader was not returned to their place: {after_restore[:3]}"
        )
        assert abs(scroll_top_after - scroll_top_before) < 50, (
            f"scroll position drifted across hide/show: {scroll_top_before} -> {scroll_top_after}"
        )


@pytest.mark.timeout(30, func_only=False)
def test_no_agents_shows_new_tab_launcher(page: Page, tmp_path: Path) -> None:
    """When there are no agents, the workspace shows a New Tab launcher.

    The old agent sidebar (and its "No agents found" message) is gone, and so is
    the dockview empty-state overlay that replaced it: the dock is never empty,
    so a view with nothing to mount opens the launcher instead. With no
    discoverable agents there is no chat to auto-open, so the launcher is the
    whole dock -- one "New tab" tab and no transcript.
    """
    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT + 2)
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    app = create_application(build_test_state(config=config, agent_manager=manager))

    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        server = make_threaded_server("127.0.0.1", _PORT + 2, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{_PORT + 2}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            page.goto(f"http://127.0.0.1:{_PORT + 2}")

            # The launcher stands in for the missing chat: one "New tab" tab and
            # nothing else, with no transcript rendered anywhere.
            expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
            expect(page.locator(".dv-default-tab-content")).to_have_count(1)
            expect(page.locator(".dv-default-tab-content").first).to_have_text("New tab")
            expect(_chat(page).locator(".message-list")).to_have_count(0)
            # No agent exists, so no chat is offered to jump to. (The rest of the
            # machine-wide table is whatever this host happens to be running, so
            # only the chat half is asserted on.)
            expect(page.locator(".new-tab-launcher-row", has_text="test-agent")).to_have_count(0)
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


_PROJECT_DIALOG_PORT = 18867


def _open_rail_switcher(page: Page) -> None:
    """Open the project rail's switcher menu (the old top-bar picker's job)."""
    page.locator(".project-rail-header").click()
    expect(page.locator(".project-rail-menu")).to_be_visible(timeout=5000)


def _switch_view_via_rail(page: Page, view_name: str) -> None:
    """Drive the rail's switcher header to mount ``view_name``."""
    _open_rail_switcher(page)
    page.locator(".project-rail-menu [role='menuitem']", has_text=view_name).first.click()


def _collapse_rail(page: Page) -> None:
    """Fold the hover-expanded rail back up, so the dock underneath is clickable.

    Driving the rail leaves the pointer resting on it, and an expanded rail is a
    240px panel floating OVER the dock rather than beside it -- so a test that
    just switched views and then reaches for a tab is reaching through it.
    Moving the pointer away is what a user does without thinking about it.

    The caller has usually just triggered a view switch, which by itself
    forces the rail closed now (Sidebar.ts tracks the last view it rendered
    for and collapses on a change, not only on mouseleave) -- a completed
    switch rebuilds the rail's own DOM subtree, and a pointer already resting
    on it at that moment gets a fresh native mouseenter with no mouseleave to
    follow, so mouseleave alone could never be trusted to fire again. The
    ``mouse.move`` here still matters for tests that expand the rail WITHOUT
    switching (e.g. just opening a menu), and the generous timeout matches
    this file's usual margin for a wait that can land behind a server
    round-trip, in case one is still in flight.
    """
    page.mouse.move(600, 400)
    expect(page.locator(".project-rail-search")).to_have_count(0, timeout=15000)


@pytest.mark.timeout(120, func_only=False)
def test_project_dialogs_end_to_end(tmp_path: Path, page: Page) -> None:
    """The rail's switcher and settings modal drive the project registry.

    End-to-end over the real frontend + server: the initial landing on the
    starter project with its WebSocket registration, the debounced autosave
    materializing that project's content file, "New project" minting the next
    "Project N" and switching onto it, and deleting the active project from the
    header's settings modal falling back to the surviving one.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _PROJECT_DIALOG_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        # The delete-fallback path surfaces a notice via alert(); auto-accept it.
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # Initial: the starter project is chosen, the fixture chat auto-opens,
        # and the debounced autosave materializes its content file.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        wait_for(
            lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.json",
        )

        # New project: no naming form any more -- it mints the next free
        # "Project N", switches onto it, and its own autosave materializes a
        # second content file under the slugified id.
        _open_rail_switcher(page)
        page.locator(".project-rail-menu [role='menuitem']", has_text="New project").click()
        page.wait_for_function("localStorage.getItem('si-active-project-id') === 'project-2'", timeout=10000)
        wait_for(
            lambda: (layout_dir / "projects" / "project-2.json").exists(),
            timeout=10.0,
            poll_interval=0.1,
            error_message="create never wrote project-2.json",
        )

        # Delete the active project from the header's context menu: the client
        # auto-switches to the fallback and the registry drops the entry.
        page.locator(".project-rail-header").click(button="right")
        page.locator(".project-rail-menu [role='menuitem']", has_text="Project settings").click()
        page.locator(".destroy-dialog-btn-cancel", has_text="Delete").click()
        page.locator(".destroy-dialog-btn-destroy", has_text="Delete project").click()
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        registry = json.loads((layout_dir / "projects_meta.json").read_text())
        assert "project-2" not in registry["project_by_id"]
        # Everything is the unfiltered view, not a project: it keeps a layout file
        # but never a registry entry.
        assert EVERYTHING_VIEW_ID not in registry["project_by_id"]


_LAYOUT_RESTORE_PORT = 18868


@pytest.mark.timeout(120, func_only=False)
def test_switching_views_preserves_chat_transcript(tmp_path: Path, page: Page) -> None:
    """A chat pane restored by a view switch still shows its own transcript.

    Regression test: ``fromJSON`` disposes the outgoing panels before creating
    the incoming ones, and the removal handler deletes their ``panelParams``.
    Because panel ids are deterministic, a chat open in BOTH views had its
    freshly-seeded params deleted mid-restore and came back bound to the primary
    (services) agent -- the tab kept its title but showed an empty transcript.

    Getting the chat into both views also exercises the model: Everything is the
    unfiltered view with a layout of its own, so it lists the machine's agent
    even though no project put it there, and opening it there leaves it open in
    the starter project too. Nothing moves.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAYOUT_RESTORE_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The fixture chat auto-opens in the starter project and shows its
        # transcript, which the debounced autosave writes out.
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        wait_for(
            lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.json",
        )

        # Over to Everything, which has its own (empty) layout, so it mounts a
        # launcher. Its machine-wide table lists the agent regardless of which
        # project shows it; opening it from there gives Everything the same chat
        # panel the starter project already has.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        page.locator(".new-tab-launcher-row:visible", has_text="test-agent").first.click()
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        wait_for(
            lambda: (layout_dir / "projects" / f"{EVERYTHING_VIEW_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message="autosave never materialized everything.json",
        )

        # Back to the starter project, whose saved layout holds the same panel id.
        _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )

        # The restored chat must show ITS transcript -- not the primary agent's
        # (which would render an empty / no-conversation state under the same tab).
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-empty")).to_have_count(0)
        expect(_chat(page).locator(".message-list-not-found")).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_layout_missing_panel_params_recovers_chat_binding(tmp_path: Path, page: Page) -> None:
    """A saved project whose panelParams are missing still binds the chat correctly.

    Panel ids encode identity (``chat-<agent-id>``), so a params-less panel is
    rebuilt from its id rather than silently defaulting to the primary agent.
    This also self-heals content files corrupted by the restore bug above.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAYOUT_RESTORE_PORT + 1, primary_agent_id=primary_agent_id) as (
        base_url,
        agent_info,
        _session_file,
    ):
        # Hand-write the starter project's content holding the agent's chat
        # panel with an EMPTY panelParams map -- the shape the restore bug used
        # to persist. The starter project is what a fresh browser mounts, so this
        # is the content the page loads.
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        projects_dir = layout_dir / "projects"
        projects_dir.mkdir(parents=True)
        panel_id = f"chat-{agent_info.id}"
        (projects_dir / f"{DEFAULT_PROJECT_ID}.json").write_text(
            json.dumps(
                {
                    "dockview": {
                        "activeGroup": "group-1",
                        "grid": {
                            "root": {
                                "type": "branch",
                                "data": [
                                    {
                                        "type": "leaf",
                                        "data": {"views": [panel_id], "activeView": panel_id, "id": "group-1"},
                                        "size": 1000,
                                    }
                                ],
                            },
                            "width": 1000,
                            "height": 1000,
                            "orientation": "HORIZONTAL",
                        },
                        "panels": {
                            panel_id: {
                                "id": panel_id,
                                "contentComponent": "chat",
                                "tabComponent": "custom",
                                "title": agent_info.name,
                            }
                        },
                    },
                    "panelParams": {},
                }
            )
        )

        page.goto(base_url)

        # The chat is rebuilt from its panel id, so it shows its own transcript.
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-empty")).to_have_count(0)
        expect(_chat(page).locator(".message-list-not-found")).to_have_count(0)


_LIVE_SURFACE_PORT = 18870

# A page for the framed tab below, served by a Playwright route rather than by a
# server. The workspace only opens ad-hoc URL tabs for ``https://`` refs, so this
# is cross-origin from the shell -- which is also the production shape, since
# every service pane is a cross-origin iframe. The document is therefore driven
# through Playwright (which crosses origins) rather than from the shell's own JS.
# Its state is an ``<input>``: typing into it is a change no reload survives,
# because the served markup has it empty.
_FRAMED_PAGE_URL = "https://e2e-live-page.example/"
_FRAMED_PAGE_HTML = "<!doctype html><html><body><input id='held' value='' /></body></html>"

# Count every ``.si-live-surface`` that leaves the document from here on. The
# requirement is not "a page comes back looking right" but "the element holding
# it never leaves the DOM" -- removing an iframe destroys its document -- so this
# watches the mechanism directly rather than inferring it from what is on screen.
_WATCH_SURFACE_REMOVALS_JS = """
() => {
  window.__e2eRemovedSurfaces = [];
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) {
        if (node instanceof Element && node.classList.contains('si-live-surface')) {
          window.__e2eRemovedSurfaces.push(node.className);
        }
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}
"""

# The surfaces holding a chat transcript, as a plain-object report. Identity is
# carried by ``__e2eChatStamp``, a property set on the ELEMENT rather than an
# attribute: nothing serializes it, so a surface that answers to it is
# necessarily the very element that was stamped, not a rebuilt look-alike.
_CHAT_SURFACE_REPORT_JS = """
(stamp) => {
  const surfaces = Array.from(document.querySelectorAll('.si-live-surface'))
    .filter((surface) => surface.querySelector('iframe[data-live-key^="chat:"]') !== null);
  if (stamp) {
    for (const surface of surfaces) surface.__e2eChatStamp = stamp;
  }
  const shown = surfaces.filter((surface) => {
    const box = surface.getBoundingClientRect();
    return getComputedStyle(surface).display !== 'none' && box.width > 0 && box.height > 0;
  });
  return {
    count: surfaces.length,
    shownCount: shown.length,
    stamps: surfaces.map((surface) => surface.__e2eChatStamp ?? null),
    removals: window.__e2eRemovedSurfaces.length,
  };
}
"""


@pytest.mark.timeout(120, func_only=False)
def test_live_page_survives_a_view_that_does_not_include_it(tmp_path: Path, page: Page) -> None:
    """A page keeps running, and keeps its state, while no view is showing it.

    The requirement in full: there is one live page per object, machine-wide, and
    a project is only a view that may or may not include it. Type into an app,
    switch to a project that does not have it, switch back -- the same document
    is still there, still holding what was typed, resized into whatever pane
    shows it now. It must not reload and must not fork into a second copy.

    This drives a real iframe document, not a stand-in. The page is typed into
    through Playwright, exactly as a user would, and read back afterwards: a
    reload would put back the empty field the route serves, and a fork would show
    that empty copy beside the typed one, so surviving the round trip is only
    possible if the very same document lived through the switch untouched.

    Asserted on three independent things, because each rules out a different
    failure: the framed document's own state (proves it was neither reloaded nor
    re-created), the surface element's identity via a non-serializable property
    (proves nothing rebuilt it), and a MutationObserver count of surfaces removed
    from the document (proves the element never left the DOM at all, which is the
    mechanism the whole design rests on).

    The framed page is cross-origin, as every real service pane is, so the shell
    cannot read into its document -- which is why its state is asserted through
    Playwright's frame locator, and why the checks made while it is hidden are
    limited to the element holding it.
    """
    primary_agent_id = "primary-services-agent"
    framed_page_selector = 'iframe[src*="e2e-live-page"]'
    with _running_e2e_server(tmp_path, _LIVE_SURFACE_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.route(
            "**e2e-live-page.example/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=_FRAMED_PAGE_HTML),
        )
        page.goto(base_url)

        # Land on the starter project with the fixture chat open.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)

        # Open the framed page beside it, through the same layout-op path an
        # agent would use, and wait for its document to actually be there.
        _broadcast_layout_op(
            base_url,
            "open",
            {"ref": _FRAMED_PAGE_URL, "new_group": True},
            agent_id="agent-test-123",
        )
        expect(page.locator(framed_page_selector)).to_have_count(1, timeout=_TRIGGER_TIMEOUT_MS)
        held_field = page.frame_locator(framed_page_selector).locator("#held")
        expect(held_field).to_have_value("", timeout=15000)

        # Type into the page, and stamp the element holding it. The typed text
        # dies on a reload; the stamp dies if anything re-creates the element.
        held_field.fill("typed-by-the-user")
        expect(held_field).to_have_value("typed-by-the-user")
        page.evaluate(
            """
            () => {
              const iframe = document.querySelector('iframe[src*="e2e-live-page"]');
              iframe.closest('.si-live-surface').__e2eSurfaceStamp = 'the-original-element';
            }
            """
        )

        # Let the debounced autosave record the arrangement, so switching away
        # and back is a real round trip through the project's stored layout.
        wait_for(
            lambda: (
                (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists()
                and "e2e-live-page.example" in (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").read_text()
            ),
            timeout=15.0,
            poll_interval=0.1,
            error_message="autosave never recorded the framed page in the starter project",
        )

        # Over to Everything, which has its own empty layout and therefore does
        # not include this page at all -- it mounts a launcher instead.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        # Nothing is showing the page, so its surface is hidden -- the same
        # ``display: none`` dockview already uses for an inactive tab.
        page.wait_for_function(
            """
            () => {
              const iframe = document.querySelector('iframe[src*="e2e-live-page"]');
              if (iframe === null) return false;
              return getComputedStyle(iframe.closest('.si-live-surface')).display === 'none';
            }
            """,
            timeout=15000,
        )

        while_away = page.evaluate(
            """
            () => {
              const iframe = document.querySelector('iframe[src*="e2e-live-page"]');
              if (iframe === null) return { present: false };
              return {
                present: true,
                stamped: iframe.closest('.si-live-surface').__e2eSurfaceStamp ?? null,
                removals: window.__e2eRemovedSurfaces.length,
              };
            }
            """
        )
        assert while_away["present"], "the page was taken out of the DOM by a view that does not include it"
        assert while_away["stamped"] == "the-original-element", f"the element was rebuilt while hidden: {while_away}"
        assert while_away["removals"] == 0, f"a live surface left the DOM on the way out: {while_away}"

        # Back to the project that includes it. The page must be the same one --
        # not reloaded, not forked -- resized into the pane showing it now.
        _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        page.wait_for_function(
            """
            () => {
              const iframe = document.querySelector('iframe[src*="e2e-live-page"]');
              if (iframe === null) return false;
              const surface = iframe.closest('.si-live-surface');
              const box = surface.getBoundingClientRect();
              return getComputedStyle(surface).display !== 'none' && box.width > 0 && box.height > 0;
            }
            """,
            timeout=15000,
        )

        on_return = page.evaluate(
            """
            () => {
              const iframes = Array.from(document.querySelectorAll('iframe[src*="e2e-live-page"]'));
              const iframe = iframes[0] ?? null;
              return {
                frameCount: iframes.length,
                stamped: iframe === null ? null : (iframe.closest('.si-live-surface').__e2eSurfaceStamp ?? null),
                removals: window.__e2eRemovedSurfaces.length,
              };
            }
            """
        )
        assert on_return["frameCount"] == 1, f"the page forked into a second copy: {on_return}"
        assert on_return["stamped"] == "the-original-element", (
            f"the element was re-created on the way back: {on_return}"
        )
        assert on_return["removals"] == 0, f"a live surface left the DOM during the round trip: {on_return}"
        # The document itself carried on: it still holds what was typed, which a
        # reload would have replaced with the empty field the route serves.
        expect(held_field).to_have_value("typed-by-the-user")


@pytest.mark.timeout(120, func_only=False)
def test_one_object_is_one_element_in_every_view_showing_it(tmp_path: Path, page: Page) -> None:
    """An object shown by two views is ONE element, shown twice -- never two.

    The starter project and Everything both end up showing the fixture chat, so
    two views want it at once. There must still be exactly one live surface for
    it, and it must be *the same element* from either side: a design that gave
    each view its own dock and rebuilt panels inside it would pass a
    "something rendered" check while quietly running two copies of the object.

    Identity is asserted rather than presence. The element is stamped with a
    plain JS property, which nothing serializes and no rebuild reproduces, so
    reading that stamp back from the other view is only possible if it is
    literally the same element. Alongside it the surface count is pinned at one,
    which is what rules out the fork the stamp alone could not see.

    A chat is a mithril-mounted page rather than an iframe, so what this test
    checks is the element's identity and singularity across the switch -- the
    survival of a framed document's own state is covered by
    ``test_live_page_survives_a_view_that_does_not_include_it`` above.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LIVE_SURFACE_PORT + 1, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The starter project shows the chat; stamp the one surface holding it.
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)
        in_project = page.evaluate(_CHAT_SURFACE_REPORT_JS, "the-original-element")
        assert in_project["count"] == 1, f"the starter project should hold exactly one chat page: {in_project}"
        assert in_project["shownCount"] == 1, f"the starter project's chat page should be on screen: {in_project}"
        wait_for(
            lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.json",
        )

        # Everything lists the machine's agent whatever project shows it. Opening
        # it here must reach for the page that already exists, not start a second.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        page.locator(".new-tab-launcher-row:visible", has_text="test-agent").first.click()
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

        in_everything = page.evaluate(_CHAT_SURFACE_REPORT_JS, None)
        assert in_everything["count"] == 1, f"opening the chat in Everything forked its page: {in_everything}"
        assert in_everything["shownCount"] == 1, f"Everything is not showing the chat page: {in_everything}"
        assert in_everything["stamps"] == ["the-original-element"], (
            f"Everything is showing a different element than the starter project: {in_everything}"
        )
        assert in_everything["removals"] == 0, f"a live surface left the DOM on the way in: {in_everything}"

        # And back: still one element, still that element.
        _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

        back_in_project = page.evaluate(_CHAT_SURFACE_REPORT_JS, None)
        assert back_in_project["count"] == 1, f"switching back forked the chat page: {back_in_project}"
        assert back_in_project["shownCount"] == 1, f"the chat page is not on screen again: {back_in_project}"
        assert back_in_project["stamps"] == ["the-original-element"], (
            f"switching back re-created the chat's element: {back_in_project}"
        )
        assert back_in_project["removals"] == 0, (
            f"a live surface left the DOM during the round trip: {back_in_project}"
        )


# Comfortably past the workspace's 1500ms autosave debounce, so a test can wait
# for the writes an action already triggered to land before provoking the next.
AUTOSAVE_SETTLE_MS = 3000

_FIXTURE_CHAT_REF = "chat:agent-test-123"


def _drop_overlay_styles(page: Page) -> dict[str, Any] | None:
    """What the drop overlay currently showing looks like, or None if there is none.

    Read as computed style rather than as markup because the whole question is
    what the user sees: whether the selection paints a region, and whether it
    carries the pseudo-element that draws the insertion line. Its left edge
    comes back too, since a line is only right if it is on the correct edge.
    """
    return page.evaluate(
        """() => {
            const el = document.querySelector('.dv-drop-target-selection');
            if (!el) return null;
            const style = getComputedStyle(el);
            const after = getComputedStyle(el, '::after');
            const box = el.getBoundingClientRect();
            return {
                background: style.backgroundColor,
                afterContent: after.content,
                afterWidth: after.width,
                afterBackground: after.backgroundColor,
                side: el.classList.contains('dv-drop-target-left')
                    ? 'left'
                    : el.classList.contains('dv-drop-target-right')
                      ? 'right'
                      : 'other',
                left: box.left,
                right: box.right,
            };
        }"""
    )


def _member_titles(layout_dir: Path) -> dict[str, str]:
    """Every name the user has given an object, straight out of the store on disk.

    The store is the machine's, not a view's, so this is read from the layout
    dir itself rather than from under ``projects/``. A workspace where nothing
    has been named has no file at all, which reads as an empty map.
    """
    titles_path = layout_dir / "member_titles.json"
    if not titles_path.exists():
        return {}
    title_by_ref = json.loads(titles_path.read_text())["title_by_ref"]
    assert isinstance(title_by_ref, dict)
    return title_by_ref


_AUTO_TITLE_PORT = 18878

# What a terminal tab reads: the "Terminal N" display form derived from its
# allocated ``terminal-N`` session. The number is the allocator's lowest free
# slot on the (shared) tmux socket, so tests capture it instead of assuming 1.
_TERMINAL_TAB_TITLE_RE = re.compile(r"^Terminal \d+$")


def _create_terminal_from_launcher(page: Page, known_titles: set[str]) -> str:
    """Create a terminal from the launcher's Terminal tile; return its tab title.

    ``known_titles`` holds the terminal tab titles earlier calls returned, so a
    second create resolves to the NEW tab rather than the first one again. No
    naming dialog ever appears: the session name is machine-allocated and the
    display name is derived from it.
    """
    # The "+" is only offered when the pane has no launcher, since a pane holds
    # at most one; a create leaves the launcher behind on the failure paths, so
    # a repeat call may find one already up.
    if page.locator(".new-tab-launcher").count() == 0:
        page.locator(".dockview-add-tab-button").first.click()
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
    page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()
    expect(page.locator(".custom-url-dialog")).to_have_count(0)

    found: dict[str, str] = {}

    def _new_tab_appeared() -> bool:
        for tab in page.locator(".dv-default-tab-content", has_text=_TERMINAL_TAB_TITLE_RE).all():
            title = tab.inner_text().strip()
            if title and title not in known_titles:
                found["title"] = title
                return True
        return False

    wait_for(_new_tab_appeared, timeout=10.0, poll_interval=0.1, error_message="no new terminal tab appeared")
    known_titles.add(found["title"])
    return found["title"]


@pytest.mark.timeout(120, func_only=False)
def test_ui_created_terminal_wears_a_derived_friendly_name(tmp_path: Path, page: Page) -> None:
    """A terminal created from the UI comes into being named "Terminal N".

    No create flow asks the user for a name: the tmux session name
    (``terminal-N``) stays the identity, machine-allocated and never surfaced
    as something to pick, and the tab's "Terminal N" is DERIVED from it -- the
    same display-name/canonical-name pairing chats and browsers use -- so
    nothing is written to the machine's title store. The store staying empty is
    asserted alongside the strip because that is the difference from the old
    arrangement, where each create filed a second copy of the name.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _AUTO_TITLE_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.goto(base_url)

        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        assert _member_titles(layout_dir) == {}, "something was named before anything was created"

        # The launcher's Terminal tile creates directly -- no naming dialog ever
        # appears -- and the tab reads "Terminal N" for the allocated session.
        titles_seen: set[str] = set()
        first_title = _create_terminal_from_launcher(page, titles_seen)
        first_number = int(first_title.removeprefix("Terminal "))

        # A second create allocates the next free session, so its number is a
        # different one (the exact values depend on what the shared tmux socket
        # already holds).
        second_title = _create_terminal_from_launcher(page, titles_seen)
        second_number = int(second_title.removeprefix("Terminal "))
        assert second_number != first_number

        # The names were derived, not stored: nothing was written to the
        # machine's title store for either create.
        page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
        assert _member_titles(layout_dir) == {}, "a derived terminal name was needlessly stored"


_TAB_RENAME_PORT = 18872


@pytest.mark.timeout(120, func_only=False)
def test_double_click_renames_a_chat_and_the_name_survives_a_reload(tmp_path: Path, page: Page) -> None:
    """Double-clicking a chat tab's title renames the chat, and the name is kept.

    The gesture is only half of it. A chat's name lives on its mngr agent --
    the typed form as its ``display_name`` label, the canonical form as its
    true name -- so the commit goes through ``mngr rename`` (the fixture's stub
    accepts it) and NOTHING lands in the machine's title store: the label is
    the name now, and a stored copy is exactly the second source of truth this
    arrangement removed. The reload is what proves the name stuck to the agent
    rather than to the tab -- the strip re-derives it from the agents payload.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _TAB_RENAME_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The fixture chat auto-opens wearing the name derived from its agent,
        # which is the name the rename replaces.
        tab_title = page.locator(".dv-default-tab-content", has_text="test-agent").first
        expect(tab_title).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        assert _member_titles(layout_dir) == {}, "something was named before anyone renamed anything"

        # The title becomes a field seeded with the name it has now, so typing
        # over a name is one gesture rather than a select-all first.
        tab_title.dblclick()
        editor = page.locator(".dv-custom-tab-title-input:visible")
        expect(editor).to_be_visible(timeout=5000)
        expect(editor).to_have_value("test-agent")

        editor.fill("Design notes")
        editor.press("Enter")

        # Enter commits: the field goes away and the strip draws the new name.
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=5000)
        expect(page.locator(".dv-custom-tab-title-input:visible")).to_have_count(0)

        # The name went to the agent, not the store: nothing was written there.
        page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
        assert _member_titles(layout_dir) == {}, "a chat rename wrote into the title store"

        page.reload()
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=15000)
        # Still the chat that was renamed, not a tab that merely kept a string.
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        # And the old name is gone rather than restored onto a second tab.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)


_TERMINAL_DESTROY_PORT = 18879


# `/api/terminals` answers by shelling out to `tmux list-sessions`, so its
# latency is a subprocess spawn's, not a handler's. Five seconds was tight
# enough to lose the race on a machine also running the rest of this suite --
# seen as a bare socket timeout inside this helper, which fails the calling
# test outright rather than being retried by it.
_TERMINALS_API_TIMEOUT_SECONDS = 20.0


def _terminal_session_names(base_url: str) -> set[str]:
    """The live user-terminal session names, as the server's terminals API reports them."""
    with urllib.request.urlopen(f"{base_url}/api/terminals", timeout=_TERMINALS_API_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return {terminal["session_name"] for terminal in payload["terminals"]}


# Same _collapse_rail race as test_renaming_an_object_in_one_view_names_it_in
# _the_other above: seen fail once on this helper's own wait, then pass twice
# on retry with no code changed in between. Separately seen to lose the
# terminals API's own timeout under load, which _TERMINALS_API_TIMEOUT_SECONDS
# now allows for.
@pytest.mark.flaky
@pytest.mark.timeout(180, func_only=False)
def test_shut_down_terminal_leaves_no_resurrected_tab_in_everything(tmp_path: Path, page: Page) -> None:
    """Shutting down a terminal leaves nothing of it for Everything to restore.

    The reported shape of this: destroy a terminal from its tab menu while
    Everything's saved layout still holds its panel, then open Everything -- and
    a dead terminal tab comes back, whose attach-or-create quietly respawns a
    fresh tmux session under the old ``terminal-N`` id, wearing no name. Destroy
    is the one cross-project operation, so the sweep has to reach Everything's
    saved arrangement even though Everything is a view rather than a project,
    with no registry entry for the project loop to find.

    The tmux session is created here by hand, standing in for the ttyd attach
    that creates it lazily in a real workspace (no terminal service runs in this
    harness). That is what lets the fleet list the terminal for Everything's
    machine table to offer, and what gives the shutdown a real session to kill.

    The destroy's whole blast radius is asserted rather than one fact of it:
    the tab leaves the mounted project, the session leaves tmux, the panel
    leaves Everything's saved content -- and, the regression, mounting
    Everything afterwards draws no terminal tab and respawns no session.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _TERMINAL_DESTROY_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        everything_file = layout_dir / "projects" / f"{EVERYTHING_VIEW_ID}.json"
        # The default tmux socket is shared with whatever else runs on this
        # machine, so "nothing respawned" is judged at the end against what was
        # already live rather than against emptiness.
        preexisting_sessions = _terminal_session_names(base_url)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )

        # Create the terminal from the launcher's tile, exactly as the user did.
        # The tab's "Terminal N" is derived from the machine-allocated session
        # name -- never hardcoded, because the allocator hands out the lowest
        # ``terminal-N`` the socket is not already using -- so the session name
        # is recovered by the reverse of the same derivation.
        terminal_title = _create_terminal_from_launcher(page, set())
        session_name = f"terminal-{terminal_title.removeprefix('Terminal ')}"

        # Session creation is lazy (on ttyd attach) and no terminal service
        # runs in this harness, so stand in for the attach. Without a live
        # session the fleet would not list the terminal for Everything's
        # machine table to offer, and the shutdown would have nothing to kill.
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", str(tmp_path)],
            check=True,
            capture_output=True,
            timeout=10.0,
        )
        try:
            wait_for(
                lambda: session_name in _terminal_session_names(base_url),
                timeout=10.0,
                poll_interval=0.1,
                error_message=f"the terminals API never listed {session_name}",
            )

            # File the terminal into Everything the way a user would: mount it,
            # open the terminal from the machine-wide table its launcher shows,
            # and let the autosave write it into Everything's saved layout.
            _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
            page.wait_for_function(
                f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
            )
            expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
            page.locator(".new-tab-launcher-row:visible", has_text=terminal_title).first.click()
            expect(page.locator(".dv-default-tab-content", has_text=terminal_title).first).to_be_visible(timeout=15000)
            wait_for(
                lambda: everything_file.exists() and session_name in everything_file.read_text(),
                timeout=15.0,
                poll_interval=0.1,
                error_message="autosave never filed the terminal into everything.json",
            )

            # Back in the starter project, shut the terminal down exactly as
            # the user did: hover its tab, open the options menu behind the
            # kebab, pick the destructive verb, and confirm.
            _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
            page.wait_for_function(
                f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
            )
            _collapse_rail(page)
            terminal_tab = page.locator(
                ".dv-tab", has=page.locator(".dv-default-tab-content", has_text=terminal_title)
            ).first
            expect(terminal_tab).to_be_visible(timeout=15000)
            terminal_tab.hover()
            terminal_tab.locator('.dv-custom-tab-action[aria-label="Tab options"]').click()
            page.locator("[role='menuitem']", has_text=f"Delete {terminal_title}").click()
            page.locator(".destroy-dialog-btn-destroy").click()

            # The whole blast radius. The tab leaves the mounted project ...
            expect(page.locator(".dv-default-tab-content", has_text=terminal_title)).to_have_count(0, timeout=10000)
            # ... the session leaves tmux ...
            wait_for(
                lambda: session_name not in _terminal_session_names(base_url),
                timeout=15.0,
                poll_interval=0.1,
                error_message=f"the destroy left tmux session {session_name} running",
            )
            # ... and the panel leaves Everything's saved content. The terminal
            # was all Everything held, so the strip may delete the file outright
            # rather than keep a layout with no panels; both count as gone.
            wait_for(
                lambda: not everything_file.exists() or session_name not in everything_file.read_text(),
                timeout=15.0,
                poll_interval=0.1,
                error_message="the destroy left the terminal's panel in everything.json",
            )

            # The regression: mounting Everything now draws its launcher (there
            # is nothing left to restore), not a terminal tab -- neither under
            # the auto-filed name nor under the raw session id a dead tab used
            # to come back wearing.
            _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
            page.wait_for_function(
                f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
            )
            expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
            expect(page.locator(".dv-default-tab-content", has_text=terminal_title)).to_have_count(0)
            expect(page.locator(".dv-default-tab-content", has_text="terminal-")).to_have_count(0)

            # And after the time an attach-or-create would have needed, the
            # mount still spawned nothing: no terminal session is live now that
            # was not already live before this test created anything.
            page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
            expect(page.locator(".dv-default-tab-content", has_text=terminal_title)).to_have_count(0)
            respawned = {
                name
                for name in _terminal_session_names(base_url)
                if name.startswith("terminal-") and name not in preexisting_sessions
            }
            assert respawned == set(), f"a destroyed terminal's session came back: {respawned}"
        finally:
            # Belt-and-braces teardown for the failure paths above; on the
            # passing path the shutdown already killed the session.
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={session_name}"],
                check=False,
                capture_output=True,
                timeout=10.0,
            )


def _project_members(layout_dir: Path) -> list[str]:
    """The starter project's member refs, straight out of the registry on disk.

    Registry writes are atomic (tmp + rename), so a read sees either the old
    or the new content in full -- but the file may not exist yet before the
    first write, which reads as "no members yet" for the polls calling this.
    """
    try:
        registry = json.loads((layout_dir / "projects_meta.json").read_text())
    except FileNotFoundError:
        return []
    members = registry["project_by_id"][DEFAULT_PROJECT_ID]["members"]
    assert isinstance(members, list)
    return members


_ROW_REMOVAL_PORT = 18873


@pytest.mark.timeout(120, func_only=False)
def test_removing_a_row_from_the_project_unfiles_it_without_destroying_it(tmp_path: Path, page: Page) -> None:
    """The rail row menu's "Remove from project" unfiles a member rather than destroying it.

    The safe middle verb of the shared object menu (objectMenu.ts): it takes
    the ref out of the mounted project's member list and undocks its tab, and
    the object itself is untouched. Both halves are unit-tested apart -- the
    menu item routes to ``onRemoveFromView``, and the endpoint unfiles without
    stopping anything -- so what this covers is the wiring between them, which
    is the part that moved when the verb went from the rail's own
    ``removalItemsForRow`` to the definition the tab menu shares.

    The removal is asserted against the registry on disk, same as any other
    membership change; "kept running" is asserted against Everything, which
    lists every object on the machine regardless of membership and so still has
    to show this one afterwards.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _ROW_REMOVAL_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        wait_for(
            lambda: _FIXTURE_CHAT_REF in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the fixture chat was never filed as a member of the starter project",
        )

        # Right-click the chat's row in the rail's tab list, which is one of the
        # two ways that row's menu opens, and take the verb from the menu rather
        # than from the row's own one-click remove: the menu is what renders the
        # shared definition.
        page.locator(".machine-sidebar").hover()
        chat_row = page.locator(".project-rail-tab", has_text="test-agent")
        expect(chat_row).to_have_count(1)
        chat_row.click(button="right")
        page.locator(".project-rail-menu [role='menuitem']", has_text="Remove from project").click()

        # The tab leaves the mounted project's dock ...
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0, timeout=10000)
        # ... and the ref leaves the project's member list on disk.
        wait_for(
            lambda: _FIXTURE_CHAT_REF not in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="Remove from project never took the member out of the registry",
        )

        # It kept running rather than being destroyed: nothing was ever docked
        # in Everything, so its launcher's machine-wide table -- not a
        # membership list, since Everything is the unfiltered view -- is what
        # still has to offer the chat even though the starter project no
        # longer does.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        expect(page.locator(".new-tab-launcher-row:visible", has_text="test-agent")).to_have_count(1)


_APP_PINNING_PORT = 18875

# The one app the machine offers in the pinning test, and the member ref it is
# filed under. The label is what ``_running_e2e_server`` mints for it, and it is
# what the pane's origin -- and therefore its iframe src -- is built from.
_PINNABLE_APP_NAME = "docs-viewer"
_PINNABLE_APP_REF = f"service:{_PINNABLE_APP_NAME}"
_PINNABLE_APP_LABEL = f"{_PINNABLE_APP_NAME}-e2elabel"

# One app's row inside the "All apps" popover. Matched on its label span
# rather than the row's whole textContent, because the row's glyph may be a
# monogram whose SVG <text> initial leaks into the latter.
_APP_ROW_SELECTOR = ".project-rail-app:has(.truncate:text-is('{name}'))"


def _open_all_apps(page: Page) -> None:
    """Hover the rail open and click through to its "All apps" popover.

    The row only exists on an expanded rail, and the rail expands on hover, so
    this is the same two moves a user makes. The popover then holds the rail
    open by itself while the pointer works down the list.
    """
    page.locator(".machine-sidebar").hover()
    page.locator(".project-rail-all-apps").click()
    expect(page.locator(".project-rail-app").first).to_be_visible(timeout=5000)


@pytest.mark.timeout(120, func_only=False)
def test_pinning_an_app_to_a_project_is_the_same_as_its_membership(tmp_path: Path, page: Page) -> None:
    """Pinning an app IS filing it in the project, and unpinning is unfiling it.

    There is one concept here, not two: an app is pinned exactly when the
    project's member list holds its ``service:<name>`` ref. So "All apps" lists
    exactly the apps the view has NOT pinned -- the pinned ones are already in
    the rail -- and the round trip has to move the app on the server, not in
    this browser: pinning puts the ref in the registry on disk, grows a rail
    shortcut and drops the row from the popover; unpinning, which the rail row's
    own pin icon does in one click, undoes all three.

    The last assertion is the one the design turns on. Unpinning is removing an
    object from a view and nothing more, so the app must still be RUNNING
    afterwards: its live page stays mounted, stays the very element it was, and
    never leaves the DOM -- which is checked by a stamp nothing serializes and by
    a MutationObserver counting surfaces removed from the document.
    """
    primary_agent_id = "primary-services-agent"
    app_frame_selector = f'iframe[src*="{_PINNABLE_APP_LABEL}"]'
    with _running_e2e_server(
        tmp_path,
        _APP_PINNING_PORT,
        primary_agent_id=primary_agent_id,
        apps=(_PINNABLE_APP_NAME,),
    ) as (base_url, _agent_info, _session_file):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        # The app's pane is a cross-origin iframe, as every service pane is;
        # serving it here keeps the tab a real loaded document rather than an
        # error page.
        page.route(
            f"**{_PINNABLE_APP_LABEL}**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=_FRAMED_PAGE_HTML),
        )
        page.goto(base_url)

        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)

        # Nothing has pinned this app, so the popover -- which lists only what
        # the view has not pinned -- offers it, and the rail carries no shortcut.
        _open_all_apps(page)
        app_row = page.locator(_APP_ROW_SELECTOR.format(name=_PINNABLE_APP_NAME))
        expect(app_row).to_have_count(1)
        expect(page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME)).to_have_count(0)
        assert _PINNABLE_APP_REF not in _project_members(layout_dir)

        # Pin it. The row leaves the popover -- a pinned app lives in the rail a
        # few pixels away, so listing it twice is what the filtering removes --
        # the rail grows a shortcut for it, and, because pinning IS membership,
        # the ref lands in the project's member list on disk.
        page.locator(f'button[aria-label="Pin {_PINNABLE_APP_NAME}"]').click()
        expect(app_row).to_have_count(0, timeout=15000)
        expect(page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME)).to_have_count(1)
        wait_for(
            lambda: _PINNABLE_APP_REF in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="pinning never filed the app as a member of the project",
        )

        # Open it from that shortcut, so there is a live page to lose. Stamp the
        # element holding it: nothing serializes the property, so a surface that
        # answers to it later is necessarily this same element.
        page.keyboard.press("Escape")
        expect(page.locator(".project-rail-app")).to_have_count(0, timeout=5000)
        page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME).click()
        expect(page.locator(app_frame_selector)).to_have_count(1, timeout=_TRIGGER_TIMEOUT_MS)
        page.evaluate(
            f"""
            () => {{
              const iframe = document.querySelector({json.dumps(app_frame_selector)});
              iframe.closest('.si-live-surface').__e2eSurfaceStamp = 'the-original-element';
            }}
            """
        )

        # Unpin it from the rail row itself, which is the one-click path now the
        # popover no longer lists a pinned app. The shortcut goes, the ref leaves
        # the project's member list, and the app returns to the popover it was
        # filtered out of.
        #
        # The popover has to be dismissed first: an open menu sits on a scrim
        # that swallows the click dismissing it, precisely so that click does not
        # also land on whatever was underneath -- the rail included.
        page.keyboard.press("Escape")
        expect(page.locator(".project-rail-app")).to_have_count(0, timeout=5000)
        page.locator(".machine-sidebar").hover()
        page.locator(f'button[aria-label="Unpin {_PINNABLE_APP_NAME}"]').click()
        expect(page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME)).to_have_count(0, timeout=15000)
        _open_all_apps(page)
        expect(page.locator(_APP_ROW_SELECTOR.format(name=_PINNABLE_APP_NAME))).to_have_count(1, timeout=15000)
        wait_for(
            lambda: _PINNABLE_APP_REF not in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="unpinning never removed the app from the project's member list",
        )

        # And the app is still running. Unpinning stops nothing: its page is the
        # same element, still mounted, and no live surface ever left the DOM.
        after_unpin = page.evaluate(
            f"""
            () => {{
              const iframes = Array.from(document.querySelectorAll({json.dumps(app_frame_selector)}));
              const iframe = iframes[0] ?? null;
              return {{
                frameCount: iframes.length,
                stamped: iframe === null ? null : (iframe.closest('.si-live-surface').__e2eSurfaceStamp ?? null),
                removals: window.__e2eRemovedSurfaces.length,
              }};
            }}
            """
        )
        assert after_unpin["frameCount"] == 1, f"unpinning stopped the app or forked its page: {after_unpin}"
        assert after_unpin["stamped"] == "the-original-element", f"unpinning re-created the app's page: {after_unpin}"
        assert after_unpin["removals"] == 0, f"a live surface left the DOM when the app was unpinned: {after_unpin}"


_LAUNCHER_RECENCY_PORT = 18876
_LAUNCHER_FILTER_PORT = 18877


def _member_last_used(layout_dir: Path) -> dict[str, int]:
    """When each object was last in front of the user, straight off the disk.

    Like ``_member_titles``, this reads the machine's store rather than a
    view's file, because recency belongs to the object machine-wide. A
    workspace where nothing has been used has no file at all, which reads as
    an empty map -- and so does a file caught mid-write, because the server
    writes it non-atomically while this test polls it, and the poll simply
    reads again.
    """
    last_used_path = layout_dir / "member_last_used.json"
    if not last_used_path.exists():
        return {}
    try:
        last_used_ms_by_ref = json.loads(last_used_path.read_text())["last_used_ms_by_ref"]
    except json.JSONDecodeError:
        return {}
    assert isinstance(last_used_ms_by_ref, dict)
    return last_used_ms_by_ref


@pytest.mark.timeout(120, func_only=False)
def test_launcher_shows_an_opened_tabs_recency_and_it_survives_a_reload(tmp_path: Path, page: Page) -> None:
    """Opening a tab gives its launcher row a recency, and a reload keeps it.

    The launcher's last-used column is fed by real uses: the dock's focus
    landing on a panel records the moment against the OBJECT's ref,
    machine-wide, and the server stamps it with its own clock. Mount-time
    arrivals are deliberately not uses -- the auto-opened chat was not touched
    by anyone -- so its row starts on the dash of an untouched object, and only
    the user's own click onto the tab moves it to "just now".

    The reload is the other half. The map lives in ``member_last_used.json``
    beside the titles store, so a fresh page load -- which restores the saved
    layout without re-touching anything, for the same mount-time reason -- has
    to get the recency back from disk. The stored stamp is read before and
    after to pin that down: it must still be there, and must never have moved
    backwards.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAUNCHER_RECENCY_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The fixture chat auto-opens into the starter project. That is a
        # mount-time arrival, not a use, so nothing has touched the store yet.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        wait_for(
            lambda: _FIXTURE_CHAT_REF in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the fixture chat was never filed as a member of the starter project",
        )
        assert _FIXTURE_CHAT_REF not in _member_last_used(layout_dir), "something was used before anyone used anything"

        # The launcher's row for the chat wears the dash of an untouched object.
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        chat_row = page.locator(
            ".new-tab-launcher-section[data-section='in-project'] .new-tab-launcher-row", has_text="test-agent"
        ).first
        expect(chat_row).to_be_visible(timeout=10000)
        expect(chat_row).to_contain_text("—")

        # Click onto the chat tab -- the dock's focus landing on it is what
        # "using" the object looks like -- and the touch reaches the store.
        page.locator(".dv-default-tab-content", has_text="test-agent").first.click()
        wait_for(
            lambda: _FIXTURE_CHAT_REF in _member_last_used(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="focusing the chat never touched the machine's last-used store",
        )

        # Back on the launcher -- focusing the chat folded the old one up, so
        # the "+" opens a fresh one -- the row now wears that recency.
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher").first).to_be_visible(timeout=10000)
        expect(chat_row).to_be_visible(timeout=10000)
        expect(chat_row).to_contain_text("just now")

        stored_ms_before_reload = _member_last_used(layout_dir)[_FIXTURE_CHAT_REF]

        # Let the autosave record the arrangement, so the reload restores the
        # chat from the saved layout instead of auto-opening (and re-touching)
        # it: whatever recency the row shows next came back from the store.
        page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
        page.reload()
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        # A launcher may have been restored with the layout. The "+" is only
        # offered when there is none -- a pane holds at most one, so with one
        # open the button would have nothing left to do -- hence asking first
        # rather than clicking blind.
        if page.locator(".new-tab-launcher").count() == 0:
            page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher").first).to_be_visible(timeout=10000)
        chat_row = page.locator(
            ".new-tab-launcher-section[data-section='in-project'] .new-tab-launcher-row", has_text="test-agent"
        ).first
        expect(chat_row).to_be_visible(timeout=10000)
        expect(chat_row).to_contain_text(re.compile(r"just now|\dm ago"))

        # And the stamp itself survived: still stored, never moved backwards.
        stored_ms_after_reload = _member_last_used(layout_dir).get(_FIXTURE_CHAT_REF)
        assert stored_ms_after_reload is not None, "the reload lost the chat's recency from the store"
        assert stored_ms_after_reload >= stored_ms_before_reload, (
            f"the chat's recency moved backwards across the reload: "
            f"{stored_ms_before_reload} -> {stored_ms_after_reload}"
        )


@pytest.mark.timeout(120, func_only=False)
def test_launcher_kind_filter_hides_a_kind_and_reset_restores_it(tmp_path: Path, page: Page) -> None:
    """Unchecking a kind in a table's filter hides its rows; Reset re-checks all.

    The filter is a checkbox menu per table: one row per kind the table holds,
    plus a reset. Unchecking "Chats" must drop the chat rows and ONLY the chat
    rows -- the app instance seeded beside them stays -- and "Reset filters"
    must bring them straight back. The machine offers an extra agent and an
    app so the "On this machine" table holds two kinds to tell apart.
    """
    with _running_e2e_server(
        tmp_path,
        _LAUNCHER_FILTER_PORT,
        primary_agent_id="primary-filter-agent",
        additional_agents=(("agent-filter-999", "filter-agent"),),
        apps=("docs-viewer",),
    ) as (base_url, _agent_info, _session_file):
        # Apps list as their INSTANCES, and an instance exists while something
        # references it -- so the machine table only holds an app row once one
        # is referenced somewhere. Seed one docs-viewer instance as a member of
        # a second project: the client mounts the first project (a fresh
        # browser lands on projects[0], the starter), so the instance lands in
        # its "On this machine" table rather than the in-project one. The
        # primary agent id is what gives the project endpoints a layout dir to
        # persist into -- without one the seed POSTs answer 500 / drop the ref.
        seed_request = urllib.request.Request(
            f"{base_url}/api/projects",
            data=json.dumps({"name": "Seed", "color": "#3B82F6", "glyph": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(seed_request, timeout=5) as response:
            assert response.status == 200
        member_request = urllib.request.Request(
            f"{base_url}/api/projects/seed/members",
            data=json.dumps({"ref": "service:docs-viewer?instance=docs-viewer-1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(member_request, timeout=5) as response:
            assert response.status == 200
        page.goto(base_url)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)

        # Both kinds start visible in the machine-wide table.
        section = page.locator(".new-tab-launcher-section[data-section='on-machine']")
        chat_row = section.locator(".new-tab-launcher-row", has_text="filter-agent")
        app_row = section.locator(".new-tab-launcher-row", has_text="docs-viewer")
        expect(chat_row).to_have_count(1, timeout=10000)
        expect(app_row).to_have_count(1)

        # Uncheck "Chats" in this table's filter menu: the chat rows go, and
        # nothing else does.
        section.locator("button[aria-expanded]").click()
        chats_checkbox = section.locator("label", has_text="Chats")
        expect(chats_checkbox).to_be_visible(timeout=5000)
        chats_checkbox.click()
        expect(chat_row).to_have_count(0)
        expect(app_row).to_have_count(1)

        # Reset re-checks everything, so the chat rows come straight back.
        section.locator("button", has_text="Reset filters").click()
        expect(chat_row).to_have_count(1)
        expect(app_row).to_have_count(1)


_TAB_OVERFLOW_PORT = 18880


@pytest.mark.timeout(180, func_only=False)
def test_overflowed_tabs_list_as_plain_rows_and_the_strip_keeps_its_handles(tmp_path: Path, page: Page) -> None:
    """Tabs folded into the "N more" dropdown list as bare rows; the strip stays whole.

    When the strip runs out of room, dockview tucks the hidden tabs behind an
    "N more" control whose dropdown builds a FRESH instance of the custom tab
    renderer per row. A row's whole job is to focus its tab, so it carries the
    kind icon and the title and none of the strip's machinery: no minus, no
    kebab, and nothing waiting behind a hover either -- close and shut-down
    belong on the strip, where the tab is wide enough to hit the right one.

    The other half is the regression this pins. While the dropdown is open,
    two live renderer instances exist for one panel -- the strip's and the
    row's -- and only the strip's may own the panel's entry in the handle
    registry (the title fade and the launcher flash reach through it) and its
    own controls. A row that claimed the entry, or tore the strip's instance
    down on dispose when the dropdown closed, would leave the strip tab inert.
    So after the dropdown has opened and closed, the strip tab must still
    reveal its own controls and open its own menu.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _TAB_OVERFLOW_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        # Narrow enough that a handful of tabs overflow the strip: every tab
        # keeps at least 140px plus the strip's reserved space, so five or six
        # tabs exhaust a 900px window and the cap below has real headroom.
        page.set_viewport_size({"width": 900, "height": 700})
        page.goto(base_url)

        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        # Fill the strip from the launcher's Terminal tile, exactly as the
        # user would; each tab derives "Terminal N" from its allocated session
        # (the numbers depend on the shared tmux socket). No tmux session
        # comes into being here -- session creation is lazy (on ttyd attach)
        # and no terminal service runs in this harness, exactly as in the
        # derived-name test above.
        #
        # All eight are created rather than stopping at the first sign of the
        # "N more" control: stopping there leaves exactly one tab folded away,
        # and which one that is depends on how wide the runner's font draws the
        # titles -- the fixture chat on one machine, a terminal on another. Every
        # tab keeps at least 140px, so eight terminals plus the chat cannot fit
        # in a 900px window whatever the metrics, and several tabs are folded
        # away. The chat is only one of them, so a terminal is certainly among
        # them, which is what the name assertions below need.
        titles_seen: set[str] = set()
        for _ in range(8):
            _create_terminal_from_launcher(page, titles_seen)

        # The fold is observer-driven, so give it a beat to catch up.
        overflow_control = page.locator(".dv-tabs-overflow-dropdown-default")
        wait_for(
            lambda: overflow_control.is_visible(),
            timeout=5.0,
            poll_interval=0.1,
            error_message="the strip never overflowed: a chat plus 8 terminals all fit at 900px wide",
        )

        # Open the dropdown: the hidden tabs are listed ...
        overflow_control.click()
        container = page.locator(".dv-tabs-overflow-container")
        expect(container).to_be_visible(timeout=5000)
        rows = container.locator(".dv-default-tab-content")
        expect(rows.first).to_be_visible(timeout=5000)

        # ... under the names the strip says, not the creation-time snapshot.
        # A terminal's panel comes into being titled by a placeholder and is
        # retitled to the derived "Terminal N" once its session name is
        # allocated, and dockview seeds each dropdown row's renderer from the
        # panel's ORIGINAL init parameters -- so a row that read its title from
        # those would say ``terminal-N`` (or the placeholder) here while the
        # strip says "Terminal N".
        expect(
            container.locator(".dv-default-tab-content", has_text=re.compile(r"^Terminal \d+$")).first
        ).to_be_visible(timeout=5000)
        expect(container.locator(".dv-default-tab-content", has_text=re.compile(r"^terminal-\d+$"))).to_have_count(0)

        # ... as bare rows: no controls revealed, and none hidden either. The
        # hover matters because that is the gesture the strip reveals its
        # minus and kebab on; the dropdown must have nothing to reveal.
        expect(container.locator(".dv-custom-tab-actions")).to_have_count(0)
        expect(container.locator(".dv-custom-tab-action")).to_have_count(0)
        rows.first.hover()
        expect(container.locator(".dv-custom-tab-actions")).to_have_count(0)
        expect(container.locator(".dv-custom-tab-action")).to_have_count(0)

        # A row's one job: clicking it closes the popover and puts its tab in
        # front on the strip.
        clicked_title = rows.first.inner_text()
        rows.first.click()
        expect(page.locator(".dv-tabs-overflow-container")).to_have_count(0, timeout=5000)
        expect(page.locator(".dv-tab.dv-active-tab .dv-default-tab-content")).to_have_text(clicked_title, timeout=5000)

        # The regression: the dropdown built (and, on close, disposed) a
        # second renderer instance for that panel, and the strip's own
        # instance must have survived it -- still revealing its controls on
        # hover, and still opening its menu from the kebab.
        strip_tab = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text=clicked_title)).first
        strip_tab.hover()
        expect(strip_tab.locator(".dv-custom-tab-action")).to_have_count(2, timeout=5000)
        strip_tab.locator('.dv-custom-tab-action[aria-label="Tab options"]').click()
        expect(page.locator("[role='menuitem']", has_text="Close tab")).to_be_visible(timeout=5000)
        page.keyboard.press("Escape")


_DRAG_OVERLAY_PORT = 18881


@pytest.mark.timeout(180, func_only=False)
def test_dropping_on_a_tab_draws_a_line_and_on_a_pane_draws_a_wash(tmp_path: Path, page: Page) -> None:
    """A drop onto a tab is a seam; a drop onto a pane is a region.

    Dropping onto a tab asks "before or after this one?", which is a position
    BETWEEN two tabs rather than an area, so it draws as a thin insertion line
    against the edge the tab would land on. dockview thins its own overlay into
    a line only when the target is under 100px wide, and the strip holds every
    tab at a 140px floor, so left alone a tab drop paints a block over half of
    its neighbour -- which reads as "replace that half" rather than "insert
    here".

    Dropping into a pane still asks "which side?", which IS a region, so that
    one keeps a filled wash. Both are asserted in one test because the pair is
    the point: the same overlay element has to say two different things, and a
    rule that turned tab drops into lines by reaching too widely would take the
    pane's wash with it.

    Computed styles are what get asserted, since this is entirely a question of
    what the user sees: a transparent selection carrying a 2px pseudo-element
    on the correct edge, against a selection whose own background is the wash.
    """
    with _running_e2e_server(tmp_path, _DRAG_OVERLAY_PORT, primary_agent_id="primary-services-agent") as (
        base_url,
        _agent_info,
        _session_file,
    ):
        page.goto(base_url)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        # A second tab, so there is a neighbour to drop against.
        terminal_title = _create_terminal_from_launcher(page, set())

        dragged = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text=terminal_title)).first
        target_tab = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text="test-agent")).first
        source_box = dragged.bounding_box()
        assert source_box is not None, "the dragged tab has no box"
        page.mouse.move(source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
        page.mouse.down()

        # Measured mid-drag: the strip re-lays its tabs out once one of them is
        # being carried, so a box read before the press points somewhere else by
        # the time the pointer arrives.
        target_box = target_tab.bounding_box()
        assert target_box is not None, "the target tab has no box"
        page.mouse.move(
            target_box["x"] + target_box["width"] * 0.2, target_box["y"] + target_box["height"] / 2, steps=25
        )
        # The overlay animates between targets; let it arrive before reading it.
        page.wait_for_timeout(400)
        target_box = target_tab.bounding_box()
        assert target_box is not None, "the target tab lost its box mid-drag"
        tab_overlay = _drop_overlay_styles(page)
        assert tab_overlay is not None, "no drop overlay appeared over the tab"

        # The selection itself paints nothing -- the line is the whole of it --
        # and the line sits against the tab's own left edge rather than halfway
        # across it.
        assert tab_overlay["background"] == "rgba(0, 0, 0, 0)", (
            f"a tab drop should not wash the tab, got {tab_overlay['background']}"
        )
        assert tab_overlay["afterContent"] not in ("none", ""), "the tab drop drew no insertion line"
        assert tab_overlay["afterWidth"] == "2px", f"the insertion line should be 2px, got {tab_overlay['afterWidth']}"
        assert tab_overlay["afterBackground"] != "rgba(0, 0, 0, 0)", "the insertion line is invisible"

        # And it is on a seam: whichever side dockview picked, the line is drawn
        # against that edge of the whole tab rather than halfway across it.
        assert tab_overlay["side"] in ("left", "right"), (
            f"a drop onto a tab should pick a side, got {tab_overlay['side']}"
        )
        line_x = tab_overlay["left"] if tab_overlay["side"] == "left" else tab_overlay["right"]
        seam_x = target_box["x"] if tab_overlay["side"] == "left" else target_box["x"] + target_box["width"]
        assert abs(line_x - seam_x) <= 1, (
            f"the {tab_overlay['side']} line should sit on that edge of the tab ({seam_x}), got {line_x}"
        )

        # The same drag over the pane body keeps the region wash, and grows no
        # line: the two answers stay different.
        pane_box = page.locator(".dv-content-container").first.bounding_box()
        assert pane_box is not None, "the pane has no box"
        page.mouse.move(pane_box["x"] + pane_box["width"] * 0.15, pane_box["y"] + pane_box["height"] / 2, steps=25)
        pane_overlay = _drop_overlay_styles(page)
        assert pane_overlay is not None, "no drop overlay appeared over the pane"
        assert pane_overlay["background"] != "rgba(0, 0, 0, 0)", "a pane drop should still show its region"
        assert pane_overlay["afterContent"] in ("none", ""), "a pane drop should not draw an insertion line"
        page.mouse.up()


_RAIL_HOVER_CONSISTENCY_PORT = 18882


@pytest.mark.timeout(120, func_only=False)
def test_rail_hover_expansion_holds_a_fixed_layout_and_only_a_pointer_leave_closes_it(
    tmp_path: Path, page: Page
) -> None:
    """Expanding the rail must never reflow it, and picking a row inside it must
    never force it shut.

    The rail is absolutely positioned inside a fixed 37px slot and expands over
    the dock by growing width alone (see Sidebar's module docstring), so a row
    shared by both states -- the header, a shortcut -- has to sit at the exact
    same y whether the rail is collapsed or expanded. If some row above it were
    ever conditionally rendered instead of held in the DOM at fixed height, every
    row below would jump down the moment the rail opened.

    Separately, `Sidebar.pick()` deliberately does not collapse the rail the way
    `closeMenus()` does: a row picked from inside the rail's own (still-hovered)
    card leaves the pointer resting on the rail, and forcing `expanded` false
    there used to snap the rail shut under a pointer that never left it. Only the
    real `onmouseleave` -- the pointer actually going -- collapses it now. Both
    regressions are easy to reintroduce independently (a new row inserted above
    the shortcuts, a shortcut's onclick reaching for `closeMenus` instead of
    `pick`), so they are pinned together here.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _RAIL_HOVER_CONSISTENCY_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        page.goto(base_url)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        rail = page.locator(".machine-sidebar")
        header = page.locator(".project-rail-header")
        # A shortcut row rather than the header: it sits below both the header
        # and the divider, so a shift in either one shows up here too.
        browser_shortcut = page.locator(".project-rail-shortcut", has_text="Browser")

        # Collapsed: the pointer starts off the rail, and the search pill (an
        # expanded-only row) is the signal that it is actually closed.
        page.mouse.move(600, 400)
        expect(page.locator(".project-rail-search")).to_have_count(0, timeout=5000)
        header_collapsed = header.bounding_box()
        shortcut_collapsed = browser_shortcut.bounding_box()
        assert header_collapsed is not None and shortcut_collapsed is not None

        # Expanded: hovering grows the rail's width, but the reference rows must
        # not move or resize vertically -- only their width (and the label that
        # fades in) changes.
        rail.hover()
        expect(page.locator(".project-rail-search")).to_be_visible(timeout=5000)
        header_expanded = header.bounding_box()
        shortcut_expanded = browser_shortcut.bounding_box()
        assert header_expanded is not None and shortcut_expanded is not None
        assert header_expanded["width"] > header_collapsed["width"], "hovering never actually expanded the rail"
        assert header_collapsed["y"] == header_expanded["y"], "the header row shifted vertically on expansion"
        assert header_collapsed["height"] == header_expanded["height"], "the header row's height changed on expansion"
        assert shortcut_collapsed["y"] == shortcut_expanded["y"], "a shortcut row shifted vertically on expansion"
        assert shortcut_collapsed["height"] == shortcut_expanded["height"], (
            "a shortcut row's height changed on expansion"
        )

        # Picking a shortcut from the still-hovered rail must not force it shut:
        # the pointer is still resting on it (Playwright's .click() moves the
        # virtual pointer onto the element first), so it should read exactly as
        # it did the instant before the click.
        page.locator(".project-rail-shortcut", has_text="Terminal").click()
        expect(page.locator(".dv-default-tab-content", has_text=_TERMINAL_TAB_TITLE_RE).first).to_be_visible(
            timeout=10000
        )
        expect(page.locator(".project-rail-search")).to_be_visible(timeout=1000)

        # Only the pointer actually leaving closes it.
        page.mouse.move(600, 400)
        expect(page.locator(".project-rail-search")).to_have_count(0, timeout=5000)


# A conversation whose transcript ends with an unresolved queue-operation/enqueue,
# so the Claude queue populator surfaces one currently-queued message. Shaped like
# the real records (a normal exchange, then an enqueue line the watcher feeds to
# the tracker); the DOM assertions below match the real queued-group structure.
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
    # A second turn is IN FLIGHT (user tail, no reply yet): a queued message only
    # stays parked while the agent is working -- an idle agent's queue is drained
    # by definition (the arrival-time sweep enforces exactly that), so the fixture
    # must model the mid-turn state or the queued group is correctly swept away.
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


@pytest.mark.timeout(30, func_only=False)
def test_queued_message_group_renders_with_actions(tmp_path: Path, page: Page) -> None:
    """A harness-queued message renders as a distinct group with the two actions.

    Drives the whole Claude path end to end: the watcher feeds the trailing
    ``enqueue`` to the queue populator, the backend pushes the ``queued_messages``
    snapshot over the agents WebSocket, and the frontend renders the queued group
    (reusing the user-bubble view) with the shoulder-tap and interrupt-to-composer
    buttons above it.
    """
    with _running_e2e_server(tmp_path, _PORT + 5, session_events=_QUEUED_SESSION_EVENTS) as (base_url, _, _):
        page.goto(base_url)

        # The committed turn renders as usual...
        expect(_chat(page).locator(".message-user", has_text="Kick off the big refactor").first).to_be_visible(
            timeout=15000
        )

        # ...and the queued message shows as a distinct group, reusing the
        # user-bubble view (not the transcript classifier).
        group = _chat(page).locator(".queued-group")
        expect(group).to_be_visible(timeout=15000)
        bubble = _chat(page).locator(".queued-message .message-user-bubble .message-content")
        expect(bubble).to_contain_text("actually also update the changelog")

        # The header row: 'Queued messages' label on the left, the shoulder-tap
        # button (with its exact tooltip) on the right. No interrupt button here --
        # interrupt-to-composer moved to the composer Stop button.
        expect(_chat(page).locator(".queued-header-label")).to_contain_text("Queued messages")
        flush_button = _chat(page).locator(".queued-action--flush")
        expect(flush_button).to_be_visible()
        expect(flush_button).to_contain_text("Shoulder tap")
        expect(flush_button).to_have_attribute(
            "data-tooltip", "Gently interrupt your agent to send queued messages early"
        )
        expect(_chat(page).locator(".queued-action--interrupt")).to_have_count(0)

        page.screenshot(path=str(tmp_path / "queued_group.png"))


_LOAD_SWITCHES_VIEW_PORT = 18883


@pytest.mark.timeout(180, func_only=False)
def test_load_op_switches_the_clients_view(tmp_path: Path, page: Page) -> None:
    """``layout.py load <view>`` switches what the connected client is showing.

    The op resolved and broadcast for as long as views have existed, but no
    client listened: the CLI reported success while nothing on screen moved.
    The client now applies a load addressed to it (or to everyone) by running
    the same ``switchToView`` the rail's own switcher uses, so an agent can
    put a view in front of the user -- Everything included, which is the view
    an agent most often wants when it needs the whole machine visible.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LOAD_SWITCHES_VIEW_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        page.goto(base_url)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )

        # The load names Everything by its display name, exactly as an agent
        # would type it; the server resolves it even though Everything has no
        # registry entry. Target every client (no explicit client id), and
        # retry through the 412 window while client_state registration lands.
        payload = json.dumps(
            {"op": "load", "args": {"layout": EVERYTHING_VIEW_NAME}, "agent_id": "agent-e2e"}
        ).encode()
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
                raise

        wait_for(
            _attempt,
            timeout=15.0,
            poll_interval=0.2,
            error_message="the load op never got past client registration",
        )

        # The client switched: its stored view id moved to Everything, and the
        # dock re-mounted (the launcher, since fresh Everything has no content).
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=15000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)

        # Everything's launcher enumerates the machine, terminals included, but
        # its fetch races this test's short window -- and the module-wide tmux
        # mark (see pytestmark) requires every test here to actually reach
        # tmux. Ask the server directly, which is the same ``tmux ls`` the
        # launcher's table is built from.
        with urllib.request.urlopen(f"{base_url}/api/terminals", timeout=5) as response:
            assert response.status == 200


_MOBILE_AUTOSAVE_PORT = 18884

# A phone-shaped browser context: what Playwright's Pixel device descriptors
# hold, inlined so the emulated UA is pinned rather than drifting with the
# Playwright version. The UA string is what matters -- emulation exposes no
# ``navigator.userAgentData``, so the client classifies itself as mobile via
# the UA-string fallback (see ClientIdentity.classifyDeviceKind).
_MOBILE_CONTEXT_ARGS: dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "viewport": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.mark.timeout(120, func_only=False)
def test_mobile_client_saves_its_own_arrangement(tmp_path: Path, page: Page) -> None:
    """A mobile client's autosave lands in the view's mobile file, not desktop's.

    Views are arranged per device: the frontend derives its own kind from the
    UA and routes load + autosave to ``<id>.mobile.json``. The desktop
    arrangement must stay untouched -- absent here, since no desktop client
    ever saved.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _MOBILE_AUTOSAVE_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        # A second, phone-shaped context on the same browser the ``page``
        # fixture runs in (that fixture's own page goes unused here).
        e2e_browser = page.context.browser
        assert e2e_browser is not None
        context = e2e_browser.new_context(**_MOBILE_CONTEXT_ARGS)
        try:
            mobile_page = context.new_page()
            mobile_page.goto(base_url)

            # The fixture chat auto-opens in the starter project; the debounced
            # autosave then writes the arrangement out -- into the mobile file.
            expect(mobile_page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(
                timeout=15000
            )
            wait_for(
                lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.mobile.json").exists(),
                timeout=15.0,
                poll_interval=0.1,
                error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.mobile.json",
            )
            assert not (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists()
        finally:
            context.close()

        # Touch tmux deterministically for the module-wide mark, exactly as the
        # load test above does: the launcher's own fetch races this short test.
        with urllib.request.urlopen(f"{base_url}/api/terminals", timeout=5) as response:
            assert response.status == 200


_TRANSCRIPT_RECOVERY_PORT = 18885


@pytest.mark.timeout(120, func_only=False)
def test_chat_recovers_from_a_failed_transcript_load(tmp_path: Path, page: Page) -> None:
    """A chat whose transcript fetch failed recovers on Refresh, without reloading the page.

    The reported shape of this: a laptop wakes, the proxy in front of the
    workspace answers ``/events`` with a 503 while its tunnel is re-dialled, and
    the panel is left on an error screen. Refresh re-fetched the transcript
    successfully -- the request was visible in the network tab -- but the panel
    kept rendering the error over it, because the failure was held by the panel
    and only its own first load ever cleared it. Nothing short of reloading the
    whole page brought the chat back.

    Both halves of that are asserted here: the error names the status rather
    than reading "Error: null", and the Refresh the error screen itself offers
    restores the transcript. The button is deliberately the one exercised --
    recovery already took a single reload before it existed, but the only
    control that did one lived in the tab's overflow menu, which nothing on the
    error screen pointed at.
    """
    with _running_e2e_server(tmp_path, _TRANSCRIPT_RECOVERY_PORT) as (base_url, _agent_info, _session_file):
        # Stand in for the proxy's 503. The plain-text body matters: it is not
        # JSON, so the request layer has no detail to report and must fall back
        # to the status rather than to mithril's stringified empty body. Only
        # the transcript snapshot is intercepted; the SSE stream beside it stays
        # healthy, so nothing retries in the background and Refresh is the sole
        # recovery path under test.
        events_url = "**/api/agents/*/events"
        page.route(
            events_url,
            lambda route: route.fulfill(status=503, content_type="text/plain", body="Backend not yet available"),
        )
        page.goto(base_url)

        error = _chat(page).locator(".message-list-error")
        expect(error).to_be_visible(timeout=15000)
        # The message itself, not the whole panel: the Refresh below is part of it.
        expect(error.locator("p")).to_have_text("Error: request failed (HTTP 503)")

        # The workspace becomes reachable again. Nothing tells the panel.
        page.unroute(events_url)

        error.locator(".message-list-reload").click()

        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-error")).to_have_count(0)

        # Touch tmux deterministically for the module-wide mark, as above.
        with urllib.request.urlopen(f"{base_url}/api/terminals", timeout=5) as response:
            assert response.status == 200
