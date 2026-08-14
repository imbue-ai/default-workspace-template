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
import sys
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
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

try:
    from playwright.sync_api import Page
    from playwright.sync_api import expect

    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    _PLAYWRIGHT_IMPORTABLE = False


def _playwright_browsers_installed() -> bool:
    """Check if Playwright browsers are installed by looking for the cache directory."""
    if not _PLAYWRIGHT_IMPORTABLE:
        return False
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        cache_dir = Path(env_path)
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    return cache_dir.exists() and any(cache_dir.iterdir())


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
            },
        ),
        patch("imbue.system_interface.server.discover_agents", return_value=agents),
    ):
        # Seed the agent into a manager and inject it; the manager is never started,
        # so no background mngr discovery runs. Its messenger is a recording fake so
        # message sends succeed without contacting mngr. The UI renders its agent
        # list from the WebSocket agents_updated snapshot, which the server sends
        # from this manager on connect.
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
    content = page.locator(".app-content")
    expect(content).to_be_visible(timeout=15000)
    expect(content.locator(".message-list")).to_have_count(1)

    content_bg = page.eval_on_selector(".app-content", "e => getComputedStyle(e).backgroundColor")
    assert content_bg == "rgb(255, 255, 255)", f"chat transcript area should be pure white, got {content_bg}"

    # The composer footer strip is now unified with the transcript -- also pure white.
    footer_bg = page.eval_on_selector(".app-footer", "e => getComputedStyle(e).backgroundColor")
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
    user_message = page.locator(".message-user")
    expect(user_message.first).to_be_visible(timeout=15000)
    expect(user_message.first).to_contain_text("Hello agent!")


@pytest.mark.timeout(30, func_only=False)
def test_assistant_message_renders(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Assistant messages render with markdown content."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    assistant_message = page.locator(".message-assistant")
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

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=15000)


@pytest.mark.timeout(30, func_only=False)
def test_send_button_appears_on_input(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The send button appears only once the composer has text."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=15000)

    # The send button is not rendered until the composer can send (non-empty).
    send_button = page.locator(".message-input-send-button")
    expect(send_button).to_have_count(0)

    # Type some text -- the send button now appears.
    textarea.fill("test message")
    expect(send_button).to_be_visible()


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
        expect(page.locator(".message-assistant").first).to_be_visible(timeout=15000)

        tool_block = page.locator(".tool-call-block").first
        expect(tool_block).to_be_visible(timeout=10000)
        # The header names the tool.
        expect(tool_block).to_contain_text("Read")

        # Collapsed by default: the details are not visible until expanded.
        tool_details = page.locator(".tool-call-details").first
        expect(tool_details).to_be_hidden()

        # Clicking the header expands the block, revealing the tool result.
        page.locator(".tool-call-header").first.click()
        expect(tool_details).to_be_visible()
        expect(tool_details).to_contain_text("file contents here")


@pytest.mark.timeout(30, func_only=False)
def test_live_stream_delivers_new_events(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """New events written to the session file appear in the UI as they stream in."""
    base_url, _, session_file = e2e_server
    page.goto(base_url)

    # Wait for initial content
    expect(page.locator(".message-user").first).to_be_visible(timeout=15000)

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
    new_message = page.locator(".message-user", has_text="This is a new streamed message!")
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
    return page.evaluate(
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
        page.wait_for_selector(".message-list", timeout=15000)
        page.wait_for_function(
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
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)

        # Scroll up into the middle of the loaded window to read history (well off
        # the live tail, but not so far that a backfill to offset 0 is triggered).
        page.evaluate(
            "() => { const el = document.querySelector('.app-content'); el.scrollTop = el.scrollHeight - el.clientHeight - 1500; }"
        )
        page.wait_for_timeout(1000)
        before_hidden = _visible_user_messages(page)
        scroll_top_before = page.evaluate("() => document.querySelector('.app-content').scrollTop")
        # Sanity: we are reading history, not parked at the start or the tail.
        assert before_hidden, "expected user messages to be rendered after scrolling up"
        assert "msg-0" not in before_hidden, f"setup should not be at the start: {before_hidden[:3]}"
        anchor_message = before_hidden[0]
        assert _min_message_index(before_hidden) >= 50, f"setup should be reading mid-history: {before_hidden[:3]}"

        # Hide the chat by switching to the sibling tab.
        _broadcast_layout_op(base_url, "focus", {"ref": probe_url}, agent_id="agent-test-123")
        page.wait_for_function(
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
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)
        after_restore = _visible_user_messages(page)
        scroll_top_after = page.evaluate("() => document.querySelector('.app-content').scrollTop")
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
            expect(page.locator(".message-list")).to_have_count(0)
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
    Moving the pointer away is what a user does without thinking about it. The
    wait is for the rows the rail only draws while expanded, because folding up
    is a 150ms transition rather than an instant.
    """
    page.mouse.move(600, 400)
    expect(page.locator(".project-rail-search")).to_have_count(0, timeout=5000)


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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(page.locator(".message-list-empty")).to_have_count(0)
        expect(page.locator(".message-list-not-found")).to_have_count(0)


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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(page.locator(".message-list-empty")).to_have_count(0)
        expect(page.locator(".message-list-not-found")).to_have_count(0)


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
    .filter((surface) => surface.querySelector('.message-user') !== null);
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
            lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists()
            and "e2e-live-page.example" in (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").read_text(),
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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

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
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

        back_in_project = page.evaluate(_CHAT_SURFACE_REPORT_JS, None)
        assert back_in_project["count"] == 1, f"switching back forked the chat page: {back_in_project}"
        assert back_in_project["shownCount"] == 1, f"the chat page is not on screen again: {back_in_project}"
        assert back_in_project["stamps"] == ["the-original-element"], (
            f"switching back re-created the chat's element: {back_in_project}"
        )
        assert back_in_project["removals"] == 0, (
            f"a live surface left the DOM during the round trip: {back_in_project}"
        )


_TAB_RENAME_PORT = 18872

# Comfortably past the workspace's 1500ms autosave debounce, so a test can wait
# for the writes an action already triggered to land before provoking the next.
AUTOSAVE_SETTLE_MS = 3000

_FIXTURE_CHAT_REF = "chat:agent-test-123"


def _member_titles(layout_dir: Path) -> dict[str, str]:
    """Every name the user has given an object, straight out of the store on disk.

    The store is the machine's, not a view's, so this is read from the layout
    dir itself rather than from under ``projects/`` -- which is the whole point
    of the tests below. A workspace where nothing has been renamed has no file
    at all, which reads as an empty map.
    """
    titles_path = layout_dir / "member_titles.json"
    if not titles_path.exists():
        return {}
    title_by_ref = json.loads(titles_path.read_text())["title_by_ref"]
    assert isinstance(title_by_ref, dict)
    return title_by_ref


def _saved_panel_params(layout_dir: Path, project_id: str) -> dict[str, Any]:
    """One view's saved per-panel params, as autosaved."""
    saved = json.loads((layout_dir / "projects" / f"{project_id}.json").read_text())
    panel_params = saved["panelParams"]
    assert isinstance(panel_params, dict)
    return panel_params


def _saved_panel_titles(layout_dir: Path, project_id: str) -> list[str]:
    """The titles dockview serialized into one view's saved layout.

    Dockview writes down whatever is on the tab strip, so this is what a view
    would draw if it drew names from its own saved layout -- which is exactly
    what a rename must no longer depend on.
    """
    saved = json.loads((layout_dir / "projects" / f"{project_id}.json").read_text())
    panels = saved["dockview"]["panels"]
    assert isinstance(panels, dict)
    return [panel["title"] for panel in panels.values() if "title" in panel]


@pytest.mark.timeout(120, func_only=False)
def test_double_click_renames_a_tab_and_the_name_survives_a_reload(tmp_path: Path, page: Page) -> None:
    """Double-clicking a tab's title renames it, and the name is kept.

    The gesture is only half of it. A name that lasted as long as the page would
    not be a rename at all, so the commit has to file the title under the
    object's ref in the machine's title store -- which is what makes the reload
    here a real assertion rather than a formality. It also pins the name to the
    *object*: the pane behind the renamed tab still shows the agent's transcript
    afterwards, so the tab was renamed rather than replaced.

    Where the name landed is asserted too, because that is the difference
    between naming an object and naming a tab: the store holds it, and the
    view's saved ``panelParams`` -- where a name used to go -- holds no name at
    all.
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

        # The fixture chat auto-opens into the starter project wearing the name
        # derived from its agent, which is the name the rename replaces.
        tab_title = page.locator(".dv-default-tab-content", has_text="test-agent").first
        expect(tab_title).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        project_file = layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json"
        wait_for(
            lambda: project_file.exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.json",
        )

        # Let the opening flurry of autosaves settle first, so the panel params
        # inspected at the end are the ones this view saved with the chat open
        # rather than a half-written intermediate.
        page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
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

        # The commit files the name under the object's ref. Without that the
        # reload below would find the old one.
        wait_for(
            lambda: _member_titles(layout_dir).get(_FIXTURE_CHAT_REF) == "Design notes",
            timeout=15.0,
            poll_interval=0.1,
            error_message="the rename never reached the machine's title store",
        )

        # Nothing wrote a name into the view. The saved layout still carries the
        # panel's title, because dockview serializes what is on the strip, but
        # the params beside it -- the ``customTitle`` a rename used to write, and
        # the only thing the client would read back as a name -- hold none. Read
        # once the save the rename may have provoked has had time to land.
        page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
        for params in _saved_panel_params(layout_dir, DEFAULT_PROJECT_ID).values():
            assert "customTitle" not in params, f"a rename wrote a name into the view's layout: {params}"

        page.reload()
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=15000)
        # Still the chat that was renamed, not a tab that merely kept a string.
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        # And the derived name is gone rather than restored onto a second tab.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)


_AUTO_TITLE_PORT = 18878


@pytest.mark.timeout(120, func_only=False)
def test_ui_created_terminal_wears_an_auto_filed_friendly_name(tmp_path: Path, page: Page) -> None:
    """A terminal created from the UI comes into being already named "Terminal 1".

    No create flow asks the user for a name: the tmux session name
    (``terminal-N``) stays the identity, machine-allocated and never surfaced
    as something to pick, and the create files the first free "Terminal N"
    into the machine's title store the moment the session name is allocated --
    exactly as if the user had renamed it. The store on disk is asserted
    alongside the strip because that is what makes it a name rather than a tab
    title: it is keyed by the terminal's ref, where every view (and a reload)
    reads it from, and where double-click rename writes over it.
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

        # The launcher's Terminal tile creates directly -- no naming dialog
        # ever appears.
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()
        expect(page.locator(".custom-url-dialog")).to_have_count(0)

        # The tab repaints to the friendly name once the title write lands
        # (the strings differ by more than case -- "Terminal 1" never matches
        # the derived ``terminal-N`` -- so this is the auto-filed name).
        expect(page.locator(".dv-default-tab-content", has_text="Terminal 1").first).to_be_visible(timeout=10000)

        # And it landed in the machine's store, keyed by the terminal's ref,
        # whose body is the machine-allocated session name the user never saw
        # a prompt for.
        titles = _member_titles(layout_dir)
        terminal_titles = {ref: title for ref, title in titles.items() if ref.startswith("terminal:terminal-")}
        assert list(terminal_titles.values()) == ["Terminal 1"], f"unexpected titles: {titles}"

        # A second create counts on: "Terminal 1" is taken (by the title just
        # filed), so the next free slot is "Terminal 2".
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()
        expect(page.locator(".dv-default-tab-content", has_text="Terminal 2").first).to_be_visible(timeout=10000)

        titles = _member_titles(layout_dir)
        terminal_titles = {ref: title for ref, title in titles.items() if ref.startswith("terminal:terminal-")}
        assert sorted(terminal_titles.values()) == ["Terminal 1", "Terminal 2"], f"unexpected titles: {titles}"


_TWO_VIEW_RENAME_PORT = 18874


@pytest.mark.timeout(180, func_only=False)
def test_renaming_an_object_in_one_view_names_it_in_the_other(tmp_path: Path, page: Page) -> None:
    """A rename names the OBJECT, so the other view showing it says the new name.

    This is the reason names are filed by ref, so it is asserted across two
    views rather than inside one. The same chat is shown in the starter project
    and in Everything -- one object in two views, which the many-to-many model
    makes ordinary -- renamed in the starter project, and then read back in
    Everything. Under the arrangement this replaces, where the name was kept on
    the panel and therefore in one view's saved layout, Everything would still
    be reading the name derived from the agent.

    The reload afterwards is the other half. Everything is where the page lands,
    so what it re-reads is the view the rename was *not* done in: the name has
    to come back from the machine's store rather than from the layout that
    happened to be saved with it on the strip.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _TWO_VIEW_RENAME_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The fixture chat auto-opens in the starter project wearing the name
        # derived from its agent.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )

        # Show the SAME object in Everything too. Opening it from Everything's
        # machine-wide table adds it there and takes it from nowhere, so after
        # this both views list the one chat.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        page.locator(".new-tab-launcher-row:visible", has_text="test-agent").first.click()
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        wait_for(
            lambda: (layout_dir / "projects" / f"{EVERYTHING_VIEW_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message="autosave never materialized everything.json",
        )

        # Rename it in the starter project, which is the only view touched from
        # here on.
        _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        _collapse_rail(page)
        tab_title = page.locator(".dv-default-tab-content", has_text="test-agent").first
        expect(tab_title).to_be_visible(timeout=15000)
        tab_title.dblclick()
        editor = page.locator(".dv-custom-tab-title-input:visible")
        expect(editor).to_be_visible(timeout=5000)
        editor.fill("Design notes")
        editor.press("Enter")
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=5000)
        wait_for(
            lambda: _member_titles(layout_dir).get(_FIXTURE_CHAT_REF) == "Design notes",
            timeout=15.0,
            poll_interval=0.1,
            error_message="the rename never reached the machine's title store",
        )

        # Everything was not mounted for any of that, so its saved layout is
        # untouched and still names the panel the old way. That is what makes
        # the next few lines an assertion about the object rather than about a
        # layout: whatever Everything is about to draw, it cannot have got the
        # new name from its own file.
        everything_titles = _saved_panel_titles(layout_dir, EVERYTHING_VIEW_ID)
        assert "test-agent" in everything_titles, f"Everything's saved layout lost the old name: {everything_titles}"
        assert "Design notes" not in everything_titles, (
            f"the rename reached a view that was not even mounted: {everything_titles}"
        )

        # The point: the other view says the new name, and says the old one
        # nowhere.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=15000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

        # It is still the name after a reload -- read back here, in the view the
        # rename was not done in ...
        page.reload()
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{EVERYTHING_VIEW_ID}'", timeout=10000
        )
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=15000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)

        # ... and in the one it was.
        _switch_view_via_rail(page, DEFAULT_PROJECT_NAME)
        page.wait_for_function(
            f"localStorage.getItem('si-active-project-id') === '{DEFAULT_PROJECT_ID}'", timeout=10000
        )
        expect(page.locator(".dv-default-tab-content", has_text="Design notes").first).to_be_visible(timeout=15000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)


_TERMINAL_DESTROY_PORT = 18879


def _terminal_session_names(base_url: str) -> set[str]:
    """The live user-terminal session names, as the server's terminals API reports them."""
    with urllib.request.urlopen(f"{base_url}/api/terminals", timeout=5.0) as response:
        payload = json.loads(response.read())
    return {terminal["session_name"] for terminal in payload["terminals"]}


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
    the tab leaves the mounted project, the session leaves tmux, the name
    leaves the machine's title store, the panel leaves Everything's saved
    content -- and, the regression, mounting Everything afterwards draws no
    terminal tab and respawns no session.
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
        page.locator(".dockview-add-tab-button").first.click()
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()
        expect(page.locator(".dv-default-tab-content", has_text="Terminal 1").first).to_be_visible(timeout=10000)

        # The machine-allocated session name, read back from the ref the
        # auto-filed "Terminal 1" was keyed under -- never hardcoded, because
        # the allocator hands out the lowest ``terminal-N`` the socket is not
        # already using.
        wait_for(
            lambda: any(ref.startswith("terminal:") for ref in _member_titles(layout_dir)),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the terminal's auto-filed name never reached the title store",
        )
        terminal_ref = next(ref for ref in _member_titles(layout_dir) if ref.startswith("terminal:"))
        session_name = terminal_ref.removeprefix("terminal:")

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
            page.locator(".new-tab-launcher-row:visible", has_text="Terminal 1").first.click()
            expect(page.locator(".dv-default-tab-content", has_text="Terminal 1").first).to_be_visible(timeout=15000)
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
                ".dv-tab", has=page.locator(".dv-default-tab-content", has_text="Terminal 1")
            ).first
            expect(terminal_tab).to_be_visible(timeout=15000)
            terminal_tab.hover()
            terminal_tab.locator(".dv-custom-tab-action").last.click()
            page.locator("[role='menuitem']", has_text="Shut down terminal").click()
            page.locator(".destroy-dialog-btn-destroy").click()

            # The whole blast radius. The tab leaves the mounted project ...
            expect(page.locator(".dv-default-tab-content", has_text="Terminal 1")).to_have_count(0, timeout=10000)
            # ... the session leaves tmux ...
            wait_for(
                lambda: session_name not in _terminal_session_names(base_url),
                timeout=15.0,
                poll_interval=0.1,
                error_message=f"the destroy left tmux session {session_name} running",
            )
            # ... the name leaves the machine's title store ...
            wait_for(
                lambda: terminal_ref not in _member_titles(layout_dir),
                timeout=15.0,
                poll_interval=0.1,
                error_message="the destroy left the terminal's name in the title store",
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
            expect(page.locator(".dv-default-tab-content", has_text="Terminal 1")).to_have_count(0)
            expect(page.locator(".dv-default-tab-content", has_text="terminal-")).to_have_count(0)

            # And after the time an attach-or-create would have needed, the
            # mount still spawned nothing: no terminal session is live now that
            # was not already live before this test created anything.
            page.wait_for_timeout(AUTOSAVE_SETTLE_MS)
            expect(page.locator(".dv-default-tab-content", has_text="Terminal 1")).to_have_count(0)
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


_SETTINGS_STAGING_PORT = 18873


def _open_project_settings(page: Page) -> None:
    """Open the active project's settings modal from the rail header's context menu."""
    page.locator(".project-rail-header").click(button="right")
    page.locator(".project-rail-menu [role='menuitem']", has_text="Project settings").click()
    expect(page.locator(".custom-url-dialog-title", has_text="Project settings")).to_be_visible(timeout=5000)


def _project_members(layout_dir: Path) -> list[str]:
    """The starter project's member refs, straight out of the registry on disk."""
    registry = json.loads((layout_dir / "projects_meta.json").read_text())
    members = registry["project_by_id"][DEFAULT_PROJECT_ID]["members"]
    assert isinstance(members, list)
    return members


@pytest.mark.timeout(120, func_only=False)
def test_settings_dialog_stages_removals_until_save(tmp_path: Path, page: Page) -> None:
    """The settings dialog's removals obey its Save button, not the click.

    Marking a row used to remove it there and then, which made it the one
    control in a dialog full of Save/Cancel fields that ignored both: a user who
    marked a row, thought better of it and pressed Cancel would find the object
    already gone from the project. So a marked row is now staged -- struck
    through, counted in the header, and applied only by Save.

    Both halves are asserted against the registry on disk rather than against
    the dialog, because that is what "was it actually removed" means: Cancel
    must leave the member list exactly as it was, and Save must take the ref out
    of it.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _SETTINGS_STAGING_PORT, primary_agent_id=primary_agent_id) as (
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
            lambda: (layout_dir / "projects" / f"{DEFAULT_PROJECT_ID}.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message=f"autosave never materialized {DEFAULT_PROJECT_ID}.json",
        )
        wait_for(
            lambda: _FIXTURE_CHAT_REF in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the fixture chat was never filed as a member of the starter project",
        )

        # Mark the chat for removal, then back out with Cancel.
        _open_project_settings(page)
        chat_row = page.locator(".project-contents-row", has_text="test-agent")
        expect(chat_row).to_have_count(1)
        chat_row.locator("button", has_text="Remove").click()
        # Staged, not applied: the row says how to undo it and the header says
        # what Save would do.
        expect(chat_row.locator("button", has_text="Undo")).to_be_visible()
        expect(page.locator(".custom-url-dialog", has_text="1 to remove on save")).to_be_visible()
        page.locator(".custom-url-dialog-cancel").click()
        expect(page.locator(".custom-url-dialog")).to_have_count(0)

        # Nothing happened. The tab is still docked, and -- after long enough for
        # a removal request to have landed if one had been sent -- the registry
        # still lists the member.
        page.wait_for_timeout(1000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible()
        assert _FIXTURE_CHAT_REF in _project_members(layout_dir), "Cancel removed the member anyway"

        # Cancel threw the marks away with the rest of the form, so reopening
        # offers the row unstaged rather than remembering what was marked.
        _open_project_settings(page)
        chat_row = page.locator(".project-contents-row", has_text="test-agent")
        expect(chat_row.locator("button", has_text="Remove")).to_have_count(1)
        expect(page.locator(".custom-url-dialog", has_text="to remove on save")).to_have_count(0)

        # Mark it again and commit this time.
        chat_row.locator("button", has_text="Remove").click()
        page.locator(".custom-url-dialog-open").click()
        expect(page.locator(".custom-url-dialog")).to_have_count(0)

        # Now it really went: the tab is undocked and the ref is out of the
        # project's member list.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent")).to_have_count(0)
        wait_for(
            lambda: _FIXTURE_CHAT_REF not in _project_members(layout_dir),
            timeout=15.0,
            poll_interval=0.1,
            error_message="Save never removed the member from the project",
        )


_APP_PINNING_PORT = 18875

# The one app the machine offers in the pinning test, and the member ref it is
# filed under. The label is what ``_running_e2e_server`` mints for it, and it is
# what the pane's origin -- and therefore its iframe src -- is built from.
_PINNABLE_APP_NAME = "docs-viewer"
_PINNABLE_APP_REF = f"service:{_PINNABLE_APP_NAME}"
_PINNABLE_APP_LABEL = f"{_PINNABLE_APP_NAME}-e2elabel"

# The "All apps" heading an app's row currently sits under, read by walking back
# up the list to the nearest thing that is not a row. The grouping is what the
# two headings MEAN, so asserting on the heading above the row is what proves an
# app moved between them -- a heading's mere presence would not. The row is found
# by its label span rather than its whole textContent, because the row's glyph
# may be a monogram whose SVG <text> initial leaks into the latter.
_HEADING_ABOVE_APP_JS = """
(appName) => {
  const rows = Array.from(document.querySelectorAll('.project-rail-app'));
  const row = rows.find((candidate) => {
    const label = candidate.querySelector('.truncate');
    return label !== null && label.textContent.trim() === appName;
  });
  if (row === undefined) return null;
  for (let node = row.previousElementSibling; node !== null; node = node.previousElementSibling) {
    if (!node.classList.contains('project-rail-app')) return node.textContent.trim();
  }
  return null;
}
"""


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
    project's member list holds its ``service:<name>`` ref. So "All apps" has
    exactly two headings -- "Pinned in <Project>" and "Unpinned" -- and the round
    trip through them has to move the app on the server, not in this browser:
    pinning puts the ref in the registry on disk and grows a rail shortcut,
    unpinning takes both away again.

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

        # Nothing has pinned this app, so it starts under "Unpinned" -- there is
        # no pinned group at all yet -- and the rail carries no shortcut for it.
        _open_all_apps(page)
        assert page.evaluate(_HEADING_ABOVE_APP_JS, _PINNABLE_APP_NAME) == "Unpinned"
        expect(page.locator(f"text=Pinned in {DEFAULT_PROJECT_NAME}")).to_have_count(0)
        expect(page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME)).to_have_count(0)
        assert _PINNABLE_APP_REF not in _project_members(layout_dir)

        # Pin it. The row moves under the project's own heading, the rail grows
        # a shortcut for it, and -- because pinning IS membership -- the ref
        # lands in the project's member list on disk.
        page.locator(f'button[aria-label="Pin {_PINNABLE_APP_NAME}"]').click()
        page.wait_for_function(
            f"() => ({_HEADING_ABOVE_APP_JS})({json.dumps(_PINNABLE_APP_NAME)}) === "
            f"{json.dumps(f'Pinned in {DEFAULT_PROJECT_NAME}')}",
            timeout=15000,
        )
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

        # Unpin it. It goes back under "Unpinned", the shortcut goes with it,
        # and the ref leaves the project's member list.
        _open_all_apps(page)
        page.locator(f'button[aria-label="Unpin {_PINNABLE_APP_NAME}"]').click()
        page.wait_for_function(
            f'() => ({_HEADING_ABOVE_APP_JS})({json.dumps(_PINNABLE_APP_NAME)}) === "Unpinned"',
            timeout=15000,
        )
        expect(page.locator(".project-rail-shortcut", has_text=_PINNABLE_APP_NAME)).to_have_count(0)
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

        # A launcher may have been restored with the layout; the "+" opens one
        # if not and flashes the existing one if so, so either way one is up.
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
    rows -- the app seeded beside them stays -- and "Reset filters" must bring
    them straight back. The machine offers an extra agent and an app so the
    "On this machine" table holds two kinds to tell apart.
    """
    with _running_e2e_server(
        tmp_path,
        _LAUNCHER_FILTER_PORT,
        additional_agents=(("agent-filter-999", "filter-agent"),),
        apps=("docs-viewer",),
    ) as (base_url, _agent_info, _session_file):
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
    registry that menu-Rename (and the title fade) reach through. A row that
    claimed the entry, or deleted it on dispose when the dropdown closed,
    would leave Rename a silent no-op or pointed at a detached row. So after
    the dropdown has opened and closed, the strip tab's Rename must still
    open the strip's own editor.
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
        # user would; each create auto-names "Terminal N". No tmux session
        # comes into being here -- session creation is lazy (on ttyd attach)
        # and no terminal service runs in this harness, exactly as in the
        # auto-filed-name test above.
        #
        # All eight are created rather than stopping at the first sign of the
        # "N more" control: stopping there leaves exactly one tab folded away,
        # and which one that is depends on how wide the runner's font draws the
        # titles -- the fixture chat on one machine, a terminal on another. Every
        # tab keeps at least 140px, so eight terminals plus the chat cannot fit
        # in a 900px window whatever the metrics, and several tabs are folded
        # away. The chat is only one of them, so a terminal is certainly among
        # them, which is what the name assertions below need.
        for index in range(1, 9):
            page.locator(".dockview-add-tab-button").first.click()
            expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
            page.locator(".new-tab-launcher-tile:visible", has_text="Terminal").click()
            expect(page.locator(".dv-default-tab-content", has_text=f"Terminal {index}").first).to_be_visible(
                timeout=10000
            )

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
        # A terminal's panel comes into being titled by its raw session name
        # and is renamed to "Terminal N" only once the auto-filed name lands,
        # and dockview seeds each dropdown row's renderer from the panel's
        # ORIGINAL init parameters -- so a row that read its title from those
        # would say ``terminal-N`` here while the strip says "Terminal N".
        expect(container.locator(".dv-default-tab-content", has_text=re.compile(r"^Terminal \d+$")).first).to_be_visible(
            timeout=5000
        )
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
        expect(page.locator(".dv-tab.dv-active-tab .dv-default-tab-content")).to_have_text(
            clicked_title, timeout=5000
        )

        # The regression: the dropdown built (and, on close, disposed) a
        # second renderer instance for that panel, and the strip tab's handle
        # must have survived it. Menu-Rename reaches the tab through the
        # handle registry, so it still opens the STRIP's editor, seeded with
        # the tab's name; Escape leaves everything as it was.
        strip_tab = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text=clicked_title)).first
        strip_tab.hover()
        strip_tab.locator(".dv-custom-tab-action").last.click()
        page.locator("[role='menuitem']", has_text="Rename").click()
        editor = page.locator(".dv-custom-tab-title-input:visible")
        expect(editor).to_have_count(1, timeout=5000)
        expect(editor).to_have_value(clicked_title)
        editor.press("Escape")
        expect(page.locator(".dv-custom-tab-title-input:visible")).to_have_count(0)
