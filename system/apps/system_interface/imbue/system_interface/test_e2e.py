"""End-to-end tests for System Interface using Playwright.

These tests start a real Flask server (threaded Werkzeug) over a registry of two apps -- the
chat app at the shell's own URL, with mocked agent discovery behind it, and a stub app served
by ``app_instances``' in-memory source over loopback -- then use Playwright to drive the shell
exactly as a user would. Every open goes through the New Tab page or a rail row, every verb
through the shell's relay, and every assertion on state reads the shell's own API or files.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator
from unittest.mock import patch

import pytest
from app_instances.blueprint import build_instances_app
from app_instances.nudge import ShellNudger
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port
from app_manifest.primitives import AppName
from playwright.sync_api import Frame
from playwright.sync_api import FrameLocator
from playwright.sync_api import Page
from playwright.sync_api import expect
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
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
    module so it holds regardless of the cwd.
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

_PORT = 18765
_BASE_URL = f"http://127.0.0.1:{_PORT}"

# The fixture chat's agent id (``_make_agent_fixture``'s default) and its address.
_FIXTURE_AGENT_ID = "agent-test-123"
_FIXTURE_AGENT_NAME = "test-agent"
_FIXTURE_CHAT_ADDRESS = f"app:chat?instance={_FIXTURE_AGENT_ID}"

# The one project every server starts with unless a test asks for none: what a migrated
# workspace has, and where a fresh browser lands (the first project, before Everything).
STARTER_PROJECT_NAME = "Project 1"
STARTER_PROJECT_ID = "project-1"
EVERYTHING_VIEW_NAME = "Everything"

# The stub app the machine offers when a test asks for one: a multi-instance app whose
# instances live in memory, created by its ``new`` action and titled "Stub N".
_STUB_APP_NAME = "docs"
_STUB_APP_DISPLAY_NAME = "Docs"
_STUB_NEW_ACTION_LABEL = "New docs"

# What a stub tab reads, and what a fresh stub instance's address looks like.
_STUB_TAB_TITLE_RE = re.compile(r"^Stub \d+$")

_TRIGGER_TIMEOUT_MS = 20000


def _chat(page: Page, agent_id: str = _FIXTURE_AGENT_ID) -> FrameLocator:
    """The chat's page, framed at the chat origin under the instance's address.

    Every chat assertion goes through it: the shell document holds no chat markup, only
    the iframe the panel showing that address owns.
    """
    return page.frame_locator(f'iframe[data-address="app:chat?instance={agent_id}"]')


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


def _make_session_file(projects_dir: Path, session_id: str, events: list[dict[str, Any]]) -> Path:
    """Create a session JSONL file with the given events."""
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    session_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return session_file


def _make_agent_fixture(
    tmp_path: Path,
    agent_id: str = _FIXTURE_AGENT_ID,
    agent_name: str = _FIXTURE_AGENT_NAME,
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
    # The session endpoint resolves an agent's CLAUDE_CONFIG_DIR from this per-agent env
    # file, so pin it at the fixture's config dir; without it the watcher falls back to the
    # real ~/.claude and the fixture transcript never loads.
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


class E2EServer(FrozenModel):
    """One running workspace under test: where it is, what it holds, and where the shell keeps its state."""

    model_config = {"arbitrary_types_allowed": True}

    base_url: str = Field(description="The shell's loopback URL")
    agent_info: AgentInfo = Field(description="The fixture agent")
    session_file: Path = Field(description="The fixture agent's transcript file")
    state_dir: Path = Field(description="The shell's state directory")
    stub_source: StubInstanceSource | None = Field(description="The stub app's instances, when the machine has one")
    stub_url: str | None = Field(description="The stub app's loopback URL, when the machine has one")


def _post_json(url: str, body: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    port: int,
    session_events: list[dict[str, Any]] | None = None,
    additional_agents: tuple[tuple[str, str], ...] = (),
    is_stub_app_offered: bool = False,
    stub_instances: tuple[str, ...] = (),
    project_names: tuple[str, ...] = (STARTER_PROJECT_NAME,),
) -> Generator[E2EServer, None, None]:
    """Run the web server with a mock agent (plus any ``additional_agents``), ready for Playwright + layout ops.

    The registry the shell reads holds the chat app at the server's own URL and, when
    ``is_stub_app_offered``, a stub app over loopback whose ``stub_instances`` are seeded as
    records titled after their keys. ``project_names`` are created through the shell's API
    before the browser lands, so the client's first view is the first of them (or Everything
    when there are none). Nothing is auto-opened: the first landing is the New Tab page.

    ``additional_agents`` is a tuple of ``(agent_id, agent_name)`` for extra agents that
    exist; they carry no transcript, a bare state dir plus a manager entry is enough for
    the chat app to list them as instances.
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
    agents = [agent_info, *extra_infos]

    # A fake logged-in `claude` on PATH: the workspace UI auto-opens the sign-in modal
    # whenever the status probe reports logged-out (as it would here, with no real claude
    # binary), and the modal's overlay would then swallow every click these tests make.
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir(exist_ok=True)
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(
        '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}\'\n'
    )
    fake_claude.chmod(0o755)
    # A fake `mngr` for the paths that shell out to it: renaming a chat renames its mngr
    # agent before the chat app lists the new title, and the fixture's agents are injected
    # fakes with no real mngr behind them. Exiting 0 stands in for "mngr accepted it".
    fake_mngr = fake_bin_dir / "mngr"
    fake_mngr.write_text("#!/bin/sh\nexit 0\n")
    fake_mngr.chmod(0o755)

    registry_path = tmp_path / "registry" / "apps.toml"
    stub_source: StubInstanceSource | None = None
    stub_url: str | None = None
    rows = [
        registry_row_toml(
            "chat",
            base_url,
            is_multi_instance=True,
            is_critical=True,
            actions=(("new", "New Chat"), ("subagent", "Open subagent")),
            default_shortcut=("new", "new"),
            display_name="Chat",
        ),
    ]
    stub_port = free_port()
    if is_stub_app_offered:
        stub_source = StubInstanceSource()
        for key in stub_instances:
            stub_source.records.append(instance_record(key, title=f"Stub {key.removeprefix('stub-')}"))
        stub_url = f"http://{LOOPBACK_HOST}:{stub_port}"
        rows.append(
            registry_row_toml(
                _STUB_APP_NAME,
                stub_url,
                is_multi_instance=True,
                actions=(("new", _STUB_NEW_ACTION_LABEL),),
                default_shortcut=("new", "focus"),
                display_name=_STUB_APP_DISPLAY_NAME,
            )
        )
    write_registry(registry_path, *rows)

    with (
        patch.dict(
            os.environ,
            {
                "MNGR_HOST_DIR": str(tmp_path),
                "MNGR_AGENT_ID": "",
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "MINDS_ACCOUNTS_ROOT": str(tmp_path / "accounts"),
                "MINDS_APPS_FILE": str(registry_path),
                "MINDS_WORKSPACE_SERVER_URL": base_url,
            },
        ),
        patch("imbue.system_interface.chat_document.discover_agents", return_value=agents),
    ):
        # A signed-in provider: creating a chat opens the chooser instead when there is
        # none, which would leave a modal over the rail these tests click through.
        account_id, _ = mint_account_dir()
        commit_account(account_id, "anthropic", "Anthropic")

        # Seed the agents into a manager that is never started, so no background mngr
        # discovery runs; its messenger is a recording fake so sends succeed offline.
        broadcaster = WebSocketBroadcaster()
        manager = AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())
        with manager._lock:
            for info in agents:
                manager._agents[info.id] = AgentStateItem(
                    id=info.id, name=info.name, state="RUNNING", labels={}, work_dir=str(tmp_path / "work")
                )
        for info in agents:
            manager._ensure_activity_tracking(info.id)
        manager.note_agent_list_known()

        state_dir = tmp_path / "shell-state"
        config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
        state = build_test_state(config=config, agent_manager=manager, shell_state_directory=state_dir)
        app = create_application(state)

        server = make_threaded_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stub_server = (
            serve_in_background(
                LOOPBACK_HOST,
                stub_port,
                build_instances_app(stub_source, ShellNudger(app_name=AppName(_STUB_APP_NAME), shell_url=base_url)),
            )
            if stub_source is not None
            else contextlib.nullcontext()
        )
        with stub_server:
            try:
                wait_for(
                    lambda: _server_is_up(base_url),
                    timeout=10.0,
                    poll_interval=0.1,
                    error_message=f"workspace server did not come up at {base_url}",
                )
                for name in project_names:
                    _post_json(f"{base_url}/api/projects", {"name": name, "color": "#3B82F6", "glyph": 1})
                # Started only once the apps are serving: the first instance fetch must find
                # the chat app (this server) and the stub app answering.
                state.shell.start()
                try:
                    yield E2EServer(
                        base_url=base_url,
                        agent_info=agent_info,
                        session_file=session_file,
                        state_dir=state_dir,
                        stub_source=stub_source,
                        stub_url=stub_url,
                    )
                finally:
                    state.shell.stop()
            finally:
                server.shutdown()
                thread.join(timeout=5.0)


def _server_is_up(base_url: str) -> bool:
    try:
        urllib.request.urlopen(f"{base_url}/api/projects", timeout=0.5)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[E2EServer, None, None]:
    """Start the web server with the fixture agent and the starter project."""
    with _running_e2e_server(tmp_path, _PORT) as server:
        yield server


# ---------- helpers ----------


def _projects(base_url: str) -> dict[str, dict[str, Any]]:
    """Every project the shell holds, by id, straight off its API."""
    return {project["id"]: project for project in _get_json(f"{base_url}/api/projects")["projects"]}


def _project_tabs(base_url: str, project_id: str = STARTER_PROJECT_ID) -> list[str]:
    return list(_projects(base_url)[project_id]["tabs"])


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
    page.wait_for_function(f"localStorage.getItem('si-active-project-id') === '{view_id}'", timeout=15000)


def _launcher_row(page: Page, address: str) -> Any:
    return page.locator(f'.new-tab-launcher-row[data-address="{address}"]:visible')


def _open_from_launcher(page: Page, address: str) -> None:
    """Open an instance from the New Tab page (opening the page from the "+" when none is up)."""
    # The dock must be up first: before it mounts there is neither a launcher nor a "+".
    expect(page.locator(".dv-default-tab-content").first).to_be_visible(timeout=15000)
    if page.locator(".new-tab-launcher:visible").count() == 0:
        page.locator(".dockview-add-tab-button:visible").first.click()
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
    row = _launcher_row(page, address)
    expect(row.first).to_be_visible(timeout=15000)
    row.first.click()


def _open_fixture_chat(page: Page) -> None:
    """Open the fixture chat from the New Tab page and wait for its transcript."""
    _open_from_launcher(page, _FIXTURE_CHAT_ADDRESS)
    expect(_chat(page).locator(".message-list").first).to_be_visible(timeout=15000)


def _tab(page: Page, title: str | re.Pattern[str]) -> Any:
    return page.locator(".dv-default-tab-content", has_text=title).first


def _broadcast_layout_op(base_url: str, op: str, args: dict[str, Any], view: str = STARTER_PROJECT_NAME) -> None:
    """POST a layout op to the loopback ``/api/layout/broadcast`` endpoint.

    This is the same path ``system/scripts/layout.py`` drives, so issuing a ``split`` here
    exercises the real frontend handler. Mutating ops are view-targeted and only succeed
    once the page's ``client_state`` registration has landed, so a 412 is retried.
    """
    payload = json.dumps({"op": op, "args": {**args, "view": view}, "agent_id": _FIXTURE_AGENT_ID}).encode()
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
            return False

    wait_for(
        _attempt,
        timeout=15.0,
        poll_interval=0.2,
        error_message=f"layout broadcast for op {op!r} never succeeded (client registration missing?)",
    )


def _stub_address(key: str) -> str:
    return f"app:{_STUB_APP_NAME}?instance={key}"


def _open_rail_switcher(page: Page) -> None:
    page.locator(".project-rail-header").click()
    expect(page.locator(".project-rail-menu")).to_be_visible(timeout=5000)


def _switch_view_via_rail(page: Page, view_name: str) -> None:
    _open_rail_switcher(page)
    page.locator(".project-rail-menu [role='menuitem']", has_text=view_name).first.click()


def _collapse_rail(page: Page) -> None:
    """Fold the hover-expanded rail back up, so the dock underneath is clickable."""
    page.mouse.move(600, 400)
    expect(page.locator(".project-rail-search")).to_have_count(0, timeout=15000)


# A page for a stub instance's frame, served by a Playwright route rather than by the stub
# (which serves only its instances API). Its state is an ``<input>``: typing into it is a
# change no reload survives, because the served markup has it empty.
_FRAMED_PAGE_HTML = "<!doctype html><html><body><input id='held' value='' /></body></html>"


def _serve_stub_pages(page: Page, server: E2EServer) -> None:
    assert server.stub_url is not None
    page.route(
        f"{server.stub_url}/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_FRAMED_PAGE_HTML),
    )


# Count every ``.si-live-surface`` that leaves the document from here on: removing an iframe
# destroys its document, so the mechanism is watched directly.
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

# The surfaces holding an address's frame, as a plain-object report. Identity is carried by
# ``__e2eStamp``, a property set on the ELEMENT rather than an attribute: nothing serializes
# it, so a surface that answers to it is necessarily the very element that was stamped.
_SURFACE_REPORT_JS = """
([address, stamp]) => {
  const surfaces = Array.from(document.querySelectorAll('.si-live-surface'))
    .filter((surface) => surface.querySelector(`iframe[data-address="${address}"]`) !== null);
  if (stamp) {
    for (const surface of surfaces) surface.__e2eStamp = stamp;
  }
  const shown = surfaces.filter((surface) => {
    const box = surface.getBoundingClientRect();
    return getComputedStyle(surface).display !== 'none' && box.width > 0 && box.height > 0;
  });
  return {
    count: surfaces.length,
    shownCount: shown.length,
    stamps: surfaces.map((surface) => surface.__e2eStamp ?? null),
    removals: window.__e2eRemovedSurfaces.length,
  };
}
"""


def _surface_report(page: Page, address: str, stamp: str | None = None) -> dict[str, Any]:
    return page.evaluate(_SURFACE_REPORT_JS, [address, stamp])


# ---------- the shell ----------


@pytest.mark.timeout(30, func_only=False)
def test_page_loads_and_shows_title(e2e_server: E2EServer, page: Page) -> None:
    page.goto(e2e_server.base_url)
    expect(page).to_have_title("System Interface")


@pytest.mark.timeout(60, func_only=False)
def test_first_landing_is_the_new_tab_page_offering_the_machine(e2e_server: E2EServer, page: Page) -> None:
    """A fresh browser lands on the starter project's New Tab page; nothing is opened for it.

    The dock is never empty: a view with nothing to mount shows the launcher as its one
    "New tab" tab. The machine's chat is offered in the "On this machine" table (the
    project's own tab set is empty), and no chat frame exists until someone opens one.
    """
    page.goto(e2e_server.base_url)
    _wait_for_view(page, STARTER_PROJECT_ID)
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
    expect(page.locator(".dv-default-tab-content")).to_have_count(1)
    expect(page.locator(".dv-default-tab-content").first).to_have_text("New tab")
    row = page.locator(".new-tab-launcher-section[data-section='on-machine']").locator(
        f'.new-tab-launcher-row[data-address="{_FIXTURE_CHAT_ADDRESS}"]'
    )
    expect(row).to_have_count(1, timeout=15000)
    expect(row).to_contain_text(_FIXTURE_AGENT_NAME)
    expect(page.locator("iframe[data-address]")).to_have_count(0)


@pytest.mark.timeout(60, func_only=False)
def test_opening_a_row_files_it_into_the_project_and_shows_its_page(e2e_server: E2EServer, page: Page) -> None:
    """Opening an instance from the launcher docks its page, titles the tab, and files the address into the project.

    Every open in a project goes through the same rule: the address joins the project's
    tab set (read back from the shell's API), the panel shows the app's page for that
    instance, and the tab wears the title the app reports.
    """
    page.goto(e2e_server.base_url)
    _wait_for_view(page, STARTER_PROJECT_ID)
    _open_fixture_chat(page)

    expect(_tab(page, _FIXTURE_AGENT_NAME)).to_be_visible(timeout=15000)
    expect(page.locator(f'iframe[data-address="{_FIXTURE_CHAT_ADDRESS}"]')).to_have_count(1)
    expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
    wait_for(
        lambda: _FIXTURE_CHAT_ADDRESS in _project_tabs(e2e_server.base_url),
        timeout=15.0,
        poll_interval=0.1,
        error_message="opening the chat never filed it into the starter project",
    )
    # And the launcher that was in the pane made way for it.
    expect(page.locator(".new-tab-launcher")).to_have_count(0)


@pytest.mark.timeout(60, func_only=False)
def test_no_projects_lands_on_everything(tmp_path: Path, page: Page) -> None:
    """With no project on the machine, the client lands on Everything, whose table is the whole machine."""
    with _running_e2e_server(tmp_path, _PORT + 2, project_names=()) as server:
        page.goto(server.base_url)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        expect(page.locator(".new-tab-launcher-section[data-section='in-project']")).to_have_count(0)
        expect(_launcher_row(page, _FIXTURE_CHAT_ADDRESS)).to_have_count(1, timeout=15000)


# ---------- the chat page ----------


@pytest.mark.timeout(60, func_only=False)
def test_chat_transcript_area_is_pure_white(e2e_server: E2EServer, page: Page) -> None:
    """The chat conversation panel renders on a pure-white background, scoped to the chat token."""
    page.goto(e2e_server.base_url)
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
def test_conversation_and_composer_render(e2e_server: E2EServer, page: Page) -> None:
    """The opened chat shows both sides of its conversation and a composer whose send button follows the text."""
    page.goto(e2e_server.base_url)
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
def test_composer_bar_survives_a_shorter_window(e2e_server: E2EServer, page: Page) -> None:
    """A window that gets shorter keeps the whole composer on screen.

    Everything below the dock is positioned in pixels -- the panes, and the live surfaces
    mirroring them -- so a row that grows with the viewport but cannot shrink back leaves
    the chat laid out at the old height with the model bar below the bottom edge.
    """
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(e2e_server.base_url)
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
    with _running_e2e_server(tmp_path, _PORT + 1, session_events=_TOOL_CALL_SESSION_EVENTS) as server:
        page.goto(server.base_url)
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
def test_live_stream_delivers_new_events(e2e_server: E2EServer, page: Page) -> None:
    """New events written to the session file appear in the UI as they stream in."""
    page.goto(e2e_server.base_url)
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
    with _running_e2e_server(tmp_path, _PORT + 5, session_events=_QUEUED_SESSION_EVENTS) as server:
        page.goto(server.base_url)
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
    with _running_e2e_server(tmp_path, _PORT + 6) as server:
        events_url = "**/api/agents/*/events"
        page.route(
            events_url,
            lambda route: route.fulfill(status=503, content_type="text/plain", body="Backend not yet available"),
        )
        page.goto(server.base_url)
        _open_from_launcher(page, _FIXTURE_CHAT_ADDRESS)

        error = _chat(page).locator(".message-list-error")
        expect(error).to_be_visible(timeout=15000)
        expect(error.locator("p")).to_have_text("Error: request failed (HTTP 503)")

        page.unroute(events_url)
        error.locator(".message-list-reload").click()

        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-error")).to_have_count(0)


# ---------- layout ops ----------


@pytest.mark.timeout(120, func_only=False)
def test_new_tab_opens_in_clicked_split(tmp_path: Path, page: Page) -> None:
    """The header "+" opens the new tab in the split whose header was clicked.

    Split the layout into two groups, make the LEFT group active (so dockview's default
    "add to the active group" would land a new tab on the left), then click the RIGHT
    split's "+" and create an instance from the launcher. It must land in the RIGHT split.
    """
    with _running_e2e_server(tmp_path, _PORT + 3, is_stub_app_offered=True, stub_instances=("stub-1",)) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        add_buttons = page.locator(".dockview-add-tab-button")
        expect(add_buttons).to_have_count(1)

        _broadcast_layout_op(
            server.base_url,
            "split",
            {
                "address": _stub_address("stub-1"),
                "relative_to": _FIXTURE_CHAT_ADDRESS,
                "direction": "right",
                "new_group": True,
            },
        )
        expect(add_buttons).to_have_count(2, timeout=10000)
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=10000)

        _tab(page, _FIXTURE_AGENT_NAME).click()
        left_group = page.locator(
            ".dv-groupview", has=page.locator(".dv-default-tab-content", has_text=_FIXTURE_AGENT_NAME)
        )
        expect(left_group).to_have_class(re.compile(r"\bdv-active-group\b"))

        boxes = [add_buttons.nth(i).bounding_box() for i in range(2)]
        assert boxes[0] is not None and boxes[1] is not None
        right_index = 0 if boxes[0]["x"] > boxes[1]["x"] else 1
        add_buttons.nth(right_index).click()

        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        page.locator(f'.new-tab-launcher-tile:visible[data-launch="{_STUB_APP_NAME}:new"]').click()

        expect(_tab(page, "Stub 2")).to_be_visible(timeout=15000)
        placement = page.evaluate(
            """
            (title) => {
              const groups = Array.from(document.querySelectorAll('.dv-groupview'))
                .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
              const has = (g) => Array.from(g.querySelectorAll('.dv-default-tab-content'))
                .some((e) => (e.textContent || '').includes(title));
              return {
                count: groups.length,
                inLeft: groups.length > 0 ? has(groups[0]) : false,
                inRight: groups.length > 0 ? has(groups[groups.length - 1]) : false,
              };
            }
            """,
            "Stub 2",
        )
        assert placement["count"] == 2, f"new tab should join the right split, not create a third group: {placement}"
        assert placement["inRight"], f"new tab should be in the right split: {placement}"
        assert not placement["inLeft"], f"new tab leaked into the left split: {placement}"
        # The create went through the relay to the app, which minted the instance.
        assert server.stub_source is not None
        assert [str(record.key) for record in server.stub_source.records] == ["stub-1", "stub-2"]


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
        tmp_path, _PORT + 4, session_events=events, is_stub_app_offered=True, stub_instances=("stub-1",)
    ) as server:
        _serve_stub_pages(page, server)
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        _chat_frame(page).wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.scrollHeight > el.clientHeight * 2; }",
            timeout=15000,
        )

        # A sibling tab in the SAME group, so hiding the chat is a pure tab switch.
        _broadcast_layout_op(server.base_url, "open", {"address": probe, "new_group": True})
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)
        _broadcast_layout_op(
            server.base_url,
            "move",
            {"address": probe, "relative_to": _FIXTURE_CHAT_ADDRESS, "direction": "within"},
        )
        page.wait_for_function(
            "() => document.querySelectorAll('.dv-groupview').length === 1", timeout=_TRIGGER_TIMEOUT_MS
        )
        _broadcast_layout_op(server.base_url, "focus", {"address": _FIXTURE_CHAT_ADDRESS})
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

        _broadcast_layout_op(server.base_url, "focus", {"address": probe})
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

        _broadcast_layout_op(server.base_url, "focus", {"address": _FIXTURE_CHAT_ADDRESS})
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


@pytest.mark.timeout(120, func_only=False)
def test_load_op_switches_the_clients_view(tmp_path: Path, page: Page) -> None:
    """``layout.py load <view>`` switches what the connected client is showing."""
    with _running_e2e_server(tmp_path, _PORT + 7) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)

        payload = json.dumps({"op": "load", "args": {"view": EVERYTHING_VIEW_NAME}, "agent_id": "agent-e2e"}).encode()
        request = urllib.request.Request(
            f"{server.base_url}/api/layout/broadcast",
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
            _attempt, timeout=15.0, poll_interval=0.2, error_message="the load op never got past client registration"
        )
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)


# ---------- projects and views ----------


@pytest.mark.timeout(120, func_only=False)
def test_project_dialogs_end_to_end(tmp_path: Path, page: Page) -> None:
    """The rail's switcher and settings modal drive the shell's project store.

    "New project" mints the next "Project N" and switches onto it, and deleting the active
    project from the header's settings modal falls back to the surviving one -- all read
    back from the shell's API.
    """
    with _running_e2e_server(tmp_path, _PORT + 8) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_CHAT_ADDRESS)

        _open_rail_switcher(page)
        page.locator(".project-rail-menu [role='menuitem']", has_text="New project").click()
        _wait_for_view(page, "project-2")
        wait_for(
            lambda: "project-2" in _projects(server.base_url),
            timeout=10.0,
            poll_interval=0.1,
            error_message="create never registered project-2",
        )
        assert _projects(server.base_url)["project-2"]["name"] == "Project 2"
        # A new project starts empty, so the launcher is what it shows.
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)

        page.locator(".project-rail-header").click(button="right")
        page.locator(".project-rail-menu [role='menuitem']", has_text="Project settings").click()
        page.locator(".destroy-dialog-btn-cancel", has_text="Delete").click()
        page.locator(".destroy-dialog-btn-destroy", has_text="Delete project").click()
        _wait_for_view(page, STARTER_PROJECT_ID)
        assert "project-2" not in _projects(server.base_url)
        # Back on the starter project, the chat it holds is restored from its saved layout.
        expect(_tab(page, _FIXTURE_AGENT_NAME)).to_be_visible(timeout=15000)


@pytest.mark.timeout(120, func_only=False)
def test_switching_views_preserves_chat_transcript(tmp_path: Path, page: Page) -> None:
    """A chat pane restored by a view switch still shows its own transcript.

    Everything lists the machine's agent whatever project shows it, so opening it there
    leaves it open in the starter project too. Switching back restores the starter
    project's layout, whose panel must bind to the same instance.
    """
    with _running_e2e_server(tmp_path, _PORT + 9) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_CHAT_ADDRESS)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        _open_from_launcher(page, _FIXTURE_CHAT_ADDRESS)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, EVERYTHING_VIEW_ID, containing=_FIXTURE_CHAT_ADDRESS)

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-list-empty")).to_have_count(0)
        expect(_chat(page).locator(".message-list-not-found")).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_live_page_survives_a_view_that_does_not_include_it(tmp_path: Path, page: Page) -> None:
    """A page keeps running, and keeps its state, while no view is showing it.

    There is one live page per instance, machine-wide, and a project is only a view that
    may or may not include it. Type into an app, switch to a view that does not have it,
    switch back: the same document is still there, still holding what was typed. Asserted
    on the framed document's own state, the surface element's identity via a
    non-serializable property, and a MutationObserver count of surfaces removed.
    """
    address = _stub_address("stub-1")
    frame_selector = f'iframe[data-address="{address}"]'
    with _running_e2e_server(tmp_path, _PORT + 10, is_stub_app_offered=True, stub_instances=("stub-1",)) as server:
        _serve_stub_pages(page, server)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)

        _open_from_launcher(page, address)
        expect(page.locator(frame_selector)).to_have_count(1, timeout=_TRIGGER_TIMEOUT_MS)
        held_field = page.frame_locator(frame_selector).locator("#held")
        expect(held_field).to_have_value("", timeout=15000)
        held_field.fill("typed-by-the-user")
        expect(held_field).to_have_value("typed-by-the-user")
        _surface_report(page, address, "the-original-element")
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=address)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        page.wait_for_function(
            f"""
            () => {{
              const iframe = document.querySelector({json.dumps(frame_selector)});
              return iframe !== null && getComputedStyle(iframe.closest('.si-live-surface')).display === 'none';
            }}
            """,
            timeout=15000,
        )
        while_away = _surface_report(page, address)
        assert while_away["count"] == 1, (
            f"the page was taken out of the DOM by a view that does not include it: {while_away}"
        )
        assert while_away["stamps"] == ["the-original-element"], f"the element was rebuilt while hidden: {while_away}"
        assert while_away["removals"] == 0, f"a live surface left the DOM on the way out: {while_away}"

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        page.wait_for_function(
            f"""
            () => {{
              const iframe = document.querySelector({json.dumps(frame_selector)});
              if (iframe === null) return false;
              const surface = iframe.closest('.si-live-surface');
              const box = surface.getBoundingClientRect();
              return getComputedStyle(surface).display !== 'none' && box.width > 0 && box.height > 0;
            }}
            """,
            timeout=15000,
        )
        on_return = _surface_report(page, address)
        assert on_return["count"] == 1, f"the page forked into a second copy: {on_return}"
        assert on_return["stamps"] == ["the-original-element"], (
            f"the element was re-created on the way back: {on_return}"
        )
        assert on_return["removals"] == 0, f"a live surface left the DOM during the round trip: {on_return}"
        expect(held_field).to_have_value("typed-by-the-user")


@pytest.mark.timeout(120, func_only=False)
def test_one_instance_is_one_element_in_every_view_showing_it(tmp_path: Path, page: Page) -> None:
    """An instance shown by two views is ONE element, shown twice -- never two."""
    with _running_e2e_server(tmp_path, _PORT + 11) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)
        in_project = _surface_report(page, _FIXTURE_CHAT_ADDRESS, "the-original-element")
        assert in_project["count"] == 1, f"the starter project should hold exactly one chat page: {in_project}"
        assert in_project["shownCount"] == 1, f"the starter project's chat page should be on screen: {in_project}"
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_CHAT_ADDRESS)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        _open_from_launcher(page, _FIXTURE_CHAT_ADDRESS)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)

        in_everything = _surface_report(page, _FIXTURE_CHAT_ADDRESS)
        assert in_everything["count"] == 1, f"opening the chat in Everything forked its page: {in_everything}"
        assert in_everything["shownCount"] == 1, f"Everything is not showing the chat page: {in_everything}"
        assert in_everything["stamps"] == ["the-original-element"], (
            f"Everything is showing a different element than the starter project: {in_everything}"
        )
        assert in_everything["removals"] == 0, f"a live surface left the DOM on the way in: {in_everything}"

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        back_in_project = _surface_report(page, _FIXTURE_CHAT_ADDRESS)
        assert back_in_project["count"] == 1, f"switching back forked the chat page: {back_in_project}"
        assert back_in_project["stamps"] == ["the-original-element"], (
            f"switching back re-created the chat's element: {back_in_project}"
        )
        assert back_in_project["removals"] == 0, (
            f"a live surface left the DOM during the round trip: {back_in_project}"
        )


# ---------- verbs ----------


@pytest.mark.timeout(120, func_only=False)
def test_double_click_renames_a_chat_and_the_name_survives_a_reload(tmp_path: Path, page: Page) -> None:
    """Double-clicking a chat tab's title renames the instance through its app, and the name is kept.

    The rename goes through the shell's relay to the chat app, which renames the mngr agent
    (the fixture's stub accepts it) and lists the new title; the tab re-derives its title
    from the inventory, so a reload proves the name stuck to the instance, not the tab.
    """
    with _running_e2e_server(tmp_path, _PORT + 12) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        tab_title = _tab(page, _FIXTURE_AGENT_NAME)
        expect(tab_title).to_be_visible(timeout=15000)

        tab_title.dblclick()
        editor = page.locator(".dv-custom-tab-title-input:visible")
        expect(editor).to_be_visible(timeout=5000)
        expect(editor).to_have_value(_FIXTURE_AGENT_NAME)
        editor.fill("Design notes")
        editor.press("Enter")

        expect(_tab(page, "Design notes")).to_be_visible(timeout=10000)
        expect(page.locator(".dv-custom-tab-title-input:visible")).to_have_count(0)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_CHAT_ADDRESS)

        page.reload()
        expect(_tab(page, "Design notes")).to_be_visible(timeout=15000)
        expect(_chat(page).locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_AGENT_NAME)).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_deleting_an_instance_removes_it_from_the_app_and_every_view(tmp_path: Path, page: Page) -> None:
    """Delete from a tab's menu deletes the instance in its app; the shell drops it from every view.

    The instance is opened in the starter project and in Everything, then deleted from the
    project's tab. The app's records lose it, the tab leaves the mounted view, the address
    leaves the project's tab set, and mounting Everything afterwards restores no tab for it.
    """
    address = _stub_address("stub-1")
    with _running_e2e_server(tmp_path, _PORT + 13, is_stub_app_offered=True, stub_instances=("stub-1",)) as server:
        assert server.stub_source is not None
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_from_launcher(page, address)
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=address)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        _open_from_launcher(page, address)
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=15000)
        _wait_for_layout_saved(server.state_dir, EVERYTHING_VIEW_ID, containing=address)

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _collapse_rail(page)
        stub_tab = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text="Stub 1")).first
        expect(stub_tab).to_be_visible(timeout=15000)
        stub_tab.hover()
        stub_tab.locator('.dv-custom-tab-action[aria-label="Tab options"]').click()
        page.locator("[role='menuitem']", has_text="Delete Stub 1").click()
        page.locator(".destroy-dialog-btn-destroy").click()

        expect(page.locator(".dv-default-tab-content", has_text="Stub 1")).to_have_count(0, timeout=10000)
        wait_for(
            lambda: server.stub_source is not None and server.stub_source.records == [],
            timeout=10.0,
            poll_interval=0.1,
            error_message="the delete never reached the app",
        )
        wait_for(
            lambda: address not in _project_tabs(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the deleted instance stayed in the project's tab set",
        )
        wait_for(
            lambda: not any(
                address in path.read_text() for path in _client_layout_files(server.state_dir, EVERYTHING_VIEW_ID)
            ),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the deleted instance stayed in Everything's saved layout",
        )

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        expect(page.locator(".dv-default-tab-content", has_text="Stub 1")).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_removing_a_row_from_the_project_unfiles_it_without_destroying_it(tmp_path: Path, page: Page) -> None:
    """The rail row menu's "Remove from project" unfiles an address rather than deleting the instance."""
    with _running_e2e_server(tmp_path, _PORT + 14) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        wait_for(
            lambda: _FIXTURE_CHAT_ADDRESS in _project_tabs(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the fixture chat was never filed into the starter project",
        )

        page.locator(".machine-sidebar").hover()
        chat_row = page.locator(f'.project-rail-tab[data-address="{_FIXTURE_CHAT_ADDRESS}"]')
        expect(chat_row).to_have_count(1)
        chat_row.click(button="right")
        page.locator(".project-rail-menu [role='menuitem']", has_text="Remove from project").click()

        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_AGENT_NAME)).to_have_count(0, timeout=10000)
        wait_for(
            lambda: _FIXTURE_CHAT_ADDRESS not in _project_tabs(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="Remove from project never took the address out of the tab set",
        )

        # It kept running: Everything's machine-wide table still offers it.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        expect(_launcher_row(page, _FIXTURE_CHAT_ADDRESS)).to_have_count(1, timeout=15000)


def _open_all_apps(page: Page) -> None:
    page.locator(".machine-sidebar").hover()
    page.locator(".project-rail-all-apps").click()
    expect(page.locator(".project-rail-app").first).to_be_visible(timeout=5000)


def _project_shortcuts(base_url: str, project_id: str = STARTER_PROJECT_ID) -> set[tuple[str, str, str]]:
    return {(s["app"], s["action"], s["mode"]) for s in _projects(base_url)[project_id]["shortcuts"]}


@pytest.mark.timeout(120, func_only=False)
def test_pinning_an_app_adds_a_rail_shortcut_and_unpinning_removes_it(tmp_path: Path, page: Page) -> None:
    """Pinning from "All apps" adds the app's primary action to the project's rail; unpinning takes it off.

    A shortcut is the project's, stored in the shell: pinning puts it in the project's
    shortcut list, grows a rail row, and drops the app from the popover (which lists only
    what the view has NOT pinned); the rail row's own pin icon undoes all three.
    """
    with _running_e2e_server(tmp_path, _PORT + 15, is_stub_app_offered=True) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        # The project was created before the shell read the registry, so its rail was
        # seeded with nothing: the stub is there to pin.
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        assert (_STUB_APP_NAME, "new", "focus") not in _project_shortcuts(server.base_url)

        _open_all_apps(page)
        app_row = page.locator(f'.project-rail-app[data-app="{_STUB_APP_NAME}"]')
        expect(app_row).to_have_count(1, timeout=15000)
        expect(page.locator(".project-rail-shortcut", has_text=_STUB_APP_DISPLAY_NAME)).to_have_count(0)

        page.locator(f'button[aria-label="Pin {_STUB_APP_DISPLAY_NAME}"]').click()
        expect(app_row).to_have_count(0, timeout=15000)
        expect(page.locator(".project-rail-shortcut", has_text=_STUB_APP_DISPLAY_NAME)).to_have_count(1)
        wait_for(
            lambda: (_STUB_APP_NAME, "new", "focus") in _project_shortcuts(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="pinning never stored the shortcut on the project",
        )

        page.keyboard.press("Escape")
        expect(page.locator(".project-rail-app")).to_have_count(0, timeout=5000)
        page.locator(".machine-sidebar").hover()
        page.locator(f'button[aria-label="Unpin {_STUB_APP_DISPLAY_NAME} from this project"]').click()
        expect(page.locator(".project-rail-shortcut", has_text=_STUB_APP_DISPLAY_NAME)).to_have_count(0, timeout=15000)
        _open_all_apps(page)
        expect(page.locator(f'.project-rail-app[data-app="{_STUB_APP_NAME}"]')).to_have_count(1, timeout=15000)
        wait_for(
            lambda: (_STUB_APP_NAME, "new", "focus") not in _project_shortcuts(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="unpinning never removed the shortcut from the project",
        )


@pytest.mark.timeout(120, func_only=False)
def test_rail_shortcut_creates_an_instance_and_the_rail_holds_a_fixed_layout(tmp_path: Path, page: Page) -> None:
    """A rail shortcut in focus mode with nothing to focus creates an instance; expanding the rail never reflows it.

    The rail expands over the dock by growing width alone, so a row shared by both states
    sits at the same y whether collapsed or expanded; and picking a row inside the
    still-hovered rail leaves it open, since only the pointer leaving closes it.
    """
    with _running_e2e_server(tmp_path, _PORT + 16, is_stub_app_offered=True, project_names=()) as server:
        assert server.stub_source is not None
        page.goto(server.base_url)
        _wait_for_view(page, EVERYTHING_VIEW_ID)

        rail = page.locator(".machine-sidebar")
        header = page.locator(".project-rail-header")
        stub_shortcut = page.locator(f'.project-rail-shortcut[data-shortcut="{_STUB_APP_NAME}:new"]')
        expect(stub_shortcut).to_have_count(1, timeout=15000)

        page.mouse.move(600, 400)
        expect(page.locator(".project-rail-search")).to_have_count(0, timeout=5000)
        header_collapsed = header.bounding_box()
        shortcut_collapsed = stub_shortcut.bounding_box()
        assert header_collapsed is not None and shortcut_collapsed is not None

        rail.hover()
        expect(page.locator(".project-rail-search")).to_be_visible(timeout=5000)
        header_expanded = header.bounding_box()
        shortcut_expanded = stub_shortcut.bounding_box()
        assert header_expanded is not None and shortcut_expanded is not None
        assert header_expanded["width"] > header_collapsed["width"], "hovering never actually expanded the rail"
        assert header_collapsed["y"] == header_expanded["y"], "the header row shifted vertically on expansion"
        assert shortcut_collapsed["y"] == shortcut_expanded["y"], "a shortcut row shifted vertically on expansion"
        assert shortcut_collapsed["height"] == shortcut_expanded["height"], "a shortcut row's height changed"

        stub_shortcut.click()
        expect(_tab(page, _STUB_TAB_TITLE_RE)).to_be_visible(timeout=15000)
        expect(page.locator(".project-rail-search")).to_be_visible(timeout=1000)
        assert [str(record.key) for record in server.stub_source.records] == ["stub-1"]

        page.mouse.move(600, 400)
        expect(page.locator(".project-rail-search")).to_have_count(0, timeout=5000)


# ---------- the launcher ----------


@pytest.mark.timeout(120, func_only=False)
def test_launcher_app_filter_hides_an_app_and_reset_restores_it(tmp_path: Path, page: Page) -> None:
    """Unchecking an app in a table's filter hides its rows; Reset re-checks all."""
    with _running_e2e_server(
        tmp_path,
        _PORT + 17,
        additional_agents=(("agent-filter-999", "filter-agent"),),
        is_stub_app_offered=True,
        stub_instances=("stub-1",),
    ) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)

        section = page.locator(".new-tab-launcher-section[data-section='on-machine']")
        chat_row = section.locator('.new-tab-launcher-row[data-address="app:chat?instance=agent-filter-999"]')
        stub_row = section.locator(f'.new-tab-launcher-row[data-address="{_stub_address("stub-1")}"]')
        expect(chat_row).to_have_count(1, timeout=15000)
        expect(stub_row).to_have_count(1, timeout=15000)

        section.locator("button[aria-expanded]").click()
        chat_checkbox = section.locator("label", has_text="Chat")
        expect(chat_checkbox).to_be_visible(timeout=5000)
        chat_checkbox.click()
        expect(chat_row).to_have_count(0)
        expect(stub_row).to_have_count(1)

        section.locator("button", has_text="Reset filters").click()
        expect(chat_row).to_have_count(1)
        expect(stub_row).to_have_count(1)


# ---------- the tab strip ----------


@pytest.mark.timeout(180, func_only=False)
def test_overflowed_tabs_list_as_plain_rows_and_the_strip_keeps_its_handles(tmp_path: Path, page: Page) -> None:
    """Tabs folded into the "N more" dropdown list as bare rows; the strip stays whole.

    While the dropdown is open, two live renderer instances exist for one panel -- the
    strip's and the row's -- and only the strip's may own the panel's handle and controls.
    """
    keys = tuple(f"stub-{n}" for n in range(1, 9))
    with _running_e2e_server(tmp_path, _PORT + 18, is_stub_app_offered=True, stub_instances=keys) as server:
        page.set_viewport_size({"width": 900, "height": 700})
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)

        for key in keys:
            _broadcast_layout_op(server.base_url, "open", {"address": _stub_address(key), "new_group": False})
        expect(_tab(page, "Stub 8")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)

        overflow_control = page.locator(".dv-tabs-overflow-dropdown-default")
        wait_for(
            lambda: overflow_control.is_visible(),
            timeout=5.0,
            poll_interval=0.1,
            error_message="the strip never overflowed: a chat plus 8 tabs all fit at 900px wide",
        )

        overflow_control.click()
        container = page.locator(".dv-tabs-overflow-container")
        expect(container).to_be_visible(timeout=5000)
        rows = container.locator(".dv-default-tab-content")
        expect(rows.first).to_be_visible(timeout=5000)
        expect(container.locator(".dv-default-tab-content", has_text=_STUB_TAB_TITLE_RE).first).to_be_visible(
            timeout=5000
        )
        expect(container.locator(".dv-custom-tab-actions")).to_have_count(0)
        expect(container.locator(".dv-custom-tab-action")).to_have_count(0)
        rows.first.hover()
        expect(container.locator(".dv-custom-tab-actions")).to_have_count(0)

        clicked_title = rows.first.inner_text()
        rows.first.click()
        expect(page.locator(".dv-tabs-overflow-container")).to_have_count(0, timeout=5000)
        expect(page.locator(".dv-tab.dv-active-tab .dv-default-tab-content", has_text=clicked_title)).to_have_count(
            1, timeout=5000
        )

        strip_tab = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text=clicked_title)).first
        strip_tab.hover()
        expect(strip_tab.locator(".dv-custom-tab-action")).to_have_count(2, timeout=5000)
        strip_tab.locator('.dv-custom-tab-action[aria-label="Tab options"]').click()
        expect(page.locator("[role='menuitem']", has_text="Close tab")).to_be_visible(timeout=5000)
        page.keyboard.press("Escape")


def _drop_overlay_styles(page: Page) -> dict[str, Any] | None:
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


@pytest.mark.timeout(180, func_only=False)
def test_dropping_on_a_tab_draws_a_line_and_on_a_pane_draws_a_wash(tmp_path: Path, page: Page) -> None:
    """A drop onto a tab is a seam (a thin insertion line); a drop onto a pane is a region (a wash)."""
    with _running_e2e_server(tmp_path, _PORT + 19, is_stub_app_offered=True, stub_instances=("stub-1",)) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_chat(page)
        _broadcast_layout_op(server.base_url, "open", {"address": _stub_address("stub-1"), "new_group": False})
        expect(_tab(page, "Stub 1")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)

        dragged = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text="Stub 1")).first
        target_tab = page.locator(
            ".dv-tab", has=page.locator(".dv-default-tab-content", has_text=_FIXTURE_AGENT_NAME)
        ).first
        source_box = dragged.bounding_box()
        assert source_box is not None, "the dragged tab has no box"
        page.mouse.move(source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
        page.mouse.down()

        target_box = target_tab.bounding_box()
        assert target_box is not None, "the target tab has no box"
        page.mouse.move(
            target_box["x"] + target_box["width"] * 0.2, target_box["y"] + target_box["height"] / 2, steps=25
        )
        page.wait_for_timeout(400)
        target_box = target_tab.bounding_box()
        assert target_box is not None, "the target tab lost its box mid-drag"
        tab_overlay = _drop_overlay_styles(page)
        assert tab_overlay is not None, "no drop overlay appeared over the tab"
        assert tab_overlay["background"] == "rgba(0, 0, 0, 0)", (
            f"a tab drop should not wash the tab, got {tab_overlay}"
        )
        assert tab_overlay["afterContent"] not in ("none", ""), "the tab drop drew no insertion line"
        assert tab_overlay["afterWidth"] == "2px", f"the insertion line should be 2px, got {tab_overlay['afterWidth']}"
        assert tab_overlay["afterBackground"] != "rgba(0, 0, 0, 0)", "the insertion line is invisible"
        assert tab_overlay["side"] in ("left", "right"), (
            f"a drop onto a tab should pick a side, got {tab_overlay['side']}"
        )
        line_x = tab_overlay["left"] if tab_overlay["side"] == "left" else tab_overlay["right"]
        seam_x = target_box["x"] if tab_overlay["side"] == "left" else target_box["x"] + target_box["width"]
        assert abs(line_x - seam_x) <= 1, (
            f"the {tab_overlay['side']} line should sit on that edge ({seam_x}), got {line_x}"
        )

        pane_box = page.locator(".dv-content-container").first.bounding_box()
        assert pane_box is not None, "the pane has no box"
        page.mouse.move(pane_box["x"] + pane_box["width"] * 0.15, pane_box["y"] + pane_box["height"] / 2, steps=25)
        pane_overlay = _drop_overlay_styles(page)
        assert pane_overlay is not None, "no drop overlay appeared over the pane"
        assert pane_overlay["background"] != "rgba(0, 0, 0, 0)", "a pane drop should still show its region"
        assert pane_overlay["afterContent"] in ("none", ""), "a pane drop should not draw an insertion line"
        page.mouse.up()


# ---------- devices ----------

# A phone-shaped browser context, inlined so the emulated UA is pinned rather than drifting
# with the Playwright version; the client classifies itself as mobile off the UA string.
_MOBILE_CONTEXT_ARGS: dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "viewport": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.mark.timeout(120, func_only=False)
def test_mobile_client_saves_its_own_arrangement(tmp_path: Path, page: Page) -> None:
    """A mobile client's autosave rewrites the view's mobile seed, not desktop's."""
    with _running_e2e_server(tmp_path, _PORT + 20) as server:
        e2e_browser = page.context.browser
        assert e2e_browser is not None
        context = e2e_browser.new_context(**_MOBILE_CONTEXT_ARGS)
        try:
            mobile_page = context.new_page()
            mobile_page.goto(server.base_url)
            _open_from_launcher(mobile_page, _FIXTURE_CHAT_ADDRESS)
            expect(mobile_page.locator(".dv-default-tab-content", has_text=_FIXTURE_AGENT_NAME).first).to_be_visible(
                timeout=15000
            )
            seeds_dir = server.state_dir / "layouts" / STARTER_PROJECT_ID
            wait_for(
                lambda: (seeds_dir / "seed.mobile.json").exists(),
                timeout=15.0,
                poll_interval=0.1,
                error_message="autosave never wrote the mobile seed",
            )
            assert not (seeds_dir / "seed.desktop.json").exists()
        finally:
            context.close()
