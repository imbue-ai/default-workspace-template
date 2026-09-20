"""End-to-end tests for the desktop shell using Playwright.

These tests start a real Flask server (threaded Werkzeug) over a registry of stub apps served by
``app_instances``' in-memory source over loopback, then use Playwright to drive the shell exactly
as a user would: every open goes through a shortcut, a launcher tile, a page's own ``shell:open``,
an agent op, or a deep link; every gesture through the pointer; and every assertion on state reads
the shell's own API or its files. The framed pages are static stand-ins that import the shell's
served ``app_contract.js`` and speak the contract, so titles, URL following, and ``shell:open``
are exercised for real. The shell knows no app by name, so a stub app is every app.
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
from flask import Flask
from flask import Response
from flask import request
from playwright.sync_api import BrowserContext
from playwright.sync_api import Frame
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import expect
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import FakeTemplateCatalogFetcher
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import catalog_document
from imbue.system_interface.testing import catalog_template_document
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.wsgi import make_threaded_server


def _frontend_built() -> bool:
    """Whether the frontend has been built: without ``static/index.html`` the server answers a placeholder, and
    without ``static/_static/app_contract.js`` the stand-in pages cannot speak to the shell."""
    static = Path(__file__).parent / "static"
    return (static / "index.html").is_file() and (static / "_static" / "app_contract.js").is_file()


pytestmark = [
    pytest.mark.release,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontend_built(),
        reason="System interface frontend not built (run `cd system && npm run build`); skipping e2e.",
    ),
]

# The default desktop every shell starts with (desktops.py), whose shortcuts are seeded from the registry.
_HOME_DESKTOP_ID = "home"

# How long a negative assertion ("nothing more opened") gives the shell before reading its state.
_NEGATIVE_SETTLE_MS = 1000

# The stub app the machine offers: a multi-instance app with one launch path, ``new`` at ``/new``, and a
# focus-mode default shortcut for it. Its pages are the stand-ins the stub serves at every other path.
_STUB_APP_NAME = "docs"
_STUB_APP_DISPLAY_NAME = "Docs"
_STUB_LAUNCH_ID = "new"
_STUB_LAUNCH_LABEL = "New docs"
_STUB_LAUNCH_PATH = "/new"
_STUB_SHORTCUT_KEY = f"{_STUB_APP_NAME}:{_STUB_LAUNCH_ID}"

# A second stub app, offered when a test needs two apps (the launcher's filter, shortcut collisions).
_SECOND_APP_NAME = "notes"
_SECOND_APP_DISPLAY_NAME = "Notes"
_SECOND_SHORTCUT_KEY = f"{_SECOND_APP_NAME}:{_STUB_LAUNCH_ID}"

# The metrics of the default theme (frontend/src/theme/default.css), for driving gestures by pixel.
_CELL_WIDTH = 96
_CELL_HEIGHT = 112
_GRID_INSET = 16
_SNAP_THRESHOLD = 16
_GEOMETRY_TOLERANCE_PX = 4

# A one-template catalog for the launcher's "Start from a template" section.
_CATALOG_TEMPLATE_SLUG = "inbox-digest"
_CATALOG_TEMPLATE_TITLE = "Inbox Digest"
_CATALOG_DOCUMENT = catalog_document(
    catalog_template_document(
        _CATALOG_TEMPLATE_SLUG,
        title=_CATALOG_TEMPLATE_TITLE,
        description="A digest of your inbox.",
        what_it_is="Turns a noisy inbox into a scannable digest.",
        author="someone",
        repository_url="https://github.com/someone/inbox-digest",
        thumbnail="",
    ),
    shelves=[{"key": "popular", "title": "Most popular", "slugs": [_CATALOG_TEMPLATE_SLUG]}],
)


class E2EServer(FrozenModel):
    """Handle to a running e2e server and its fixtures."""

    base_url: str = Field(description="The shell's loopback URL")
    state_dir: Path = Field(description="The shell's state directory")
    stub_url: str = Field(description="The stub app's loopback URL, where its pages are framed from")


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    is_second_app_offered: bool = False,
    is_stub_taking_message: bool = False,
    is_catalog_offered: bool = False,
) -> Generator[E2EServer, None, None]:
    """Run the shell on a free port over the stub app (and the second one when asked), each seeded with one instance.

    ``is_stub_taking_message`` declares a ``message`` param on the stub's ``new`` launch path, which is what makes
    it the app the launcher's seeded prompts go to. With ``is_catalog_offered`` the shell has a template catalog.
    """
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    registry_path = tmp_path / "registry" / "apps.toml"
    stub_source = StubInstanceSource()
    stub_source.records.append(instance_record("stub-1", title="Stub 1"))
    stub_port = free_port()
    stub_url = f"http://{LOOPBACK_HOST}:{stub_port}"
    rows = [
        registry_row_toml(
            _STUB_APP_NAME,
            stub_url,
            is_multi_instance=True,
            actions=((_STUB_LAUNCH_ID, _STUB_LAUNCH_LABEL),),
            default_shortcut=(_STUB_LAUNCH_ID, "focus"),
            default_shortcut_launch=_STUB_LAUNCH_ID,
            display_name=_STUB_APP_DISPLAY_NAME,
            action_params={_STUB_LAUNCH_ID: ("message",)} if is_stub_taking_message else None,
            launch_paths=((_STUB_LAUNCH_ID, _STUB_LAUNCH_LABEL, _STUB_LAUNCH_PATH),),
        )
    ]
    second_source: StubInstanceSource | None = None
    second_port = free_port()
    second_url = f"http://{LOOPBACK_HOST}:{second_port}"
    if is_second_app_offered:
        second_source = StubInstanceSource()
        second_source.records.append(instance_record("stub-1", title="Note 1"))
        rows.append(
            registry_row_toml(
                _SECOND_APP_NAME,
                second_url,
                is_multi_instance=True,
                actions=((_STUB_LAUNCH_ID, "New notes"),),
                default_shortcut=(_STUB_LAUNCH_ID, "focus"),
                default_shortcut_launch=_STUB_LAUNCH_ID,
                display_name=_SECOND_APP_DISPLAY_NAME,
                launch_paths=((_STUB_LAUNCH_ID, "New notes", _STUB_LAUNCH_PATH),),
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
            catalog_fetcher.body_by_url[config.system_interface_template_catalog_url] = _CATALOG_DOCUMENT
        state = build_test_state(
            config=config, shell_state_directory=state_dir, template_catalog_fetcher=catalog_fetcher
        )
        app = create_application(state)

        stub_server = serve_in_background(
            LOOPBACK_HOST, stub_port, _stub_app(stub_source, AppName(_STUB_APP_NAME), base_url)
        )
        second_server = (
            serve_in_background(
                LOOPBACK_HOST, second_port, _stub_app(second_source, AppName(_SECOND_APP_NAME), base_url)
            )
            if second_source is not None
            else contextlib.nullcontext()
        )
        with stub_server, second_server:
            # Bound and started here, inside the stubs' contexts, so the shutdown below owns it whatever fails
            # first (a stub whose port is taken never leaves a bound shell socket behind).
            server = make_threaded_server("127.0.0.1", port, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                wait_for(
                    lambda: _server_is_up(base_url),
                    timeout=10.0,
                    poll_interval=0.1,
                    error_message=f"workspace server did not come up at {base_url}",
                )
                # Started only once the apps are serving: the first instance fetch must find them answering.
                state.shell.start()
                try:
                    yield E2EServer(base_url=base_url, state_dir=state_dir, stub_url=stub_url)
                finally:
                    state.shell.stop()
            finally:
                server.shutdown()
                thread.join(timeout=5.0)
                server.server_close()


def _server_is_up(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/desktops", timeout=0.5):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[E2EServer, None, None]:
    """Start the shell over the one stub app."""
    with _running_e2e_server(tmp_path) as server:
        yield server


# A page for a stub window's frame, served by the stub app itself on its loopback origin (a page a Playwright route
# fulfils has no network origin, and Chromium's local-network policy then refuses it the shell's contract module).
# It imports the shell's served contract module and connects: it reports its location (path and a title derived
# from it) once greeted, and exposes the verbs the tests drive (navigate in place, ask for an open). A navigable
# page declares the capability and shows a pushed path in place; a plain one declares nothing, so the shell reloads
# its frame to move it. Its ``#held`` input is state no reload survives.
_STUB_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Stub</title></head><body>
<div id="where"></div><input id="held" value="" />
<script type="module">
import { connectToShell } from "__BASE_URL__/_static/app_contract.js";
const isNavigable = __NAVIGABLE__;
const where = document.getElementById("where");
const titleOf = (path) => "Stub " + path;
const here = () => location.pathname + location.search;
const show = (path) => { where.textContent = path; document.title = titleOf(path); };
show(here());
window.__navigations = [];
window.__presses = 0;
window.addEventListener("pointerdown", () => { window.__presses += 1; });
const handlers = {
  onHandshake(handshake) {
    window.__handshake = handshake;
    connection.location(here(), titleOf(here()));
  },
};
if (isNavigable) {
  handlers.capabilities = { navigation: true };
  handlers.onNavigate = (path) => {
    history.replaceState(null, "", path);
    show(path);
    window.__navigations.push(path);
  };
}
const connection = connectToShell(handlers);
window.__navigateTo = (path) => {
  history.pushState(null, "", path);
  show(path);
  connection.location(path, titleOf(path));
};
window.__openPath = (path, ifPresent) => connection.openPath(path, ifPresent);
</script></body></html>"""

# The cookie a browser context sets on a stub origin to be served the plain (non-navigable) page.
_PLAIN_PAGE_COOKIE = "stub-page"
_PLAIN_PAGE_COOKIE_VALUE = "plain"


def _stub_page_html(base_url: str, is_navigable: bool) -> str:
    return _STUB_PAGE_TEMPLATE.replace("__BASE_URL__", base_url).replace(
        "__NAVIGABLE__", "true" if is_navigable else "false"
    )


def _stub_app(source: StubInstanceSource, app_name: AppName, base_url: str) -> Flask:
    """The stub app: the instances API, plus the stand-in page at every other path."""
    app = build_instances_app(source, ShellNudger(app_name=app_name, shell_url=base_url))

    def _page(path: str = "") -> Response:
        is_navigable = request.cookies.get(_PLAIN_PAGE_COOKIE) != _PLAIN_PAGE_COOKIE_VALUE
        return Response(_stub_page_html(base_url, is_navigable), mimetype="text/html")

    app.add_url_rule("/", view_func=_page, endpoint="stub_page_root")
    app.add_url_rule("/<path:path>", view_func=_page, endpoint="stub_page")
    return app


def _use_plain_pages(context: BrowserContext, server: E2EServer) -> None:
    """Have the stub app serve this context the plain page, the one without in-place navigation."""
    context.add_cookies([{"name": _PLAIN_PAGE_COOKIE, "value": _PLAIN_PAGE_COOKIE_VALUE, "url": server.stub_url}])


def _desktops(base_url: str) -> list[dict[str, Any]]:
    return list(_get_json(f"{base_url}/api/desktops")["desktops"])


def _desktop(base_url: str, desktop_id: str = _HOME_DESKTOP_ID) -> dict[str, Any]:
    return next(desktop for desktop in _desktops(base_url) if desktop["id"] == desktop_id)


def _windows(base_url: str, desktop_id: str = _HOME_DESKTOP_ID) -> list[dict[str, Any]]:
    return list(_desktop(base_url, desktop_id)["windows"])


def _shortcut_cells(base_url: str, desktop_id: str = _HOME_DESKTOP_ID) -> dict[str, tuple[int, int]]:
    """Each shortcut's cell by its ``app:launch`` key, off the API."""
    return {
        f"{shortcut['target']['app']}:{shortcut['target']['launch']}": (
            shortcut["cell"]["column"],
            shortcut["cell"]["row"],
        )
        for shortcut in _desktop(base_url, desktop_id)["shortcuts"]
    }


def _placements(base_url: str, client_id: str, desktop_id: str = _HOME_DESKTOP_ID) -> dict[str, dict[str, Any]]:
    """The client's stored placements of the desktop by window id, straight off the API."""
    layout = _get_json(f"{base_url}/api/placements/{desktop_id}?client={urllib.parse.quote(client_id)}")
    return {placement["window_id"]: placement for placement in layout["placements"]}


def _placement_file(state_dir: Path, client_id: str, desktop_id: str = _HOME_DESKTOP_ID) -> Path:
    return state_dir / "placements" / desktop_id / f"{client_id}.json"


def _stored_placements(
    state_dir: Path, client_id: str, desktop_id: str = _HOME_DESKTOP_ID
) -> dict[str, dict[str, Any]]:
    path = _placement_file(state_dir, client_id, desktop_id)
    if not path.is_file():
        return {}
    return {placement["window_id"]: placement for placement in json.loads(path.read_text())["placements"]}


def _wait_for_stored_placement(
    server: E2EServer,
    client_id: str,
    window_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    what: str,
    desktop_id: str = _HOME_DESKTOP_ID,
) -> dict[str, Any]:
    """Wait until the client's placement file holds a placement of ``window_id`` satisfying ``predicate``."""

    def _saved() -> bool:
        placement = _stored_placements(server.state_dir, client_id, desktop_id).get(window_id)
        return placement is not None and predicate(placement)

    wait_for(_saved, timeout=15.0, poll_interval=0.1, error_message=f"autosave never wrote {what} for {window_id}")
    return _stored_placements(server.state_dir, client_id, desktop_id)[window_id]


def _assert_no_further_window(page: Page, server: E2EServer, expected_ids: list[str]) -> None:
    """The shell opened nothing beyond ``expected_ids``: a negative nothing announces, so this settles once and
    then reads the desktop's windows."""
    page.wait_for_timeout(_NEGATIVE_SETTLE_MS)
    assert [window["id"] for window in _windows(server.base_url)] == expected_ids


def _wait_for_window_count(base_url: str, count: int, desktop_id: str = _HOME_DESKTOP_ID) -> list[dict[str, Any]]:
    wait_for(
        lambda: len(_windows(base_url, desktop_id)) == count,
        timeout=15.0,
        poll_interval=0.1,
        error_message=f"desktop {desktop_id} never held {count} window(s)",
    )
    return _windows(base_url, desktop_id)


def _broadcast_op(base_url: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """POST an op to ``/api/layout/broadcast`` the way ``system/scripts/layout.py`` does, retrying while the shell
    has not yet registered the client the op names (a 404 or 412)."""
    payload = json.dumps({"op": op, "args": args, "requester": None}).encode()
    answer: dict[str, Any] = {}

    def _attempt() -> bool:
        request = urllib.request.Request(
            f"{base_url}/api/layout/broadcast",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                answer.update(json.loads(response.read()))
                return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 412):
                return False
            raise AssertionError(f"op {op!r} refused with HTTP {e.code}: {e.read().decode(errors='replace')}") from e
        except (TimeoutError, urllib.error.URLError):
            return False

    wait_for(_attempt, timeout=15.0, poll_interval=0.2, error_message=f"op {op!r} never succeeded")
    return answer


def _client_id(page: Page) -> str:
    client_id = page.evaluate("() => localStorage.getItem('si-client-id')")
    assert isinstance(client_id, str) and client_id
    return client_id


def _land(page: Page, server: E2EServer, query: str = "") -> None:
    """Open the shell and wait for the home desktop's backdrop and its seeded shortcut."""
    page.goto(f"{server.base_url}/{query}")
    expect(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]')).to_be_visible(timeout=15000)
    expect(page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')).to_be_visible(timeout=15000)


def _window(page: Page, window_id: str) -> Locator:
    return page.locator(f'[data-window-id="{window_id}"]')


def _shown_windows(page: Page) -> Locator:
    return page.locator("[data-window-id]")


def _taskbar_entry(page: Page, window_id: str) -> Locator:
    return page.locator(f'[data-taskbar-entry="{window_id}"]')


def _double_click(shortcut: Locator) -> None:
    shortcut.dblclick()


def _tap(shortcut: Locator) -> None:
    shortcut.tap()


def _open_via_shortcut(
    page: Page, server: E2EServer, key: str = _STUB_SHORTCUT_KEY, run: Callable[[Locator], None] = _double_click
) -> str:
    """Run a shortcut (by double click unless ``run`` says otherwise) and wait for the one new window it opens;
    answers the window id."""
    before = {window["id"] for window in _windows(server.base_url)}
    run(page.locator(f'[data-shortcut="{key}"]'))
    wait_for(
        lambda: len(set(window["id"] for window in _windows(server.base_url)) - before) == 1,
        timeout=15.0,
        poll_interval=0.1,
        error_message="the shortcut opened no window",
    )
    (window_id,) = set(window["id"] for window in _windows(server.base_url)) - before
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    return window_id


def _page_frame(page: Page, window_id: str) -> Frame:
    """The Playwright frame of the window's live page, once it has been greeted by the shell."""
    handle = page.locator(f'iframe[data-live-page="{window_id}"]').element_handle(timeout=15000)
    frame = handle.content_frame()
    assert frame is not None
    frame.wait_for_function("() => window.__handshake !== undefined", timeout=15000)
    return frame


def _box(locator: Locator) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None, "the element has no box"
    return box


def _center(box: dict[str, float]) -> tuple[float, float]:
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _drag(page: Page, start: tuple[float, float], end: tuple[float, float], is_released: bool = True) -> None:
    """A pointer drag from ``start`` to ``end`` in steps, so the threshold and every snap zone see the pointer."""
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0] + 8, start[1] + 8, steps=2)
    page.mouse.move(*end, steps=12)
    if is_released:
        page.mouse.up()


def _drag_title_bar(page: Page, window_id: str, dx: float, dy: float) -> None:
    handle = _window(page, window_id).locator("[data-drag-handle]")
    start = _center(_box(handle))
    _drag(page, start, (start[0] + dx, start[1] + dy))


def _move_window_off_the_shortcuts(page: Page, window_id: str) -> None:
    """Drag the window toward the bottom right, clear of the seeded shortcuts' cells."""
    _drag_title_bar(page, window_id, 500, 350)


def _assert_close(actual: float, expected: float, what: str) -> None:
    assert abs(actual - expected) <= _GEOMETRY_TOLERANCE_PX, f"{what}: {actual} is not within tolerance of {expected}"


def _assert_same_box(actual: dict[str, float], expected: dict[str, float], what: str) -> None:
    for key in ("x", "y", "width", "height"):
        _assert_close(actual[key], expected[key], f"{what} {key}")


def _cell_center(backdrop: dict[str, float], column: int, row: int) -> tuple[float, float]:
    return (
        backdrop["x"] + _GRID_INSET + column * _CELL_WIDTH + _CELL_WIDTH / 2,
        backdrop["y"] + _GRID_INSET + row * _CELL_HEIGHT + _CELL_HEIGHT / 2,
    )


def _launch_message(path: str) -> str:
    """The ``message`` a launch path's query carries."""
    (message,) = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["message"]
    return message


def _open_launcher(page: Page) -> Locator:
    field = page.locator("[data-launcher-field]")
    if field.locator("input").count() > 0:
        field.locator("input").click()
    else:
        field.click()
    overlay = page.locator("[data-launcher-overlay]")
    expect(overlay).to_be_visible(timeout=10000)
    return overlay


def _open_desktops_menu(page: Page) -> None:
    page.locator("[data-desktops-menu]").click()
    expect(page.locator('[data-floating="desktops-menu"]')).to_be_visible(timeout=5000)


def _second_context(page: Page, **context_args: Any) -> BrowserContext:
    """A second browser context: its own storage, so its own client id."""
    browser = page.context.browser
    assert browser is not None
    return browser.new_context(**context_args)


@contextlib.contextmanager
def _second_client(page: Page, e2e_server: E2EServer, **context_args: Any) -> Generator[Page, None, None]:
    """A page of a second browser context (its own client id), landed on the shell and closed with the context."""
    context = _second_context(page, **context_args)
    try:
        other_page = context.new_page()
        _land(other_page, e2e_server)
        yield other_page
    finally:
        context.close()


@pytest.mark.timeout(60, func_only=False)
def test_fresh_browser_lands_on_home_with_the_seeded_shortcut_and_registers_as_a_client(
    e2e_server: E2EServer, page: Page
) -> None:
    """A fresh browser lands on the home desktop over the bundled wallpaper: the seeded shortcut sits in the first
    cell, nothing is open, the taskbar carries the launcher field and both tray widgets, and the shell soon knows
    the client with home as its active desktop."""
    _land(page, e2e_server)
    expect(page).to_have_title("System Interface")
    shortcut = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
    expect(shortcut).to_have_attribute("data-cell", "0,0")
    expect(shortcut.locator(".shortcut-label")).to_have_text(_STUB_APP_DISPLAY_NAME)
    expect(_shown_windows(page)).to_have_count(0)
    expect(page.locator("[data-taskbar] [data-launcher-field]")).to_be_visible()
    expect(page.locator('[data-tray-widget="desktops"] [data-desktop-switch]')).to_have_count(1)
    expect(page.locator(f'[data-running-app="{_STUB_APP_NAME}"]')).to_be_visible()
    assert (
        page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]')
        .evaluate("(el) => getComputedStyle(el).backgroundImage")
        .endswith('/wallpapers/bundled/dawn")')
    )
    client_id = _client_id(page)
    wait_for(
        lambda: any(
            client["id"] == client_id and client["active_desktop"] == _HOME_DESKTOP_ID
            for client in _get_json(f"{e2e_server.base_url}/api/clients")["clients"]
        ),
        timeout=15.0,
        poll_interval=0.2,
        error_message="the shell never registered the client on the home desktop",
    )


@pytest.mark.timeout(60, func_only=False)
def test_shortcut_opens_a_window_whose_page_speaks_the_contract(e2e_server: E2EServer, page: Page) -> None:
    """A double click on the shortcut opens a window at the launch path: the shell records the window and this
    client's shown placement, frames the page at the app's origin plus the path, greets it with the window, the
    desktop, and the path, and takes the title the page reports into the title bar and the taskbar."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)

    (window,) = _windows(e2e_server.base_url)
    assert window["app"] == _STUB_APP_NAME and window["path"] == _STUB_LAUNCH_PATH
    client_id = _client_id(page)
    placement = _placements(e2e_server.base_url, client_id)[window_id]
    assert placement["is_minimized"] is False and placement["state"] == "NORMAL"

    frame = _page_frame(page, window_id)
    assert frame.url == f"{e2e_server.stub_url}{_STUB_LAUNCH_PATH}"
    handshake = frame.evaluate("() => window.__handshake")
    assert handshake["clientId"] == client_id
    assert handshake["windowId"] == window_id
    assert handshake["desktopId"] == _HOME_DESKTOP_ID
    assert handshake["path"] == _STUB_LAUNCH_PATH
    expect(_window(page, window_id).locator(".window-title")).to_have_text(f"Stub {_STUB_LAUNCH_PATH}", timeout=10000)
    expect(_taskbar_entry(page, window_id)).to_contain_text(f"Stub {_STUB_LAUNCH_PATH}")
    expect(_taskbar_entry(page, window_id)).to_have_attribute("data-focused", "true")
    wait_for(
        lambda: _windows(e2e_server.base_url)[0]["title"] == f"Stub {_STUB_LAUNCH_PATH}",
        timeout=10.0,
        poll_interval=0.1,
        error_message="the reported title never reached the window record",
    )


@pytest.mark.timeout(60, func_only=False)
def test_focus_shortcut_raises_the_existing_window_and_its_menu_opens_another(
    e2e_server: E2EServer, page: Page
) -> None:
    """The seeded shortcut is in focus mode: run again it raises the app's window rather than opening a second;
    the launch path from its context menu always opens another."""
    _land(page, e2e_server)
    first = _open_via_shortcut(page, e2e_server)
    _move_window_off_the_shortcuts(page, first)
    _window(page, first).locator('[data-window-control="minimize"]').click()
    expect(_taskbar_entry(page, first)).to_have_attribute("data-minimized", "true")
    page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]').dblclick()
    expect(_window(page, first)).to_be_visible(timeout=10000)
    _assert_no_further_window(page, e2e_server, [first])

    page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]').click(button="right")
    expect(page.locator('[data-floating="shortcut-menu"]')).to_be_visible(timeout=5000)
    page.locator('[data-menu-item="open-new"]').click()
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    assert {window["path"] for window in windows} == {_STUB_LAUNCH_PATH}
    expect(_shown_windows(page)).to_have_count(2, timeout=15000)


@pytest.mark.timeout(60, func_only=False)
def test_launcher_tile_search_and_this_desktop_rows(tmp_path: Path, page: Page) -> None:
    """The launcher opens from its field: its tiles list every app's launch paths, a tile opens a window, searching
    narrows the tiles and the rows to matches, and a row of an open window brings that window forward."""
    with _running_e2e_server(tmp_path, is_second_app_offered=True) as server:
        _land(page, server)
        overlay = _open_launcher(page)
        expect(overlay.locator(f'.launcher-tile[data-launch="{_STUB_SHORTCUT_KEY}"]')).to_be_visible()
        expect(overlay.locator(f'.launcher-tile[data-launch="{_SECOND_SHORTCUT_KEY}"]')).to_be_visible()
        expect(overlay.locator("[data-launcher-window]")).to_have_count(0)

        overlay.locator(f'.launcher-tile[data-launch="{_SECOND_SHORTCUT_KEY}"]').click()
        (window,) = _wait_for_window_count(server.base_url, 1)
        assert window["app"] == _SECOND_APP_NAME
        expect(_window(page, window["id"])).to_be_visible(timeout=15000)
        expect(overlay).to_be_hidden()

        _window(page, window["id"]).locator('[data-window-control="minimize"]').click()
        expect(_taskbar_entry(page, window["id"])).to_have_attribute("data-minimized", "true")
        overlay = _open_launcher(page)
        expect(overlay.locator(f'[data-launcher-window="{window["id"]}"]')).to_be_visible()
        overlay.locator(f'[data-launcher-window="{window["id"]}"]').click()
        expect(overlay).to_be_hidden()
        expect(_window(page, window["id"])).to_be_visible(timeout=10000)
        expect(_window(page, window["id"])).to_have_attribute("data-focused", "true")

        overlay = _open_launcher(page)
        page.locator("[data-launcher-field] input").fill("docs")
        expect(overlay.locator("[data-launch]")).to_have_count(1)
        expect(overlay.locator(f'[data-launch="{_STUB_SHORTCUT_KEY}"]')).to_be_visible()
        expect(overlay.locator("[data-launcher-window]")).to_have_count(0)
        page.locator("[data-launcher-field] input").fill("zzzz")
        expect(overlay.locator(".launcher-no-matches")).to_be_visible()
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        expect(overlay).to_be_hidden()


@pytest.mark.timeout(60, func_only=False)
def test_launcher_intents_and_templates_seed_a_message(tmp_path: Path, page: Page) -> None:
    """A "Start something" intent opens the message-taking launch path with the prompt as its query; the catalog's
    template card does the same with its adopt message."""
    with _running_e2e_server(tmp_path, is_stub_taking_message=True, is_catalog_offered=True) as server:
        _land(page, server)
        overlay = _open_launcher(page)
        overlay.locator('[data-start="build-app"]').click()
        (window,) = _wait_for_window_count(server.base_url, 1)
        assert window["path"].startswith(f"{_STUB_LAUNCH_PATH}?message=")
        assert "build a new app" in _launch_message(window["path"])

        overlay = _open_launcher(page)
        card = overlay.locator(f'button[data-template="{_CATALOG_TEMPLATE_SLUG}"]').first
        expect(card).to_be_visible(timeout=10000)
        card.click()
        detail = page.locator(f'[role="dialog"][data-template="{_CATALOG_TEMPLATE_SLUG}"]')
        expect(detail).to_be_visible(timeout=5000)
        detail.locator(".new-tab-template-adopt").click()
        windows = _wait_for_window_count(server.base_url, 2)
        messages = {_launch_message(window["path"]) for window in windows}
        assert "/use-template https://github.com/someone/inbox-digest" in messages, messages


@pytest.mark.timeout(60, func_only=False)
def test_page_shell_open_opens_a_sibling_window_of_its_app(e2e_server: E2EServer, page: Page) -> None:
    """A page's ``shell:open`` opens another window of its own app at the path it names; with ``focus`` a window
    already at that path is raised instead."""
    _land(page, e2e_server)
    first = _open_via_shortcut(page, e2e_server)
    frame = _page_frame(page, first)
    frame.evaluate("() => window.__openPath('/?doc=2', 'focus')")
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    (second,) = [window for window in windows if window["id"] != first]
    assert second["app"] == _STUB_APP_NAME and second["path"] == "/?doc=2"
    expect(_window(page, second["id"])).to_have_attribute("data-focused", "true", timeout=15000)
    second_frame = _page_frame(page, second["id"])
    assert second_frame.url == f"{e2e_server.stub_url}/?doc=2"

    frame.evaluate("() => window.__openPath('/?doc=2', 'focus')")
    _assert_no_further_window(page, e2e_server, [first, second["id"]])
    frame.evaluate("() => window.__openPath('/?doc=2', 'new')")
    (third,) = [
        window
        for window in _wait_for_window_count(e2e_server.base_url, 3)
        if window["id"] not in (first, second["id"])
    ]
    assert third["app"] == _STUB_APP_NAME and third["path"] == "/?doc=2"


@pytest.mark.timeout(60, func_only=False)
def test_agent_open_op_opens_a_window_for_the_named_client(e2e_server: E2EServer, page: Page) -> None:
    """An agent's ``open`` op naming an app, a path, and the client opens the window on that client's active
    desktop, shown and focused there, without a reload."""
    _land(page, e2e_server)
    client_id = _client_id(page)
    answer = _broadcast_op(
        e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/?doc=7", "client": client_id}
    )
    window_id = answer["window_id"]
    assert answer["desktop_id"] == _HOME_DESKTOP_ID
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    expect(_window(page, window_id)).to_have_attribute("data-focused", "true")
    frame = _page_frame(page, window_id)
    assert frame.url == f"{e2e_server.stub_url}/?doc=7"
    assert _placements(e2e_server.base_url, client_id)[window_id]["is_minimized"] is False


@pytest.mark.timeout(60, func_only=False)
def test_deep_links_open_a_path_and_run_a_launch_path(e2e_server: E2EServer, page: Page) -> None:
    """``?open=<app>:<path>`` opens that page and ``?launch=<app>:<launch>`` runs the launch path, each once, and
    the shell strips the parameters from its URL so a reload opens nothing more."""
    _land(page, e2e_server, "?" + urllib.parse.urlencode({"open": f"{_STUB_APP_NAME}:/?doc=5"}))
    (window,) = _wait_for_window_count(e2e_server.base_url, 1)
    assert window["path"] == "/?doc=5"
    expect(_window(page, window["id"])).to_be_visible(timeout=15000)
    assert "open=" not in page.url

    _land(page, e2e_server, "?" + urllib.parse.urlencode({"launch": f"{_STUB_APP_NAME}:{_STUB_LAUNCH_ID}"}))
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    assert {window["path"] for window in windows} == {"/?doc=5", _STUB_LAUNCH_PATH}
    assert "launch=" not in page.url
    page.reload()
    expect(_shown_windows(page)).to_have_count(2, timeout=15000)
    _assert_no_further_window(page, e2e_server, [window["id"] for window in windows])


@pytest.mark.timeout(90, func_only=False)
def test_move_and_resize_persist_across_reload(e2e_server: E2EServer, page: Page) -> None:
    """Dragging the title bar moves the window and dragging a corner resizes it; the placement file records both
    and a reload puts the window back where it was, at the size it was."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    client_id = _client_id(page)
    before = _box(_window(page, window_id))

    _drag_title_bar(page, window_id, 120, 60)
    moved = _box(_window(page, window_id))
    _assert_close(moved["x"], before["x"] + 120, "moved x")
    _assert_close(moved["y"], before["y"] + 60, "moved y")

    corner = _center(_box(_window(page, window_id).locator('[data-resize-edge="se"]')))
    _drag(page, corner, (corner[0] + 100, corner[1] + 80))
    resized = _box(_window(page, window_id))
    _assert_close(resized["width"], before["width"] + 100, "resized width")
    _assert_close(resized["height"], before["height"] + 80, "resized height")
    _assert_close(resized["x"], moved["x"], "resized x")

    backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))
    stored = _wait_for_stored_placement(
        e2e_server,
        client_id,
        window_id,
        lambda placement: (
            abs(placement["frame"]["width"] * backdrop["width"] - resized["width"]) <= _GEOMETRY_TOLERANCE_PX
        ),
        "the resized frame",
    )
    assert stored["state"] == "NORMAL" and stored["is_minimized"] is False

    page.reload()
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    _assert_same_box(_box(_window(page, window_id)), resized, "after reload")


@pytest.mark.timeout(90, func_only=False)
def test_snap_maximize_and_unsnap_by_dragging(e2e_server: E2EServer, page: Page) -> None:
    """Dragging a window's title to the left edge shows the snap preview and snaps it to the left half; to the top
    edge maximizes it; dragging a snapped window's title away un-snaps it back to a normal frame. Each state lands
    in the placement file and survives a reload."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    client_id = _client_id(page)
    backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))
    window = _window(page, window_id)
    normal = _box(window)

    start = _center(_box(window.locator("[data-drag-handle]")))
    _drag(page, start, (backdrop["x"] + _SNAP_THRESHOLD / 2, start[1]), is_released=False)
    expect(page.locator("[data-snap-preview]")).to_be_visible()
    page.mouse.up()
    expect(window).to_have_attribute("data-window-state", "SNAPPED_LEFT")
    snapped = _box(window)
    _assert_close(snapped["x"], backdrop["x"], "snapped x")
    _assert_close(snapped["width"], backdrop["width"] / 2, "snapped width")
    _assert_close(snapped["height"], backdrop["height"], "snapped height")
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: placement["state"] == "SNAPPED_LEFT", "SNAPPED_LEFT"
    )
    page.reload()
    expect(window).to_have_attribute("data-window-state", "SNAPPED_LEFT", timeout=15000)

    start = _center(_box(window.locator("[data-drag-handle]")))
    _drag(page, start, (start[0] + 200, start[1] + 150))
    expect(window).to_have_attribute("data-window-state", "NORMAL")
    unsnapped = _box(window)
    _assert_close(unsnapped["width"], normal["width"], "unsnapped width")
    _assert_close(unsnapped["height"], normal["height"], "unsnapped height")
    assert unsnapped["x"] > snapped["x"] + _SNAP_THRESHOLD
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: placement["state"] == "NORMAL", "NORMAL"
    )

    start = _center(_box(window.locator("[data-drag-handle]")))
    _drag(page, start, (start[0], backdrop["y"] + _SNAP_THRESHOLD / 2))
    expect(window).to_have_attribute("data-window-state", "MAXIMIZED")
    _assert_same_box(_box(window), backdrop, "maximized")
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: placement["state"] == "MAXIMIZED", "MAXIMIZED"
    )
    page.reload()
    expect(window).to_have_attribute("data-window-state", "MAXIMIZED", timeout=15000)


@pytest.mark.timeout(90, func_only=False)
def test_title_bar_double_click_and_controls_toggle_maximize_and_minimize(e2e_server: E2EServer, page: Page) -> None:
    """A double click on the title bar maximizes and restores; the minimize control hides the window and its
    taskbar entry marks it, a click on the entry brings it back; the page's frame stays the same element across
    all of it (its typed state survives), and a second client sees the window minimized from the start."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    client_id = _client_id(page)
    window = _window(page, window_id)
    normal = _box(window)
    frame = _page_frame(page, window_id)
    frame.fill("#held", "typed before")

    window.locator("[data-drag-handle]").dblclick()
    expect(window).to_have_attribute("data-window-state", "MAXIMIZED")
    expect(window.locator('[data-window-control="restore"]')).to_be_visible()
    window.locator("[data-drag-handle]").dblclick()
    expect(window).to_have_attribute("data-window-state", "NORMAL")
    _assert_same_box(_box(window), normal, "restored")

    window.locator('[data-window-control="minimize"]').click()
    expect(_shown_windows(page)).to_have_count(0)
    expect(_taskbar_entry(page, window_id)).to_have_attribute("data-minimized", "true")
    expect(page.locator(f'iframe[data-live-page="{window_id}"]')).to_be_hidden()
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: placement["is_minimized"] is True, "minimized"
    )

    with _second_client(page, e2e_server) as other_page:
        expect(_taskbar_entry(other_page, window_id)).to_have_attribute("data-minimized", "true", timeout=15000)
        expect(_shown_windows(other_page)).to_have_count(0)
        assert _client_id(other_page) != client_id

    _taskbar_entry(page, window_id).click()
    expect(window).to_be_visible()
    expect(_taskbar_entry(page, window_id)).to_have_attribute("data-minimized", "false")
    assert _page_frame(page, window_id).input_value("#held") == "typed before"
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: placement["is_minimized"] is False, "restored"
    )


@pytest.mark.timeout(60, func_only=False)
def test_clicking_a_lower_window_raises_it_and_the_focused_one_takes_pointer_events(
    e2e_server: E2EServer, page: Page
) -> None:
    """Two windows: the later one is focused and its page takes the pointer; a press on the earlier one's content
    raises it (the shield takes the press), after which a real click into its content reaches its page through the
    transparent chrome, and the stack order is what the placement file says."""
    _land(page, e2e_server)
    first = _open_via_shortcut(page, e2e_server)
    _move_window_off_the_shortcuts(page, first)
    page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]').click(button="right")
    page.locator('[data-menu-item="open-new"]').click()
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    (second,) = [window["id"] for window in windows if window["id"] != first]
    expect(_window(page, second)).to_have_attribute("data-focused", "true", timeout=15000)
    expect(_window(page, first)).to_have_attribute("data-focused", "false")
    expect(_window(page, first).locator("[data-window-shield]")).to_have_count(1)
    expect(_window(page, second).locator("[data-window-shield]")).to_have_count(0)

    shield_box = _box(_window(page, first).locator("[data-window-shield]"))
    page.mouse.click(shield_box["x"] + 20, shield_box["y"] + shield_box["height"] - 20)
    expect(_window(page, first)).to_have_attribute("data-focused", "true")
    expect(_window(page, second)).to_have_attribute("data-focused", "false")
    expect(_window(page, first).locator("[data-window-shield]")).to_have_count(0)

    content_box = _box(_window(page, first).locator("[data-window-content]"))
    first_frame = _page_frame(page, first)
    assert first_frame.evaluate("() => window.__presses") == 0
    page.mouse.click(content_box["x"] + content_box["width"] / 2, content_box["y"] + content_box["height"] / 2)
    first_frame.wait_for_function("() => window.__presses === 1", timeout=15000)
    client_id = _client_id(page)

    def _first_on_top() -> bool:
        return list(_stored_placements(e2e_server.state_dir, client_id))[-1:] == [first]

    wait_for(_first_on_top, timeout=15.0, poll_interval=0.1, error_message="the raise never reached the file")


@pytest.mark.timeout(90, func_only=False)
def test_url_following_across_two_clients_in_place_and_by_reload(e2e_server: E2EServer, page: Page) -> None:
    """A page navigating in one client reports its location; the window's path changes for everyone, and the
    other client's page follows: a navigable page is told to move in place (its frame keeps its element and
    state), a plain page is reloaded at the new path."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    frame = _page_frame(page, window_id)

    navigable = _second_context(page)
    plain = _second_context(page)
    try:
        _use_plain_pages(plain, e2e_server)
        navigable_page = navigable.new_page()
        plain_page = plain.new_page()
        for other in (navigable_page, plain_page):
            _land(other, e2e_server)
            expect(_taskbar_entry(other, window_id)).to_be_visible(timeout=15000)
            _taskbar_entry(other, window_id).click()
            expect(_window(other, window_id)).to_be_visible(timeout=15000)
        navigable_frame = _page_frame(navigable_page, window_id)
        navigable_frame.fill("#held", "kept in place")
        plain_frame = _page_frame(plain_page, window_id)
        plain_frame.fill("#held", "lost on reload")

        frame.evaluate("() => window.__navigateTo('/?doc=2')")
        wait_for(
            lambda: _windows(e2e_server.base_url)[0]["path"] == "/?doc=2",
            timeout=15.0,
            poll_interval=0.1,
            error_message="the location report never changed the window's path",
        )
        expect(navigable_frame.locator("#where")).to_have_text("/?doc=2", timeout=15000)
        assert navigable_frame.evaluate("() => window.__navigations") == ["/?doc=2"]
        assert navigable_frame.input_value("#held") == "kept in place"

        plain_frame = _page_frame(plain_page, window_id)
        expect(plain_frame.locator("#where")).to_have_text("/?doc=2", timeout=15000)
        assert plain_frame.url == f"{e2e_server.stub_url}/?doc=2"
        assert plain_frame.input_value("#held") == ""
        # The reporting page itself was never moved: the report came from it.
        assert frame.evaluate("() => window.__navigations") == []
        for other in (page, navigable_page, plain_page):
            expect(_window(other, window_id).locator(".window-title")).to_have_text("Stub /?doc=2", timeout=15000)
    finally:
        navigable.close()
        plain.close()


@pytest.mark.timeout(90, func_only=False)
def test_close_removes_the_window_for_every_client_and_the_close_chord_closes_the_focused_one(
    e2e_server: E2EServer, page: Page
) -> None:
    """The close control removes the window from the desktop: the other client loses it too, and neither client's
    placement file keeps it. The embedder's close chord closes the focused window the same way."""
    _land(page, e2e_server)
    first = _open_via_shortcut(page, e2e_server)
    client_id = _client_id(page)
    with _second_client(page, e2e_server) as other_page:
        expect(_taskbar_entry(other_page, first)).to_be_visible(timeout=15000)

        _window(page, first).locator('[data-window-control="close"]').click()
        _wait_for_window_count(e2e_server.base_url, 0)
        expect(_taskbar_entry(page, first)).to_have_count(0, timeout=15000)
        expect(_taskbar_entry(other_page, first)).to_have_count(0, timeout=15000)
        wait_for(
            lambda: first not in _stored_placements(e2e_server.state_dir, client_id),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the closed window stayed in the placement file",
        )

    second = _open_via_shortcut(page, e2e_server)
    page.evaluate("() => window.postMessage({ type: 'minds:close-active-tab' }, '*')")
    _wait_for_window_count(e2e_server.base_url, 0)
    expect(_window(page, second)).to_have_count(0, timeout=15000)


@pytest.mark.timeout(60, func_only=False)
def test_shortcut_drag_lands_in_a_free_cell_and_a_collision_displaces_the_occupant(tmp_path: Path, page: Page) -> None:
    """Dragging a shortcut to an empty cell moves it there for everyone; dropping one on an occupied cell takes the
    cell and moves the occupant to the nearest free one, so no two shortcuts share a cell."""
    with _running_e2e_server(tmp_path, is_second_app_offered=True) as server:
        _land(page, server)
        assert _shortcut_cells(server.base_url) == {_STUB_SHORTCUT_KEY: (0, 0), _SECOND_SHORTCUT_KEY: (0, 1)}
        backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))

        docs = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
        _drag(page, _center(_box(docs)), _cell_center(backdrop, 2, 2))
        wait_for(
            lambda: _shortcut_cells(server.base_url)[_STUB_SHORTCUT_KEY] == (2, 2),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the shortcut never moved to (2, 2)",
        )
        expect(docs).to_have_attribute("data-cell", "2,2")

        notes = page.locator(f'[data-shortcut="{_SECOND_SHORTCUT_KEY}"]')
        _drag(page, _center(_box(notes)), _cell_center(backdrop, 2, 2))
        wait_for(
            lambda: _shortcut_cells(server.base_url)[_SECOND_SHORTCUT_KEY] == (2, 2),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the dropped shortcut never took the occupied cell",
        )
        # The nearest free cell, ties by lower column then lower row (contracts.md section 10).
        displaced = _shortcut_cells(server.base_url)[_STUB_SHORTCUT_KEY]
        assert displaced == (1, 2)
        expect(notes).to_have_attribute("data-cell", "2,2")
        expect(docs).to_have_attribute("data-cell", "1,2")


@pytest.mark.timeout(60, func_only=False)
def test_shortcut_menu_changes_mode_and_removes_and_the_tray_adds_one_back(e2e_server: E2EServer, page: Page) -> None:
    """The shortcut's menu flips its mode and removes it; the running app's popover in the tray adds it back."""
    _land(page, e2e_server)
    shortcut = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
    shortcut.click(button="right")
    page.locator('[data-menu-item="change-mode"]').click()
    wait_for(
        lambda: _desktop(e2e_server.base_url)["shortcuts"][0]["mode"] == "new",
        timeout=10.0,
        poll_interval=0.1,
        error_message="the mode never flipped",
    )
    expect(shortcut.locator(".shortcut-label")).to_have_text(_STUB_LAUNCH_LABEL)

    shortcut.click(button="right")
    page.locator('[data-menu-item="remove"]').click()
    expect(shortcut).to_have_count(0, timeout=10000)
    wait_for(
        lambda: _desktop(e2e_server.base_url)["shortcuts"] == [],
        timeout=10.0,
        poll_interval=0.1,
        error_message="the shortcut stayed in the desktop record",
    )

    page.locator(f'[data-running-app="{_STUB_APP_NAME}"]').click()
    popover = page.locator(f'[data-running-app-popover="{_STUB_APP_NAME}"]')
    expect(popover).to_be_visible(timeout=5000)
    popover.locator(f'[data-add-shortcut="{_STUB_SHORTCUT_KEY}"]').click()
    expect(shortcut).to_be_visible(timeout=10000)
    assert _shortcut_cells(e2e_server.base_url) == {_STUB_SHORTCUT_KEY: (0, 0)}


@pytest.mark.timeout(90, func_only=False)
def test_desktop_create_settings_switch_and_delete_through_the_tray(e2e_server: E2EServer, page: Page) -> None:
    """The Desktops widget's menu creates a desktop (the client switches to it, with the seeded shortcut), its
    settings dialog renames it, the widget switches back and forth (each desktop keeps its own windows), and the
    delete confirmation removes it, moving the client home."""
    _land(page, e2e_server)
    home_window = _open_via_shortcut(page, e2e_server)

    _open_desktops_menu(page)
    page.locator('[data-menu-item="new-desktop"]').click()
    wait_for(
        lambda: len(_desktops(e2e_server.base_url)) == 2,
        timeout=10.0,
        poll_interval=0.1,
        error_message="no second desktop was created",
    )
    (created,) = [desktop for desktop in _desktops(e2e_server.base_url) if desktop["id"] != _HOME_DESKTOP_ID]
    expect(page.locator(f'[data-desktop-id="{created["id"]}"]')).to_be_visible(timeout=15000)
    expect(page.locator(f'[data-desktop-switch="{created["id"]}"]')).to_have_attribute("data-active", "true")
    expect(page.locator(f'[data-desktop-id="{created["id"]}"] [data-shortcut="{_STUB_SHORTCUT_KEY}"]')).to_be_visible()
    expect(_taskbar_entry(page, home_window)).to_have_count(0)

    _open_desktops_menu(page)
    page.locator('[data-menu-item="settings"]').click()
    dialog = page.locator(f'[data-desktop-settings="{created["id"]}"]')
    expect(dialog).to_be_visible(timeout=5000)
    dialog.locator('input[placeholder="desktop name"]').fill("Research")
    dialog.locator(".desktop-settings-save").click()
    expect(dialog).to_be_hidden(timeout=5000)
    wait_for(
        lambda: _desktop(e2e_server.base_url, created["id"])["name"] == "Research",
        timeout=10.0,
        poll_interval=0.1,
        error_message="the rename never landed",
    )

    page.locator(f'[data-desktop-switch="{_HOME_DESKTOP_ID}"]').click()
    expect(_taskbar_entry(page, home_window)).to_be_visible(timeout=15000)
    expect(_window(page, home_window)).to_be_visible()
    page.locator(f'[data-desktop-switch="{created["id"]}"]').click()
    expect(_taskbar_entry(page, home_window)).to_have_count(0, timeout=15000)

    _open_desktops_menu(page)
    page.locator('[data-menu-item="delete"]').click()
    expect(dialog).to_be_visible(timeout=5000)
    dialog.locator(".desktop-settings-confirm-delete").click()
    wait_for(
        lambda: [desktop["id"] for desktop in _desktops(e2e_server.base_url)] == [_HOME_DESKTOP_ID],
        timeout=10.0,
        poll_interval=0.1,
        error_message="the desktop was never deleted",
    )
    expect(page.locator(f'[data-desktop-switch="{_HOME_DESKTOP_ID}"]')).to_have_attribute("data-active", "true")
    expect(_window(page, home_window)).to_be_visible(timeout=15000)


@pytest.mark.timeout(60, func_only=False)
def test_running_apps_widget_lists_windows_and_launch_paths(e2e_server: E2EServer, page: Page) -> None:
    """The Running apps widget shows one icon per running app; its popover lists the app's windows on this
    desktop (a row raises one) and its launch paths (a row opens one)."""
    _land(page, e2e_server)
    first = _open_via_shortcut(page, e2e_server)
    _window(page, first).locator('[data-window-control="minimize"]').click()
    expect(_taskbar_entry(page, first)).to_have_attribute("data-minimized", "true")

    page.locator(f'[data-running-app="{_STUB_APP_NAME}"]').click()
    popover = page.locator(f'[data-running-app-popover="{_STUB_APP_NAME}"]')
    expect(popover).to_be_visible(timeout=5000)
    expect(popover.locator(f'[data-popover-window="{first}"]')).to_be_visible()
    popover.locator(f'[data-popover-launch="{_STUB_SHORTCUT_KEY}"]').click()
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    (second,) = [window["id"] for window in windows if window["id"] != first]
    expect(_window(page, second)).to_be_visible(timeout=15000)

    page.locator(f'[data-running-app="{_STUB_APP_NAME}"]').click()
    expect(popover).to_be_visible(timeout=5000)
    popover.locator(f'[data-popover-window="{first}"]').click()
    expect(_window(page, first)).to_be_visible(timeout=10000)
    expect(_window(page, first)).to_have_attribute("data-focused", "true")


# A phone-shaped browser context, inlined so the emulated UA is pinned rather than drifting with the Playwright
# version. The shell reads compactness off the viewport width and touch off the coarse pointer.
_MOBILE_CONTEXT_ARGS: dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "viewport": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
}


@pytest.mark.timeout(90, func_only=False)
def test_phone_shows_every_window_maximized_with_an_icon_only_taskbar(e2e_server: E2EServer, page: Page) -> None:
    """On a phone the shell is compact and touch: a tap on the shortcut opens the window, every window fills the
    backdrop with no resize edges or maximize controls, the taskbar shows icons only, the launcher field is a
    button that opens the overlay, and the stored placement is the client's own (still a normal frame, since
    compactness is how this client renders, not what it saves)."""
    with _second_client(page, e2e_server, **_MOBILE_CONTEXT_ARGS) as phone_page:
        expect(phone_page.locator("html")).to_have_attribute("data-compact", "")
        expect(phone_page.locator("html")).to_have_attribute("data-touch", "")

        window_id = _open_via_shortcut(phone_page, e2e_server, run=_tap)
        window = _window(phone_page, window_id)
        expect(window).to_have_attribute("data-window-state", "MAXIMIZED")
        expect(window.locator("[data-resize-edge]")).to_have_count(0)
        expect(window.locator('[data-window-control="maximize"]')).to_have_count(0)
        expect(window.locator('[data-window-control="restore"]')).to_have_count(0)
        backdrop = _box(phone_page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))
        _assert_same_box(_box(window), backdrop, "phone window")
        expect(_taskbar_entry(phone_page, window_id)).to_be_visible()
        expect(phone_page.locator(".taskbar-entry-title")).to_have_count(0)
        assert _placements(e2e_server.base_url, _client_id(phone_page))[window_id]["state"] == "NORMAL"
        frame = _page_frame(phone_page, window_id)
        assert frame.url == f"{e2e_server.stub_url}{_STUB_LAUNCH_PATH}"

        expect(phone_page.locator("[data-launcher-field]")).to_be_visible()
        phone_page.locator("[data-launcher-field]").tap()
        expect(phone_page.locator("[data-launcher-overlay]")).to_be_visible(timeout=10000)
        expect(phone_page.locator(f'[data-launcher-overlay] [data-launcher-window="{window_id}"]')).to_be_visible()
        phone_page.keyboard.press("Escape")
        phone_page.keyboard.press("Escape")
        expect(phone_page.locator("[data-launcher-overlay]")).to_be_hidden()

        _taskbar_entry(phone_page, window_id).tap()
        expect(_shown_windows(phone_page)).to_have_count(0)
        expect(_taskbar_entry(phone_page, window_id)).to_have_attribute("data-minimized", "true")
        _taskbar_entry(phone_page, window_id).tap()
        expect(window).to_be_visible()


@pytest.mark.timeout(90, func_only=False)
def test_a_phone_and_a_laptop_share_the_windows_but_not_the_arrangement(e2e_server: E2EServer, page: Page) -> None:
    """A window the laptop opens reaches the phone's taskbar minimized (its own client's arrangement); the focus-mode
    shortcut on the phone restores that window there rather than opening another; a window the phone opens from a
    launcher tile is a window on the laptop too, minimized there in turn."""
    _land(page, e2e_server)
    laptop_window = _open_via_shortcut(page, e2e_server)
    with _second_client(page, e2e_server, **_MOBILE_CONTEXT_ARGS) as phone_page:
        expect(_taskbar_entry(phone_page, laptop_window)).to_have_attribute("data-minimized", "true", timeout=15000)

        phone_page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]').tap()
        expect(_window(phone_page, laptop_window)).to_have_attribute("data-window-state", "MAXIMIZED", timeout=15000)
        _assert_no_further_window(phone_page, e2e_server, [laptop_window])

        phone_page.locator("[data-launcher-field]").tap()
        overlay = phone_page.locator("[data-launcher-overlay]")
        expect(overlay).to_be_visible(timeout=10000)
        overlay.locator(f'.launcher-tile[data-launch="{_STUB_SHORTCUT_KEY}"]').tap()
        windows = _wait_for_window_count(e2e_server.base_url, 2)
        (phone_window,) = [window["id"] for window in windows if window["id"] != laptop_window]
        expect(_window(phone_page, phone_window)).to_have_attribute("data-window-state", "MAXIMIZED", timeout=15000)
        expect(_taskbar_entry(page, phone_window)).to_have_attribute("data-minimized", "true", timeout=15000)
        expect(_window(page, laptop_window)).to_have_attribute("data-focused", "true")
