"""End-to-end tests for the workspace shell using Playwright.

These tests start a real Flask server (threaded Werkzeug) over a registry of stub apps served by
``app_instances``' in-memory source over loopback, then use Playwright to drive the shell exactly
as a user would. Every open goes through the New Tab page or a rail row, every verb through the
shell's relay, and every assertion on state reads the shell's own API or files. The shell knows no
app by name, so a stub app is every app.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator

import pytest
from app_instances.blueprint import build_instances_app
from app_instances.nudge import ShellNudger
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port
from app_manifest.primitives import AppName
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import FakeTemplateCatalogFetcher
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import catalog_document
from imbue.system_interface.testing import catalog_template_document
from imbue.system_interface.testing import is_e2e_browser_installed
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
        reason=("System interface frontend not built (run `cd system && npm run build`); skipping e2e."),
    ),
]

_PORT = 18765
_BASE_URL = f"http://127.0.0.1:{_PORT}"

# The one project every server starts with unless a test asks for none: what a migrated
# workspace has, and where a fresh browser lands (the first project, before Everything).
STARTER_PROJECT_NAME = "Project 1"
STARTER_PROJECT_ID = "project-1"
EVERYTHING_VIEW_NAME = "Everything"

# The stub app the machine offers: a multi-instance app whose instances live in memory,
# created by its ``new`` action and titled "Stub N". Every server seeds it with the fixture
# instance, the one the tests open first.
_STUB_APP_NAME = "docs"
_STUB_APP_DISPLAY_NAME = "Docs"
_STUB_NEW_ACTION_LABEL = "New docs"
_FIXTURE_KEY = "stub-1"
_FIXTURE_TITLE = "Stub 1"
_FIXTURE_ADDRESS = f"app:{_STUB_APP_NAME}?instance={_FIXTURE_KEY}"

# A second stub app, offered when a test needs two apps on the machine (the launcher's filter).
_SECOND_APP_NAME = "notes"
_SECOND_APP_DISPLAY_NAME = "Notes"
_SECOND_APP_ADDRESS = f"app:{_SECOND_APP_NAME}?instance=stub-1"

# What a stub tab reads, and what a fresh stub instance's address looks like.
_STUB_TAB_TITLE_RE = re.compile(r"^Stub \d+$")

_TRIGGER_TIMEOUT_MS = 20000

# A one-template catalog for the New Tab page's "Start from a template" section: one shelf, one
# card, published from a repository the adopt message names.
_CATALOG_TEMPLATE_SLUG = "inbox-digest"
_CATALOG_TEMPLATE_TITLE = "Inbox Digest"
_CATALOG_TEMPLATE_REPOSITORY_URL = "https://github.com/someone/inbox-digest"
_CATALOG_DOCUMENT = catalog_document(
    catalog_template_document(
        _CATALOG_TEMPLATE_SLUG,
        title=_CATALOG_TEMPLATE_TITLE,
        description="A digest of your inbox.",
        what_it_is="Turns a noisy inbox into a scannable digest.",
        author="someone",
        repository_url=_CATALOG_TEMPLATE_REPOSITORY_URL,
        thumbnail="",
    ),
    shelves=[{"key": "popular", "title": "Most popular", "slugs": [_CATALOG_TEMPLATE_SLUG]}],
)


class E2EServer(FrozenModel):
    """Handle to a running e2e server and its fixtures."""

    model_config = {"arbitrary_types_allowed": True}

    base_url: str = Field(description="The shell's loopback URL")
    state_dir: Path = Field(description="The shell's state directory")
    stub_source: StubInstanceSource = Field(description="The stub app's in-memory instances")
    stub_url: str = Field(description="The stub app's loopback URL, where its pages are framed from")
    second_source: StubInstanceSource | None = Field(description="The second stub app's instances, when offered")


def _post_json(url: str, body: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    port: int,
    stub_instances: tuple[str, ...] = (_FIXTURE_KEY,),
    is_second_app_offered: bool = False,
    project_names: tuple[str, ...] = (STARTER_PROJECT_NAME,),
    is_stub_taking_message: bool = False,
    is_catalog_offered: bool = False,
    catalog_body: bytes | None = None,
) -> Generator[E2EServer, None, None]:
    """Run the shell with a stub app whose ``stub_instances`` are seeded as records titled after their keys.

    ``project_names`` are created through the shell's API before the browser lands, so the
    client's first view is the first of them (or Everything when there are none). Nothing is
    auto-opened: the first landing is the New Tab page. ``is_stub_taking_message`` declares a
    ``message`` param on the stub's ``new`` action, which is what makes it the app the page's seeded
    prompts go to. With ``is_catalog_offered`` the shell has a template catalog URL, answered by
    ``catalog_body`` -- or by nothing, so the page sees the catalog fail to load.
    """
    base_url = f"http://127.0.0.1:{port}"
    registry_path = tmp_path / "registry" / "apps.toml"
    stub_source = StubInstanceSource()
    for key in stub_instances:
        stub_source.records.append(instance_record(key, title=f"Stub {key.removeprefix('stub-')}"))
    stub_port = free_port()
    stub_url = f"http://{LOOPBACK_HOST}:{stub_port}"
    rows = [
        registry_row_toml(
            _STUB_APP_NAME,
            stub_url,
            is_multi_instance=True,
            actions=(("new", _STUB_NEW_ACTION_LABEL),),
            default_shortcut=("new", "focus"),
            display_name=_STUB_APP_DISPLAY_NAME,
            action_params={"new": ("message",)} if is_stub_taking_message else None,
        )
    ]
    second_source: StubInstanceSource | None = None
    second_port = free_port()
    if is_second_app_offered:
        second_source = StubInstanceSource()
        second_source.records.append(instance_record("stub-1", title="Note 1"))
        rows.append(
            registry_row_toml(
                _SECOND_APP_NAME,
                f"http://{LOOPBACK_HOST}:{second_port}",
                is_multi_instance=True,
                actions=(("new", "New notes"),),
                default_shortcut=("new", "focus"),
                display_name=_SECOND_APP_DISPLAY_NAME,
            )
        )
    write_registry(registry_path, *rows)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", base_url)
        state_dir = tmp_path / "shell-state"
        config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
        catalog_fetcher: FakeTemplateCatalogFetcher | None = None
        if is_catalog_offered:
            catalog_fetcher = FakeTemplateCatalogFetcher()
            if catalog_body is not None:
                catalog_fetcher.body_by_url[config.system_interface_template_catalog_url] = catalog_body
        state = build_test_state(
            config=config, shell_state_directory=state_dir, template_catalog_fetcher=catalog_fetcher
        )
        app = create_application(state)

        server = make_threaded_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stub_server = serve_in_background(
            LOOPBACK_HOST,
            stub_port,
            build_instances_app(
                stub_source,
                ShellNudger(app_name=AppName(_STUB_APP_NAME), shell_url=base_url),
            ),
        )
        second_server = (
            serve_in_background(
                LOOPBACK_HOST,
                second_port,
                build_instances_app(
                    second_source,
                    ShellNudger(app_name=AppName(_SECOND_APP_NAME), shell_url=base_url),
                ),
            )
            if second_source is not None
            else contextlib.nullcontext()
        )
        with stub_server, second_server:
            try:
                wait_for(
                    lambda: _server_is_up(base_url),
                    timeout=10.0,
                    poll_interval=0.1,
                    error_message=f"workspace server did not come up at {base_url}",
                )
                for name in project_names:
                    _post_json(
                        f"{base_url}/api/projects",
                        {"name": name, "color": "#3B82F6", "glyph": 1},
                    )
                # Started only once the apps are serving: the first instance fetch must find them answering.
                state.shell.start()
                try:
                    yield E2EServer(
                        base_url=base_url,
                        state_dir=state_dir,
                        stub_source=stub_source,
                        stub_url=stub_url,
                        second_source=second_source,
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
    """Start the shell with the fixture instance and the starter project."""
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
    """The dock names the view it has mounted; the active view itself lives in the shell's client record."""
    page.wait_for_selector(
        f'.dockview-workspace[data-view-id="{view_id}"]',
        state="attached",
        timeout=15000,
    )


def _launcher_row(page: Page, address: str) -> Any:
    return page.locator(f'.new-tab-launcher-row[data-address="{address}"]:visible')


def _search_launcher(page: Page, query: str) -> None:
    """Type into the New Tab page's search field, which swaps the page for the machine-wide results."""
    page.locator(".new-tab-launcher:visible .new-tab-launcher-search input").fill(query)


def _open_from_launcher(page: Page, address: str) -> None:
    """Open an instance from the New Tab page (opening the page from the "+" when none is up).

    A project's resting page lists only its own tab set, so an instance the project does not hold
    is reached the way a user reaches it: by searching for its app.
    """
    # The dock must be up first: before it mounts there is neither a launcher nor a "+". A view
    # that mounts its launcher does so a beat after the switch lands, so the launcher gets a
    # moment to appear before the "+" (which a launcher hides) is reached for.
    expect(page.locator(".dv-default-tab-content").first).to_be_visible(timeout=15000)
    launcher = page.locator(".new-tab-launcher:visible")
    if launcher.count() == 0:
        try:
            expect(launcher.first).to_be_visible(timeout=3000)
        except AssertionError:
            page.locator(".dockview-add-tab-button:visible").first.click()
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
    row = _launcher_row(page, address)
    if row.count() == 0:
        _search_launcher(page, _app_name_of_address(address))
    expect(row.first).to_be_visible(timeout=15000)
    row.first.click()


def _app_name_of_address(address: str) -> str:
    return address.removeprefix("app:").split("?", 1)[0]


def _open_fixture_instance(page: Page) -> None:
    """Open the fixture instance from the New Tab page and wait for its tab and frame."""
    _open_from_launcher(page, _FIXTURE_ADDRESS)
    expect(_tab(page, _FIXTURE_TITLE)).to_be_visible(timeout=15000)
    expect(page.locator(f'iframe[data-address="{_FIXTURE_ADDRESS}"]')).to_have_count(1, timeout=15000)


def _tab(page: Page, title: str | re.Pattern[str]) -> Any:
    return page.locator(".dv-default-tab-content", has_text=title).first


def _broadcast_layout_op(base_url: str, op: str, args: dict[str, Any], view: str = STARTER_PROJECT_NAME) -> None:
    """POST a layout op to the loopback ``/api/layout/broadcast`` endpoint.

    This is the same path ``system/scripts/layout.py`` drives, so issuing a ``split`` here
    exercises the real frontend handler. Mutating ops are view-targeted and only succeed
    once the page's ``client_state`` registration has landed, so a 412 is retried.
    """
    payload = json.dumps(
        {
            "op": op,
            "args": {**args, "view": view},
            "requester": "app:chat?instance=agent-e2e",
        }
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
    return f"app:{_STUB_APP_NAME}?instance={key}"


def _open_rail_switcher(page: Page) -> None:
    page.locator(".project-rail-header").click()
    expect(page.locator(".project-rail-menu")).to_be_visible(timeout=5000)


def _switch_view_via_rail(page: Page, view_name: str) -> None:
    """Pick a view from the rail's switcher, re-opening it when a click did not land.

    The rail's menu closes on outside mousedown and window blur and re-renders on every
    inventory or projects broadcast, so on a loaded runner the item click occasionally
    lands on a menu that has just closed or been rebuilt and the view stays put, with the
    menu gone either way. The retry keys on the switch itself: the rail header names the
    active view as soon as the shell moves (before the incoming layout is fetched), so a
    header still naming the old view after a click is a miss, as is a menu that closed
    before the click could reach it.
    """
    menu = page.locator(".project-rail-menu")
    header = page.locator(".project-rail-header")
    for attempt in range(3):
        try:
            if menu.count() == 0:
                _open_rail_switcher(page)
            menu.locator("[role='menuitem']", has_text=view_name).first.click(timeout=5000)
            expect(header).to_contain_text(view_name, timeout=5000)
            return
        except (AssertionError, PlaywrightTimeoutError):
            if attempt == 2:
                raise


def _collapse_rail(page: Page) -> None:
    """Fold the hover-expanded rail back up, so the dock underneath is clickable."""
    page.mouse.move(600, 400)
    expect(page.locator(".project-rail-search")).to_have_count(0, timeout=15000)


# A page for a stub instance's frame, served by a Playwright route rather than by the stub
# (which serves only its instances API). Its state is an ``<input>``: typing into it is a
# change no reload survives, because the served markup has it empty.
_FRAMED_PAGE_HTML = "<!doctype html><html><body><input id='held' value='' /></body></html>"


def _serve_stub_pages(page: Page, server: E2EServer) -> None:
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


def _wait_for_surface_shown(page: Page, address: str, stamp: str | None = None) -> dict[str, Any]:
    """The surface report once a surface holding ``address`` is on screen.

    The live layer places a page's surface on the animation frame after the dock has laid
    its pane out (a zero-sized pane keeps it hidden), so a report taken the instant the tab
    appears can find the element present but not yet shown; the wait is for that frame.
    """
    page.wait_for_function(
        f"([address, stamp]) => ({_SURFACE_REPORT_JS.strip()})([address, stamp]).shownCount >= 1",
        arg=[address, stamp],
        timeout=15000,
    )
    return _surface_report(page, address, stamp)


# ---------- the shell ----------


@pytest.mark.timeout(30, func_only=False)
def test_page_loads_and_shows_title(e2e_server: E2EServer, page: Page) -> None:
    page.goto(e2e_server.base_url)
    expect(page).to_have_title("System Interface")


@pytest.mark.timeout(60, func_only=False)
def test_first_landing_is_the_new_tab_page_offering_the_machine(e2e_server: E2EServer, page: Page) -> None:
    """A fresh browser lands on the starter project's New Tab page; nothing is opened for it.

    The dock is never empty: a view with nothing to mount shows the launcher as its one
    "New tab" tab. The project's own tab set is empty, so no table is shown and the page goes
    from "Open new" straight to the offers; the machine's instance is reached through the
    search field, in the "On this machine" results. No frame exists until someone opens one.
    """
    page.goto(e2e_server.base_url)
    _wait_for_view(page, STARTER_PROJECT_ID)
    expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
    expect(page.locator(".dv-default-tab-content")).to_have_count(1)
    expect(page.locator(".dv-default-tab-content").first).to_have_text("New tab")
    expect(page.locator(f'.new-tab-launcher-tile[data-launch="{_STUB_APP_NAME}:new"]')).to_be_visible(timeout=15000)
    expect(page.locator(".new-tab-launcher-section")).to_have_count(0)
    expect(page.locator(".new-tab-start-tile")).to_have_count(6)
    expect(page.locator(".new-tab-templates")).to_have_count(0)

    _search_launcher(page, _STUB_APP_NAME)
    row = page.locator(".new-tab-launcher-section[data-section='on-machine']").locator(
        f'.new-tab-launcher-row[data-address="{_FIXTURE_ADDRESS}"]'
    )
    expect(row).to_have_count(1, timeout=15000)
    expect(row).to_contain_text(_FIXTURE_TITLE)
    expect(page.locator(".new-tab-start-tile")).to_have_count(0)
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
    _serve_stub_pages(page, e2e_server)
    _open_fixture_instance(page)

    expect(page.frame_locator(f'iframe[data-address="{_FIXTURE_ADDRESS}"]').locator("#held")).to_be_visible(
        timeout=15000
    )
    wait_for(
        lambda: _FIXTURE_ADDRESS in _project_tabs(e2e_server.base_url),
        timeout=15.0,
        poll_interval=0.1,
        error_message="opening the instance never filed it into the starter project",
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
        expect(_launcher_row(page, _FIXTURE_ADDRESS)).to_have_count(1, timeout=15000)


@pytest.mark.timeout(120, func_only=False)
def test_new_tab_opens_in_clicked_split(tmp_path: Path, page: Page) -> None:
    """The header "+" opens the new tab in the split whose header was clicked.

    Split the layout into two groups, make the LEFT group active (so dockview's default
    "add to the active group" would land a new tab on the left), then click the RIGHT
    split's "+" and create an instance from the launcher. It must land in the RIGHT split.
    """
    with _running_e2e_server(tmp_path, _PORT + 3, stub_instances=(_FIXTURE_KEY, "stub-2")) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_instance(page)
        add_buttons = page.locator(".dockview-add-tab-button")
        expect(add_buttons).to_have_count(1)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)

        _broadcast_layout_op(
            server.base_url,
            "split",
            {
                "address": _stub_address("stub-2"),
                "relative_to": _FIXTURE_ADDRESS,
                "direction": "right",
                "new_group": True,
            },
        )
        expect(add_buttons).to_have_count(2, timeout=10000)
        expect(_tab(page, "Stub 2")).to_be_visible(timeout=10000)

        _tab(page, _FIXTURE_TITLE).click()
        left_group = page.locator(
            ".dv-groupview",
            has=page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE),
        )
        expect(left_group).to_have_class(re.compile(r"\bdv-active-group\b"))

        boxes = [add_buttons.nth(i).bounding_box() for i in range(2)]
        assert boxes[0] is not None and boxes[1] is not None
        right_index = 0 if boxes[0]["x"] > boxes[1]["x"] else 1
        add_buttons.nth(right_index).click()

        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        page.locator(f'.new-tab-launcher-tile:visible[data-launch="{_STUB_APP_NAME}:new"]').click()

        expect(_tab(page, "Stub 3")).to_be_visible(timeout=15000)
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
            "Stub 3",
        )
        assert placement["count"] == 2, f"new tab should join the right split, not create a third group: {placement}"
        assert placement["inRight"], f"new tab should be in the right split: {placement}"
        assert not placement["inLeft"], f"new tab leaked into the left split: {placement}"
        # The create went through the relay to the app, which minted the instance.
        assert [str(record.key) for record in server.stub_source.records] == [
            "stub-1",
            "stub-2",
            "stub-3",
        ]


@pytest.mark.timeout(120, func_only=False)
def test_load_op_switches_the_clients_view(tmp_path: Path, page: Page) -> None:
    """``layout.py load <view>`` switches what the connected client is showing."""
    with _running_e2e_server(tmp_path, _PORT + 7) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)

        payload = json.dumps(
            {
                "op": "load",
                "args": {"view": EVERYTHING_VIEW_NAME},
                "requester": "app:chat?instance=agent-e2e",
            }
        ).encode()
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
            _attempt,
            timeout=15.0,
            poll_interval=0.2,
            error_message="the load op never got past client registration",
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
        _open_fixture_instance(page)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)

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
        # Back on the starter project, the instance it holds is restored from its saved layout.
        expect(_tab(page, _FIXTURE_TITLE)).to_be_visible(timeout=15000)


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
    with _running_e2e_server(tmp_path, _PORT + 10) as server:
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
        on_return = _wait_for_surface_shown(page, address)
        assert on_return["count"] == 1, f"the page forked into a second copy: {on_return}"
        assert on_return["stamps"] == ["the-original-element"], (
            f"the element was re-created on the way back: {on_return}"
        )
        assert on_return["removals"] == 0, f"a live surface left the DOM during the round trip: {on_return}"
        expect(held_field).to_have_value("typed-by-the-user")


@pytest.mark.timeout(60, func_only=False)
def test_a_tab_opened_right_before_a_view_switch_is_saved_into_the_view_it_was_opened_in(
    tmp_path: Path, page: Page
) -> None:
    """Switching views flushes the outgoing view's pending autosave first.

    Autosave is debounced; a switch inside that window must still write the edit into the
    view it was made in, or the tab is gone when the user comes back (and a referenced
    instance opened that way would be left with nothing referencing it).
    """
    address = _stub_address("stub-1")
    with _running_e2e_server(tmp_path, _PORT + 21) as server:
        _serve_stub_pages(page, server)
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_from_launcher(page, address)
        expect(page.locator(f'iframe[data-address="{address}"]')).to_have_count(1, timeout=_TRIGGER_TIMEOUT_MS)
        # Straight on to the switch, well inside the autosave debounce.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=address)
        assert not any(
            address in path.read_text() for path in _client_layout_files(server.state_dir, EVERYTHING_VIEW_ID)
        ), "the outgoing view's arrangement was saved under the incoming view"


# Flaky: the switch back to the project sometimes never mounts its view. The client then alternates
# fetching the project's and Everything's layouts every half second until the wait times out, which
# is a race in the view switch itself, not in this test.
@pytest.mark.flaky
@pytest.mark.timeout(120, func_only=False)
def test_one_instance_is_one_element_in_every_view_showing_it(tmp_path: Path, page: Page) -> None:
    """An instance shown by two views is ONE element, shown twice -- never two."""
    with _running_e2e_server(tmp_path, _PORT + 11) as server:
        _serve_stub_pages(page, server)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_instance(page)
        page.evaluate(_WATCH_SURFACE_REMOVALS_JS)
        in_project = _wait_for_surface_shown(page, _FIXTURE_ADDRESS, "the-original-element")
        assert in_project["count"] == 1, f"the starter project should hold exactly one page: {in_project}"
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)

        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        _open_from_launcher(page, _FIXTURE_ADDRESS)
        expect(_tab(page, _FIXTURE_TITLE)).to_be_visible(timeout=15000)

        in_everything = _wait_for_surface_shown(page, _FIXTURE_ADDRESS)
        assert in_everything["count"] == 1, f"opening the instance in Everything forked its page: {in_everything}"
        assert in_everything["stamps"] == ["the-original-element"], (
            f"Everything is showing a different element than the starter project: {in_everything}"
        )
        assert in_everything["removals"] == 0, f"a live surface left the DOM on the way in: {in_everything}"

        _switch_view_via_rail(page, STARTER_PROJECT_NAME)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(_tab(page, _FIXTURE_TITLE)).to_be_visible(timeout=15000)
        back_in_project = _surface_report(page, _FIXTURE_ADDRESS)
        assert back_in_project["count"] == 1, f"switching back forked the page: {back_in_project}"
        assert back_in_project["stamps"] == ["the-original-element"], (
            f"switching back re-created the page's element: {back_in_project}"
        )
        assert back_in_project["removals"] == 0, (
            f"a live surface left the DOM during the round trip: {back_in_project}"
        )


# ---------- verbs ----------


@pytest.mark.timeout(120, func_only=False)
def test_double_click_renames_an_instance_and_the_name_survives_a_reload(tmp_path: Path, page: Page) -> None:
    """Double-clicking a tab's title renames the instance through its app, and the name is kept.

    The rename goes through the shell's relay to the app, which records the new title and
    lists it; the tab re-derives its title from the inventory, so a reload proves the name
    stuck to the instance, not the tab.
    """
    with _running_e2e_server(tmp_path, _PORT + 12) as server:
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_instance(page)
        tab_title = _tab(page, _FIXTURE_TITLE)
        expect(tab_title).to_be_visible(timeout=15000)

        tab_title.dblclick()
        editor = page.locator(".dv-custom-tab-title-input:visible")
        expect(editor).to_be_visible(timeout=5000)
        expect(editor).to_have_value(_FIXTURE_TITLE)
        editor.fill("Design notes")
        editor.press("Enter")

        expect(_tab(page, "Design notes")).to_be_visible(timeout=10000)
        expect(page.locator(".dv-custom-tab-title-input:visible")).to_have_count(0)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)

        page.reload()
        expect(_tab(page, "Design notes")).to_be_visible(timeout=15000)
        assert [str(record.title) for record in server.stub_source.records] == ["Design notes"]
        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE)).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_deleting_an_instance_removes_it_from_the_app_and_every_view(tmp_path: Path, page: Page) -> None:
    """Delete from a tab's menu deletes the instance in its app; the shell drops it from every view.

    The instance is opened in the starter project and in Everything, then deleted from the
    project's tab. The app's records lose it, the tab leaves the mounted view, the address
    leaves the project's tab set, and mounting Everything afterwards restores no tab for it.
    """
    address = _FIXTURE_ADDRESS
    with _running_e2e_server(tmp_path, _PORT + 13) as server:
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
            lambda: server.stub_source.records == [],
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
        _open_fixture_instance(page)
        wait_for(
            lambda: _FIXTURE_ADDRESS in _project_tabs(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the fixture instance was never filed into the starter project",
        )

        page.locator(".machine-sidebar").hover()
        fixture_row = page.locator(f'.project-rail-tab[data-address="{_FIXTURE_ADDRESS}"]')
        expect(fixture_row).to_have_count(1)
        fixture_row.click(button="right")
        page.locator(".project-rail-menu [role='menuitem']", has_text="Remove from project").click()

        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE)).to_have_count(0, timeout=10000)
        wait_for(
            lambda: _FIXTURE_ADDRESS not in _project_tabs(server.base_url),
            timeout=15.0,
            poll_interval=0.1,
            error_message="Remove from project never took the address out of the tab set",
        )

        # It kept running: Everything's machine-wide table still offers it.
        _switch_view_via_rail(page, EVERYTHING_VIEW_NAME)
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=15000)
        expect(_launcher_row(page, _FIXTURE_ADDRESS)).to_have_count(1, timeout=15000)


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
    with _running_e2e_server(tmp_path, _PORT + 15, stub_instances=()) as server:
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
    with _running_e2e_server(tmp_path, _PORT + 16, stub_instances=(), project_names=()) as server:
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
    """Unchecking an app in a table's filter hides its rows; Reset re-checks all.

    In a project the machine table is a search result, so the search is what brings both apps'
    rows into one table (both instances are titled "... 1").
    """
    with _running_e2e_server(tmp_path, _PORT + 17, is_second_app_offered=True) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)
        _search_launcher(page, "1")

        section = page.locator(".new-tab-launcher-section[data-section='on-machine']")
        notes_row = section.locator(f'.new-tab-launcher-row[data-address="{_SECOND_APP_ADDRESS}"]')
        stub_row = section.locator(f'.new-tab-launcher-row[data-address="{_FIXTURE_ADDRESS}"]')
        expect(notes_row).to_have_count(1, timeout=15000)
        expect(stub_row).to_have_count(1, timeout=15000)

        section.locator("button[aria-expanded]").click()
        notes_checkbox = section.locator("label", has_text=_SECOND_APP_DISPLAY_NAME)
        expect(notes_checkbox).to_be_visible(timeout=5000)
        notes_checkbox.click()
        expect(notes_row).to_have_count(0)
        expect(stub_row).to_have_count(1)

        section.locator("button", has_text="Reset filters").click()
        expect(notes_row).to_have_count(1)
        expect(stub_row).to_have_count(1)


@pytest.mark.timeout(120, func_only=False)
def test_new_tab_lists_the_template_catalog_and_adopts_one_into_a_seeded_chat(tmp_path: Path, page: Page) -> None:
    """The catalog's shelves render as rails of cards, a card opens its detail, and "Make it mine"
    starts a chat whose first message adopts the template. The stub app declares that its ``new``
    action takes a message, which is all the page goes by, so the create it receives is what the
    page sent: the ``new`` action with the message."""
    with _running_e2e_server(
        tmp_path, _PORT + 23, is_stub_taking_message=True, is_catalog_offered=True, catalog_body=_CATALOG_DOCUMENT
    ) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)

        shelves = page.locator(".new-tab-template-shelf")
        expect(shelves).to_have_count(2, timeout=15000)
        expect(shelves.first).to_have_attribute("data-shelf", "popular")
        expect(shelves.last).to_have_attribute("data-shelf", "all")
        card = shelves.first.locator(f'.new-tab-template-card[data-template="{_CATALOG_TEMPLATE_SLUG}"]')
        expect(card).to_contain_text(_CATALOG_TEMPLATE_TITLE)

        card.click()
        detail = page.locator(".new-tab-template-detail")
        expect(detail).to_be_visible(timeout=5000)
        expect(detail).to_contain_text("Turns a noisy inbox into a scannable digest.")

        page.locator(".new-tab-template-adopt").click()
        is_adopted = poll_until(
            lambda: f"create:new:{{'message': '/use-template {_CATALOG_TEMPLATE_REPOSITORY_URL}'}}"
            in server.stub_source.calls,
            timeout=15.0,
            poll_interval=0.1,
        )
        if not is_adopted:
            pytest.fail(f"adopting the template never created a seeded chat: {server.stub_source.calls}")
        expect(page.locator(".new-tab-template-detail")).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_new_tab_start_something_seeds_a_chat_with_the_tiles_prompt(tmp_path: Path, page: Page) -> None:
    """A "Start something" tile creates a chat carrying its prompt as the first message; "See more"
    reveals the tiles past the first page."""
    with _running_e2e_server(tmp_path, _PORT + 24, is_stub_taking_message=True) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(page.locator(".new-tab-launcher")).to_be_visible(timeout=10000)

        expect(page.locator('.new-tab-start-tile[data-start="learn"]')).to_have_count(0)
        page.locator(".new-tab-start-more").click()
        expect(page.locator('.new-tab-start-tile[data-start="learn"]')).to_be_visible(timeout=5000)
        expect(page.locator(".new-tab-start-more")).to_have_count(0)

        page.locator('.new-tab-start-tile[data-start="learn"]').click()
        is_seeded = poll_until(
            lambda: any(
                call.startswith("create:new:{'message': 'Teach me about Minds") for call in server.stub_source.calls
            ),
            timeout=15.0,
            poll_interval=0.1,
        )
        if not is_seeded:
            pytest.fail(f"the tile never created a seeded chat: {server.stub_source.calls}")


@pytest.mark.timeout(60, func_only=False)
def test_new_tab_says_when_the_template_catalog_could_not_be_loaded(tmp_path: Path, page: Page) -> None:
    with _running_e2e_server(tmp_path, _PORT + 25, is_catalog_offered=True) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        expect(page.locator(".new-tab-templates-status")).to_have_text("Failed to load templates.", timeout=15000)
        expect(page.locator(".new-tab-template-shelf")).to_have_count(0)


# ---------- the tab strip ----------


@pytest.mark.timeout(180, func_only=False)
def test_overflowed_tabs_list_as_plain_rows_and_the_strip_keeps_its_handles(tmp_path: Path, page: Page) -> None:
    """Tabs folded into the "N more" dropdown list as bare rows; the strip stays whole.

    While the dropdown is open, two live renderer instances exist for one panel -- the
    strip's and the row's -- and only the strip's may own the panel's handle and controls.
    """
    keys = tuple(f"stub-{n}" for n in range(2, 10))
    with _running_e2e_server(tmp_path, _PORT + 18, stub_instances=(_FIXTURE_KEY, *keys)) as server:
        page.set_viewport_size({"width": 900, "height": 700})
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_instance(page)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)

        # Filled from the New Tab page, as a user would: a row opened from the "+" of the group
        # lands in that group (an agent's ``open`` would split beside the active group instead).
        for key in keys:
            _open_from_launcher(page, _stub_address(key))
            expect(_tab(page, f"Stub {key.removeprefix('stub-')}")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)

        overflow_control = page.locator(".dv-tabs-overflow-dropdown-default")
        wait_for(
            lambda: overflow_control.is_visible(),
            timeout=5.0,
            poll_interval=0.1,
            error_message="the strip never overflowed: 9 tabs all fit at 900px wide",
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

        strip_tab = page.locator(
            ".dv-tab",
            has=page.locator(".dv-default-tab-content", has_text=clicked_title),
        ).first
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
    with _running_e2e_server(tmp_path, _PORT + 19, stub_instances=(_FIXTURE_KEY, "stub-2")) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        _open_fixture_instance(page)
        _wait_for_layout_saved(server.state_dir, STARTER_PROJECT_ID, containing=_FIXTURE_ADDRESS)
        _broadcast_layout_op(
            server.base_url,
            "open",
            {"address": _stub_address("stub-2"), "new_group": False},
        )
        expect(_tab(page, "Stub 2")).to_be_visible(timeout=_TRIGGER_TIMEOUT_MS)

        dragged = page.locator(".dv-tab", has=page.locator(".dv-default-tab-content", has_text="Stub 2")).first
        target_tab = page.locator(
            ".dv-tab",
            has=page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE),
        ).first
        source_box = dragged.bounding_box()
        assert source_box is not None, "the dragged tab has no box"
        page.mouse.move(
            source_box["x"] + source_box["width"] / 2,
            source_box["y"] + source_box["height"] / 2,
        )
        page.mouse.down()

        target_box = target_tab.bounding_box()
        assert target_box is not None, "the target tab has no box"
        page.mouse.move(
            target_box["x"] + target_box["width"] * 0.2,
            target_box["y"] + target_box["height"] / 2,
            steps=25,
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
        page.mouse.move(
            pane_box["x"] + pane_box["width"] * 0.15,
            pane_box["y"] + pane_box["height"] / 2,
            steps=25,
        )
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
            _open_from_launcher(mobile_page, _FIXTURE_ADDRESS)
            expect(mobile_page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE).first).to_be_visible(
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


# ---------- the layout file is the truth ----------


@pytest.mark.timeout(120, func_only=False)
def test_two_windows_of_one_client_mirror_a_server_made_arrangement(tmp_path: Path, page: Page) -> None:
    """An agent's op edits the client's file on the shell; both windows of that client show it without a reload,
    and the window that did not act saves nothing back (no echo)."""
    with _running_e2e_server(tmp_path, _PORT + 21) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        # A second window of the same browser context shares the stored client id: one client, two windows.
        second = page.context.new_page()
        try:
            second.goto(server.base_url)
            _wait_for_view(second, STARTER_PROJECT_ID)
            expect(second.locator(".new-tab-launcher")).to_be_visible(timeout=15000)

            _broadcast_layout_op(server.base_url, "open", {"address": _FIXTURE_ADDRESS})

            for window in (page, second):
                expect(window.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE).first).to_be_visible(
                    timeout=15000
                )
            layout_files = _client_layout_files(server.state_dir, STARTER_PROJECT_ID)
            assert len(layout_files) == 1, "two windows of one browser are one client with one layout file"
            stored = json.loads(layout_files[0].read_text())
            stamp = stored["updated_at"]
            # The windows applied the file rather than saving their own copies over it: the stamp holds.
            second.wait_for_timeout(3000)
            assert json.loads(layout_files[0].read_text())["updated_at"] == stamp
            assert [panel["params"]["address"] for panel in stored["dockview"]["panels"].values()] == [_FIXTURE_ADDRESS]
            assert "tabs" not in stored
        finally:
            second.close()


@pytest.mark.timeout(120, func_only=False)
def test_a_deep_link_lands_on_the_view_and_docks_the_instance(tmp_path: Path, page: Page) -> None:
    """``/?view=<id>&open=<address>`` switches the requesting client to the view, docks the instance, and leaves
    a clean URL behind; a stale target is ignored."""
    with _running_e2e_server(tmp_path, _PORT + 22) as server:
        page.goto(server.base_url)
        _wait_for_view(page, STARTER_PROJECT_ID)
        # The machine lists the instance before the deep link asks for it, as a switcher entry would find it
        # (a project's page reaches the machine through its search).
        _search_launcher(page, _STUB_APP_NAME)
        expect(_launcher_row(page, _FIXTURE_ADDRESS).first).to_be_visible(timeout=15000)

        address = urllib.parse.quote(_FIXTURE_ADDRESS, safe="")
        page.goto(f"{server.base_url}/?view={EVERYTHING_VIEW_ID}&open={address}&follow=nobody")

        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE).first).to_be_visible(timeout=15000)
        page.wait_for_function("!window.location.search.includes('view=')", timeout=15000)
        assert "open=" not in page.url and "follow=" not in page.url
        # The client record follows the deep link, so the next plain load lands on Everything too.
        wait_for(
            lambda: any(
                client["active_view"] == EVERYTHING_VIEW_ID
                for client in _get_json(f"{server.base_url}/api/clients")["clients"]
            ),
            timeout=15.0,
            poll_interval=0.2,
            error_message="the client record never recorded the deep link's view",
        )
        # The docked tab is autosaved into Everything's file before the next load, which then restores it.
        _wait_for_layout_saved(server.state_dir, EVERYTHING_VIEW_ID, containing=_FIXTURE_ADDRESS)
        page.goto(f"{server.base_url}/?open=app%3Anowhere%3Finstance%3Dgone")
        _wait_for_view(page, EVERYTHING_VIEW_ID)
        expect(page.locator(".dv-default-tab-content", has_text=_FIXTURE_TITLE).first).to_be_visible(timeout=15000)
