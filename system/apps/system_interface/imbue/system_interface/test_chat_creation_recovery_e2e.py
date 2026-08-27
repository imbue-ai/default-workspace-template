"""End-to-end tests for a chat panel opened during agent creation.

``create-chat`` returns 201 as soon as the background ``mngr create`` thread
starts, but the agent is only registered with the ``AgentManager`` when that
thread finishes. Every endpoint the freshly opened panel calls resolves the
agent through that registry, so the panel's first ``/events`` fetch 404s and
latches into the "No conversation data" view.

The ``proto_agent_created`` broadcast normally covers that window with the
creation build log, but it is a transient edge event: the frontend holds the
proto-agent only between ``proto_agent_created`` and ``proto_agent_completed``,
so any delivery lag longer than the creation itself leaves no render in which
the cover is up. These tests pin the two ways that happens -- the event missing
the window entirely, and the pair arriving back-to-back -- and assert the panel
recovers on its own once the agent resolves, with no reload and no tab switch.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
import urllib.request
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server


def _playwright_browsers_installed() -> bool:
    """Check whether a launchable browser is present (Fortress or Playwright's cache)."""
    return is_e2e_browser_installed()


def _frontend_built() -> bool:
    """Check whether the frontend has been built (``static/index.html`` exists).

    Without a build the Flask server serves a "Frontend not built" placeholder, so
    every e2e test would ``page.goto()`` and then burn its per-test timeout waiting
    for selectors that can never appear. The path is resolved relative to this test
    module (``imbue/system_interface/`` holds both this file and the build output)
    so it holds regardless of the cwd.
    """
    return (Path(__file__).parent / "static" / "index.html").is_file()


pytestmark = [
    pytest.mark.release,
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontend_built(),
        reason=(
            "System interface frontend not built "
            "(run `cd system/apps/system_interface/frontend && npm run build`); skipping e2e."
        ),
    ),
]

_PRIMARY_AGENT_ID = "agent-primary-0001"
# How long the stand-in ``mngr create`` runs. Long enough that the panel is
# mounted, has 404'd, and has settled into the not-found view well before the
# agent is registered, so recovery is unambiguously driven by the resolution
# rather than by the initial load happening to win the race.
_CREATE_SECONDS = 4
_RECOVERY_TIMEOUT_MS = 20000
# One fixed port per test, matching test_e2e.py's convention.
_PORT = 18951


class _WithholdProtoCreatedBroadcaster(WebSocketBroadcaster):
    """Withholds ``proto_agent_created`` so the build-log cover never engages.

    ``release_on_completion`` chooses which delivery pathology is modelled: when
    False the event is dropped outright (the socket was down for the whole
    creation window), and when True it is flushed immediately ahead of
    ``proto_agent_completed`` (a handler thread that fell more than one creation
    window behind). Both leave the frontend without a render in which the proto
    agent is present. Every other broadcast, including ``agents_updated``, goes
    out untouched.
    """

    # Plain list rather than a field: WebSocketBroadcaster is a pydantic model
    # with ``extra="forbid"``, and pydantic rewrites underscored class
    # attributes into private-attribute descriptors.
    _withheld: list[Callable[[], None]] = []
    _release_on_completion: bool = False

    def broadcast_proto_agent_created(
        self,
        agent_id: str,
        name: str,
        creation_type: str,
        parent_agent_id: str | None,
    ) -> None:
        def send() -> None:
            WebSocketBroadcaster.broadcast_proto_agent_created(
                self,
                agent_id=agent_id,
                name=name,
                creation_type=creation_type,
                parent_agent_id=parent_agent_id,
            )

        if type(self)._release_on_completion:
            type(self)._withheld.append(send)

    def broadcast_proto_agent_completed(self, agent_id: str, success: bool, error: str | None) -> None:
        while type(self)._withheld:
            type(self)._withheld.pop(0)()
        WebSocketBroadcaster.broadcast_proto_agent_completed(self, agent_id=agent_id, success=success, error=error)


@contextlib.contextmanager
def _serving_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    release_on_completion: bool,
) -> Generator[str, None, None]:
    """Serve the real app with a stand-in ``mngr`` that takes ``_CREATE_SECONDS`` to create."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (tmp_path / "agents" / _PRIMARY_AGENT_ID).mkdir(parents=True)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_mngr = fake_bin / "mngr"
    fake_mngr.write_text(f"#!/bin/sh\nsleep {_CREATE_SECONDS}\nexit 0\n")
    fake_mngr.chmod(0o755)
    # A logged-in `claude`, so the sign-in modal's overlay does not swallow the
    # clicks that drive the "+" menu.
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}\'\n'
    )
    fake_claude.chmod(0o755)

    broadcaster = _WithholdProtoCreatedBroadcaster()
    type(broadcaster)._withheld = []
    type(broadcaster)._release_on_completion = release_on_completion

    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", _PRIMARY_AGENT_ID)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    manager = AgentManager.build(
        broadcaster,
        messenger=RecordingMngrMessenger(),
        mngr_binary=str(fake_mngr),
    )
    with manager._lock:
        manager._agents[_PRIMARY_AGENT_ID] = AgentStateItem(
            id=_PRIMARY_AGENT_ID,
            name="primary",
            state="RUNNING",
            labels={},
            work_dir=str(work_dir),
        )

    config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
    app = create_application(build_test_state(config=config, agent_manager=manager))
    server = make_threaded_server("127.0.0.1", port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # The index, not /api/agents: the manager above is the only source of agents
    # these tests use, and the discovery endpoint would load the repo's real mngr
    # config, which refuses to run under pytest.
    wait_for(
        lambda: _is_serving(base_url),
        timeout=15.0,
        error_message=f"system interface did not start on {base_url}",
    )

    try:
        yield base_url
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def _is_serving(base_url: str) -> bool:
    try:
        urllib.request.urlopen(f"{base_url}/", timeout=0.5)
    except OSError:
        return False
    return True


def _create_chat_through_ui(page: Page, base_url: str) -> None:
    """Drive the New Tab launcher's Chat tile, exactly as a user would.

    The "+" no longer drops a menu down: it opens a full-page launcher tab whose
    "Open new" row starts a chat. There is no naming dialog: the tile creates
    the agent directly under a machine-minted name and files a friendly
    "Chat N" display title on its own.
    """
    page.goto(base_url)
    page.wait_for_selector(".dockview-add-tab-button", timeout=_RECOVERY_TIMEOUT_MS)
    page.locator(".dockview-add-tab-button").first.click()
    page.wait_for_selector(".new-tab-launcher", timeout=_RECOVERY_TIMEOUT_MS)
    # Anchored: ``has_text`` is a case-insensitive substring match, so a bare
    # "chat" would also match whatever the provider picker beside the tile says.
    page.locator(".new-tab-launcher-tile:visible", has_text=re.compile(r"^New chat$")).click()


@pytest.mark.tmux
@pytest.mark.timeout(120, func_only=False)
def test_not_found_panel_recovers_when_the_agent_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page
) -> None:
    """A panel that 404s its first events fetch reloads itself once the agent registers.

    With ``proto_agent_created`` dropped, nothing covers the creation window: the
    panel 404s, shows "No conversation data", and is the state the user is stuck
    in today. It must leave that state on its own -- no reload, no tab switch --
    once ``agents_updated`` names the agent.
    """
    with _serving_workspace(tmp_path, monkeypatch, port=_PORT, release_on_completion=False) as base_url:
        _create_chat_through_ui(page, base_url)

        # The tile filed the first free "Chat N" the moment the create
        # returned, so the new tab wears the friendly display name -- the
        # machine petname the agent actually runs under never shows on the
        # strip and was never asked for.
        expect(page.locator(".dv-default-tab-content", has_text="Chat 1").first).to_be_visible(
            timeout=_RECOVERY_TIMEOUT_MS
        )

        not_found = page.locator(".message-list-not-found")
        expect(not_found).to_be_visible(timeout=_RECOVERY_TIMEOUT_MS)
        expect(not_found).to_have_count(0, timeout=_RECOVERY_TIMEOUT_MS)
        # Recovered into the transcript view -- empty, since the fresh agent has
        # no messages yet, but a real transcript rather than the error state.
        # Scoped to `:visible` because dockview keeps the inactive primary chat
        # mounted at zero size, and it carries an empty transcript too.
        expect(page.locator(".message-list-empty:visible")).to_have_count(1, timeout=_RECOVERY_TIMEOUT_MS)


@pytest.mark.tmux
@pytest.mark.timeout(120, func_only=False)
def test_not_found_panel_recovers_when_both_proto_events_arrive_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page
) -> None:
    """Recovery does not depend on the proto-agent events being observed separately.

    ``proto_agent_created`` and ``proto_agent_completed`` are delivered
    back-to-back here, which is what a client draining a backlog sees. The
    frontend adds and drops the proto agent inside a single redraw, so the build
    log never renders and the panel is left on the 404 -- the panel must still
    recover from the agent resolving.
    """
    with _serving_workspace(tmp_path, monkeypatch, port=_PORT + 1, release_on_completion=True) as base_url:
        _create_chat_through_ui(page, base_url)

        not_found = page.locator(".message-list-not-found")
        expect(not_found).to_be_visible(timeout=_RECOVERY_TIMEOUT_MS)
        expect(not_found).to_have_count(0, timeout=_RECOVERY_TIMEOUT_MS)
        expect(page.locator(".message-list-empty:visible")).to_have_count(1, timeout=_RECOVERY_TIMEOUT_MS)


@pytest.mark.tmux
@pytest.mark.timeout(120, func_only=False)
def test_not_found_panel_does_not_poll_the_screen_capture_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: Page
) -> None:
    """The not-found view captures the terminal once, not on every redraw.

    ``fetchScreenCapture`` is called from the not-found render and ends in a
    redraw, so a guard that keys on the *result* re-arms itself whenever the
    capture comes back empty -- which is exactly the case here, where the agent
    has no pane to capture. That feedback loop issued hundreds of requests per
    second, each one shelling out to tmux on a real workspace.
    """
    with _serving_workspace(tmp_path, monkeypatch, port=_PORT + 2, release_on_completion=False) as base_url:
        screen_requests: list[str] = []
        page.on(
            "request",
            lambda request: screen_requests.append(request.url) if "/screen" in request.url else None,
        )

        _create_chat_through_ui(page, base_url)
        expect(page.locator(".message-list-not-found")).to_be_visible(timeout=_RECOVERY_TIMEOUT_MS)
        expect(page.locator(".message-list-not-found")).not_to_be_visible(timeout=_RECOVERY_TIMEOUT_MS)

        # One capture attempt for the agent, however many times the view redrew.
        assert len(screen_requests) <= 2, f"screen capture was polled {len(screen_requests)} times"
