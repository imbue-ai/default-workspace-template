"""End-to-end tests for the desktop shell using Playwright.

These tests start a real Flask server (threaded Werkzeug) over a registry of stub apps served by
stand-in pages over loopback, then use Playwright to drive the shell exactly
as a user would: every open goes through a shortcut, a launcher row, a page's own ``shell:open``,
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
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Generator

import pytest
from app_manifest.manifest import RESERVED_LAUNCH_PARAM_NAMES
from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from playwright.sync_api import BrowserContext
from playwright.sync_api import FloatRect
from playwright.sync_api import Frame
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import expect
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.shell.testing import write_rollback_point
from imbue.system_interface.shell.testing import write_stub_update_self_script
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.testing import is_server_answering
from imbue.system_interface.testing import serve_app
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

# The stub app the machine offers: an app with one launch path, ``new`` at ``/new``, and a
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

# A pinned stub app (pinned-taskbar-entries plan), offered when a test needs a pinned window: its root is the home
# path, and its pin is what the chat's manifest declares. Its two further launch paths take typed text
# (launcher-and-getting-started plan section 3.1), so it is what the launcher's free-text rows run.
_PINNED_APP_NAME = "buddy"
_PINNED_APP_DISPLAY_NAME = "Buddy"
_PINNED_HOME_PATH = "/"
_PINNED_NEW_LAUNCH_ID = "new"
_PINNED_SEND_LAUNCH_ID = "send"
_PINNED_DRAFT_LAUNCH_ID = "draft"
_PINNED_TEXT_PARAM = "message"

# The query parameter a stub's POST launch path names itself under in the page path it answers.
_LAUNCHED_VIA_PARAM = "via"

# The metrics of the default theme (frontend/src/theme/default.css), for driving gestures by pixel.
_CELL_WIDTH = 96
_CELL_HEIGHT = 112
_GRID_INSET = 16
_SNAP_THRESHOLD = 16
_GEOMETRY_TOLERANCE_PX = 4


class E2EServer(FrozenModel):
    """Handle to a running e2e server and its fixtures."""

    base_url: str = Field(description="The shell's loopback URL")
    state_dir: Path = Field(description="The shell's state directory")
    repo_root: Path = Field(description="The workspace root the update notice reads its record under")
    stub_url: str = Field(description="The stub app's loopback URL, where its pages are framed from")
    pinned_url: str = Field(default="", description="The pinned stub app's loopback URL; empty when none is offered")
    agent_events_path: Path = Field(
        description="The agents event file the avatar's mood is read from, absent at first"
    )


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _post_json(url: str, payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    is_second_app_offered: bool = False,
    # The pinned stub's ``[pin]`` as ``(style, scope, default_mode)``; None offers no pinned app.
    pin: tuple[str, str, str] | None = None,
) -> Generator[E2EServer, None, None]:
    """Run the shell on a free port over the stub app (and the second one when asked).

    With ``pin`` another stub app is registered with that pin, so every desktop holds its pinned window, and with
    POST launch paths taking typed and drafted text, so the launcher has its free-text rows and the avatar dialog
    its draft.
    """
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    registry_path = tmp_path / "registry" / "apps.toml"

    # The stubs serve first, so the registry can name where their pages are framed from.
    second_serving = serve_app(_stub_app(base_url)) if is_second_app_offered else contextlib.nullcontext()
    pinned_serving = serve_app(_stub_app(base_url)) if pin is not None else contextlib.nullcontext()
    with (
        pytest.MonkeyPatch.context() as monkeypatch,
        serve_app(_stub_app(base_url)) as stub_served,
        second_serving as second_served,
        pinned_serving as pinned_served,
    ):
        stub_url = stub_served.http_url
        rows = [
            registry_row_toml(
                _STUB_APP_NAME,
                stub_url,
                default_shortcut=(_STUB_LAUNCH_ID, "focus"),
                display_name=_STUB_APP_DISPLAY_NAME,
                launch_paths=((_STUB_LAUNCH_ID, _STUB_LAUNCH_LABEL, _STUB_LAUNCH_PATH),),
            )
        ]
        if second_served is not None:
            rows.append(
                registry_row_toml(
                    _SECOND_APP_NAME,
                    second_served.http_url,
                    default_shortcut=(_STUB_LAUNCH_ID, "focus"),
                    display_name=_SECOND_APP_DISPLAY_NAME,
                    launch_paths=((_STUB_LAUNCH_ID, "New notes", _STUB_LAUNCH_PATH),),
                )
            )
        if pinned_served is not None and pin is not None:
            rows.append(
                registry_row_toml(
                    _PINNED_APP_NAME,
                    pinned_served.http_url,
                    display_name=_PINNED_APP_DISPLAY_NAME,
                    pin=(_PINNED_HOME_PATH, *pin),
                    # The pin's home path is a plain page, as the chat's root is; the other three are POST launch
                    # paths as the chat's, two taking typed text and one a draft.
                    launch_paths=(
                        ("root", _PINNED_APP_DISPLAY_NAME, _PINNED_HOME_PATH),
                        (_PINNED_NEW_LAUNCH_ID, f"New {_PINNED_APP_DISPLAY_NAME}", "/new"),
                        (_PINNED_SEND_LAUNCH_ID, f"Send to {_PINNED_APP_DISPLAY_NAME.lower()}...", "/send"),
                        (_PINNED_DRAFT_LAUNCH_ID, f"Draft into {_PINNED_APP_DISPLAY_NAME.lower()}", "/draft"),
                    ),
                    launch_params={
                        _PINNED_NEW_LAUNCH_ID: [_PINNED_TEXT_PARAM],
                        _PINNED_SEND_LAUNCH_ID: [_PINNED_TEXT_PARAM],
                        _PINNED_DRAFT_LAUNCH_ID: [_PINNED_TEXT_PARAM],
                    },
                    launch_text_params={
                        _PINNED_NEW_LAUNCH_ID: _PINNED_TEXT_PARAM,
                        _PINNED_SEND_LAUNCH_ID: _PINNED_TEXT_PARAM,
                    },
                    launch_draft_params={_PINNED_DRAFT_LAUNCH_ID: _PINNED_TEXT_PARAM},
                    launch_methods={
                        _PINNED_NEW_LAUNCH_ID: "POST",
                        _PINNED_SEND_LAUNCH_ID: "POST",
                        _PINNED_DRAFT_LAUNCH_ID: "POST",
                    },
                )
            )
        write_registry(registry_path, *rows)
        monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", base_url)
        state_dir = tmp_path / "shell-state"
        config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
        repo_root = tmp_path / "repo"
        agent_events_path = tmp_path / "mngr-events" / "events.jsonl"
        agent_events_path.parent.mkdir()
        state = build_test_state(
            config=config,
            shell_state_directory=state_dir,
            repo_root=repo_root,
            agent_events_path=agent_events_path,
        )
        app = create_application(state)
        # Bound and started here, inside the stubs' contexts, so the shutdown below owns it whatever fails
        # first (a stub whose port is taken never leaves a bound shell socket behind).
        server = make_threaded_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for(
                lambda: is_server_answering(base_url),
                timeout=10.0,
                poll_interval=0.1,
                error_message=f"workspace server did not come up at {base_url}",
            )
            # Started only once the apps are serving: the first liveness probe must find them answering.
            state.shell.start()
            try:
                yield E2EServer(
                    base_url=base_url,
                    state_dir=state_dir,
                    repo_root=repo_root,
                    stub_url=stub_url,
                    pinned_url=pinned_served.http_url if pinned_served is not None else "",
                    agent_events_path=agent_events_path,
                )
            finally:
                state.shell.stop()
        finally:
            server.shutdown()
            thread.join(timeout=5.0)
            server.server_close()


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


def _stub_app(base_url: str) -> Flask:
    """The stub app: the stand-in page at every GET path, and at every POST path a launch that answers the page it
    opens, the root with the launch path and the posted params (the shell's envelope aside) as its query. Every
    launch posted is kept for the tests to read at ``/__launches``."""
    app = Flask("stub")
    launches: list[dict[str, Any]] = []

    def _page(path: str = "") -> Response:
        is_navigable = request.cookies.get(_PLAIN_PAGE_COOKIE) != _PLAIN_PAGE_COOKIE_VALUE
        return Response(_stub_page_html(base_url, is_navigable), mimetype="text/html")

    def _launch(path: str) -> Response:
        body = request.get_json()
        assert isinstance(body, dict), "a launch is posted as a JSON object"
        launches.append({"path": f"/{path}", "body": body})
        params = {name: value for name, value in body.items() if name not in RESERVED_LAUNCH_PARAM_NAMES}
        return jsonify({"path": _launched_page_path(path, params)})

    def _posted_launches() -> Response:
        return jsonify(launches)

    app.add_url_rule("/", view_func=_page, endpoint="stub_page_root", methods=["GET"])
    app.add_url_rule("/__launches", view_func=_posted_launches, endpoint="stub_launches", methods=["GET"])
    app.add_url_rule("/<path:path>", view_func=_page, endpoint="stub_page", methods=["GET"])
    app.add_url_rule("/<path:path>", view_func=_launch, endpoint="stub_launch", methods=["POST"])
    return app


def _launched_page_path(launch_path: str, params: dict[str, Any]) -> str:
    """The page a stub's POST launch path at ``/<launch_path>`` answers for ``params``."""
    return "/?" + urllib.parse.urlencode({_LAUNCHED_VIA_PARAM: launch_path, **params})


def _posted_launches(app_url: str) -> list[dict[str, Any]]:
    """Every launch the stub app at ``app_url`` was posted, as ``{"path", "body"}``, oldest first."""
    return list(_get_json(f"{app_url}/__launches"))


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
    answer: dict[str, Any] = {}

    def _attempt() -> bool:
        try:
            answer.update(_post_json(f"{base_url}/api/layout/broadcast", {"op": op, "args": args, "requester": None}))
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


def _land(page: Page, server: E2EServer, query: str = "", desktop_id: str = _HOME_DESKTOP_ID) -> None:
    """Open the shell and wait for the desktop's backdrop (home's unless said otherwise) and the seeded shortcut,
    which every desktop seeded from home carries too."""
    page.goto(f"{server.base_url}/{query}")
    expect(page.locator(f'[data-desktop-id="{desktop_id}"]')).to_be_visible(timeout=15000)
    expect(page.locator(f'[data-desktop-id="{desktop_id}"] [data-shortcut="{_STUB_SHORTCUT_KEY}"]')).to_be_visible(
        timeout=15000
    )


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

    def _opened() -> set[str]:
        return {window["id"] for window in _windows(server.base_url)} - before

    run(page.locator(f'[data-shortcut="{key}"]'))
    assert poll_until(lambda: len(_opened()) == 1, timeout=15.0, poll_interval=0.1), (
        f"the shortcut opened {len(_opened())} windows, not one"
    )
    (window_id,) = _opened()
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    return window_id


def _page_frame(page: Page, window_id: str) -> Frame:
    """The Playwright frame of the window's live page, once it has been greeted by the shell."""
    handle = page.locator(f'iframe[data-live-page="{window_id}"]').element_handle(timeout=15000)
    frame = handle.content_frame()
    assert frame is not None
    frame.wait_for_function("() => window.__handshake !== undefined", timeout=15000)
    return frame


def _box(locator: Locator) -> FloatRect:
    box = locator.bounding_box()
    assert box is not None, "the element has no box"
    return box


def _center(box: FloatRect) -> tuple[float, float]:
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


def _assert_same_box(actual: FloatRect, expected: FloatRect, what: str) -> None:
    for key in ("x", "y", "width", "height"):
        _assert_close(actual[key], expected[key], f"{what} {key}")


def _cell_center(backdrop: FloatRect, column: int, row: int) -> tuple[float, float]:
    return (
        backdrop["x"] + _GRID_INSET + column * _CELL_WIDTH + _CELL_WIDTH / 2,
        backdrop["y"] + _GRID_INSET + row * _CELL_HEIGHT + _CELL_HEIGHT / 2,
    )


def _open_launcher(page: Page) -> Locator:
    """Focus the launcher's field, answering the menu it opens."""
    page.locator("[data-launcher-field] textarea").click()
    menu = page.locator("[data-launcher-overlay]")
    expect(menu).to_be_visible(timeout=10000)
    return menu


def _own_window_path(base_url: str, client_id: str, window_id: str) -> str | None:
    """The client's own stored path for an independent window, off the layout route; None while it has none."""
    layout = _get_json(f"{base_url}/api/placements/{_HOME_DESKTOP_ID}?client={client_id}")
    return layout["window_paths"].get(window_id, {}).get("path")


def _wait_for_own_window_path(base_url: str, client_id: str, window_id: str, path: str) -> None:
    wait_for(
        lambda: _own_window_path(base_url, client_id, window_id) == path,
        timeout=15.0,
        poll_interval=0.1,
        error_message=f"the client's own path for {window_id} never became {path!r}",
    )


def _open_desktops_menu(page: Page) -> None:
    page.locator("[data-desktops-menu]").click()
    expect(page.locator(".desktops-menu")).to_be_visible(timeout=5000)


def _open_entry_menu(page: Page, entry: Locator) -> Locator:
    """Right-click a taskbar or floating entry, answering its open menu."""
    entry.click(button="right")
    menu = page.locator(".entry-menu")
    expect(menu).to_be_visible(timeout=5000)
    return menu


def _second_context(page: Page, **context_args: Any) -> BrowserContext:
    """A second browser context: its own storage, so its own client id."""
    browser = page.context.browser
    assert browser is not None
    return browser.new_context(**context_args)


@contextlib.contextmanager
def _second_client(
    page: Page, e2e_server: E2EServer, desktop_id: str = _HOME_DESKTOP_ID, **context_args: Any
) -> Generator[Page, None, None]:
    """A page of a second browser context (its own client id), landed on the shell (on ``desktop_id``) and closed
    with the context."""
    context = _second_context(page, **context_args)
    try:
        other_page = context.new_page()
        _land(other_page, e2e_server, desktop_id=desktop_id)
        yield other_page
    finally:
        context.close()


@pytest.mark.timeout(60, func_only=False)
def test_fresh_browser_lands_on_home_with_the_seeded_shortcut_and_registers_as_a_client(
    e2e_server: E2EServer, page: Page
) -> None:
    """A fresh browser lands on the home desktop over the bundled wallpaper: the seeded shortcut sits in the first
    cell, nothing is open, the taskbar carries the launcher field and the Desktops tray widget, and the shell soon
    knows the client with home as its active desktop."""
    _land(page, e2e_server)
    expect(page).to_have_title("System Interface")
    shortcut = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
    expect(shortcut).to_have_attribute("data-cell", "0,0")
    expect(shortcut.locator(".shortcut-label")).to_have_text(_STUB_APP_DISPLAY_NAME)
    expect(_shown_windows(page)).to_have_count(0)
    expect(page.locator("[data-taskbar] [data-launcher-field]")).to_be_visible()
    expect(page.locator('[data-tray-widget="desktops"] [data-desktop-switch]')).to_have_count(1)
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
    expect(page.locator(".shortcut-menu")).to_be_visible(timeout=5000)
    page.locator('[data-menu-row="open-new"]').click()
    windows = _wait_for_window_count(e2e_server.base_url, 2)
    assert {window["path"] for window in windows} == {_STUB_LAUNCH_PATH}
    expect(_shown_windows(page)).to_have_count(2, timeout=15000)


@pytest.mark.timeout(60, func_only=False)
def test_launcher_menu_lists_launch_paths_and_windows_and_runs_the_highlight(tmp_path: Path, page: Page) -> None:
    """The launcher opens from its field as a menu: one row per launch path with the first highlighted; the arrows
    move the highlight and Enter runs it; typing narrows the rows to matches and adds the windows across desktops,
    a window row brings its window forward, and with no match the menu says so; Escape clears, then closes."""
    with _running_e2e_server(tmp_path, is_second_app_offered=True) as server:
        _land(page, server)
        menu = _open_launcher(page)
        docs_row = menu.locator(f'[data-launch="{_STUB_SHORTCUT_KEY}"]')
        notes_row = menu.locator(f'[data-launch="{_SECOND_SHORTCUT_KEY}"]')
        expect(docs_row).to_have_attribute("data-highlighted", "true")
        expect(notes_row).to_have_attribute("data-highlighted", "false")
        expect(menu.locator("[data-launcher-window]")).to_have_count(0)
        # No app takes typed text, so there is no free-text row and no key binding for one.
        expect(menu.locator("[data-text-action]")).to_have_count(0)

        page.keyboard.press("ArrowDown")
        expect(notes_row).to_have_attribute("data-highlighted", "true")
        page.keyboard.press("Enter")
        (window,) = _wait_for_window_count(server.base_url, 1)
        assert window["app"] == _SECOND_APP_NAME and window["path"] == _STUB_LAUNCH_PATH
        expect(_window(page, window["id"])).to_be_visible(timeout=15000)
        expect(menu).to_be_hidden()
        expect(page.locator("[data-launcher-field] textarea")).to_have_value("")

        _window(page, window["id"]).locator('[data-window-control="minimize"]').click()
        expect(_taskbar_entry(page, window["id"])).to_have_attribute("data-minimized", "true")
        menu = _open_launcher(page)
        expect(menu.locator("[data-launcher-window]")).to_have_count(0)
        page.locator("[data-launcher-field] textarea").fill("notes")
        expect(menu.locator("[data-launch]")).to_have_count(1)
        expect(menu.locator(f'[data-launcher-window="{window["id"]}"]')).to_have_attribute("data-minimized", "true")
        menu.locator(f'[data-launcher-window="{window["id"]}"]').click()
        expect(menu).to_be_hidden()
        expect(_window(page, window["id"])).to_be_visible(timeout=10000)
        expect(_window(page, window["id"])).to_have_attribute("data-focused", "true")

        menu = _open_launcher(page)
        page.locator("[data-launcher-field] textarea").fill("zzzz")
        expect(menu.locator(".launcher-no-matches")).to_be_visible()
        expect(menu.locator("[data-launch]")).to_have_count(0)
        # One Escape clears the text; the next, on an empty field, closes the menu.
        page.keyboard.press("Escape")
        expect(page.locator("[data-launcher-field] textarea")).to_have_value("")
        expect(menu.locator(".launcher-no-matches")).to_have_count(0)
        expect(menu.locator(f'[data-launch="{_STUB_SHORTCUT_KEY}"]')).to_have_attribute("data-highlighted", "true")
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()


@pytest.mark.timeout(90, func_only=False)
def test_launcher_free_text_rows_point_the_pinned_window_at_the_text(tmp_path: Path, page: Page) -> None:
    """The launch paths that take typed text are the menu's free-text rows: Enter with no match runs the primary
    one, Ctrl+Enter the secondary, each posting the launch path with the text (and this client's id and the
    window's path, the shell's envelope) and pointing this client's view of the app's independent pinned window at
    the page the app answers (no second window opens) and showing it; the row at the pin's home path is a focus row
    that raises the pinned window; an empty field offers no secondary row; and Shift+Enter breaks the line, after
    which the menu offers the free-text rows alone and the text goes with its line break."""
    with _running_e2e_server(tmp_path, pin=("plain", "independent", "bar")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        client_id = _client_id(page)
        menu = _open_launcher(page)
        primary = menu.locator('[data-text-action="primary"]')
        secondary = menu.locator('[data-text-action="secondary"]')
        expect(primary).to_have_attribute("data-launch", f"{_PINNED_APP_NAME}:{_PINNED_NEW_LAUNCH_ID}")
        expect(secondary).to_have_count(0)
        # The first launch-path row is highlighted (the stub app's, in registry order) and wears the Enter caption;
        # the pinned app's home-path row is a launch-path row too, and its free-text rows are never launch-path rows.
        stub_row = menu.locator(f'[data-launch="{_STUB_SHORTCUT_KEY}"]')
        expect(stub_row).to_have_attribute("data-highlighted", "true")
        expect(stub_row.locator('[data-key="enter"]')).to_have_text("Enter")
        expect(menu.locator(f'[data-launch="{_PINNED_APP_NAME}:root"]')).to_have_attribute("data-highlighted", "false")
        expect(menu.locator("[data-launch]")).to_have_count(3)

        page.locator("[data-launcher-field] textarea").fill("hello there")
        expect(menu.locator(".launcher-no-matches")).to_be_visible()
        expect(primary).to_have_attribute("data-highlighted", "true")
        expect(primary.locator('[data-key="enter"]')).to_have_text("Enter")
        expect(stub_row).to_have_count(0)
        expect(secondary).to_have_attribute("data-launch", f"{_PINNED_APP_NAME}:{_PINNED_SEND_LAUNCH_ID}")
        expect(secondary).not_to_have_attribute("data-disabled", "true")
        page.keyboard.press("Enter")
        expect(menu).to_be_hidden()
        new_page_path = _launched_page_path(_PINNED_NEW_LAUNCH_ID, {_PINNED_TEXT_PARAM: "hello there"})
        _wait_for_own_window_path(server.base_url, client_id, pinned["id"], new_page_path)
        expect(_window(page, pinned["id"])).to_be_visible(timeout=15000)
        frame = _page_frame(page, pinned["id"])
        expect(frame.locator("#where")).to_have_text(new_page_path, timeout=15000)
        # The text went in the post's body with the shell's envelope.
        (posted,) = _posted_launches(server.pinned_url)
        assert posted["path"] == f"/{_PINNED_NEW_LAUNCH_ID}"
        assert posted["body"] == {
            _PINNED_TEXT_PARAM: "hello there",
            "client_id": client_id,
            "desktop_id": _HOME_DESKTOP_ID,
            "window_path": _PINNED_HOME_PATH,
        }
        # The shared record keeps the home path, and nothing else opened.
        page.wait_for_timeout(_NEGATIVE_SETTLE_MS)
        assert _pinned_window(server.base_url)["path"] == _PINNED_HOME_PATH
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]

        menu = _open_launcher(page)
        field = page.locator("[data-launcher-field] textarea")
        field.fill("again")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("and more")
        expect(field).to_have_value("again\nand more")
        # A text with a line break is a message: the free-text rows (the draft row among them) stand alone,
        # without the no-match note.
        expect(menu.locator("[data-launch]")).to_have_count(3)
        expect(menu.locator(".launcher-no-matches")).to_have_count(0)
        page.keyboard.press("Control+Enter")
        send_page_path = _launched_page_path(_PINNED_SEND_LAUNCH_ID, {_PINNED_TEXT_PARAM: "again\nand more"})
        _wait_for_own_window_path(server.base_url, client_id, pinned["id"], send_page_path)
        expect(frame.locator("#where")).to_have_text(send_page_path, timeout=15000)
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]
        # The window's path in the envelope is where this client's view stood when the text was sent.
        assert [(launch["path"], launch["body"]["window_path"]) for launch in _posted_launches(server.pinned_url)] == [
            (f"/{_PINNED_NEW_LAUNCH_ID}", _PINNED_HOME_PATH),
            (f"/{_PINNED_SEND_LAUNCH_ID}", new_page_path),
        ]

        # The focus row raises the pinned window rather than opening a second root.
        _window(page, pinned["id"]).locator('[data-window-control="minimize"]').click()
        expect(_taskbar_entry(page, pinned["id"])).to_have_attribute("data-minimized", "true")
        menu = _open_launcher(page)
        menu.locator(f'[data-launch="{_PINNED_APP_NAME}:root"]').click()
        expect(_window(page, pinned["id"])).to_be_visible(timeout=10000)
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]


@pytest.mark.timeout(60, func_only=False)
def test_the_launcher_field_grows_upward_out_of_its_row_and_lifts_the_menu(e2e_server: E2EServer, page: Page) -> None:
    """The field is one row in the taskbar until its text has lines (plan section 4.1): then it grows upward out of
    its one-row footprint over the backdrop, the taskbar and its entries hold their places, and the menu's foot
    rises with the field (section 4.2); a cleared field is one row again and the menu comes back down."""
    _land(page, e2e_server)
    menu = _open_launcher(page)
    taskbar_box = _box(page.locator("[data-taskbar]"))
    entries_box = _box(page.locator("[data-taskbar-entries]"))
    field = page.locator("[data-launcher-field]")
    one_row_box = _box(field)
    _assert_same_box(one_row_box, _box(page.locator(".launcher-field-slot")), "the one-row field in its slot")
    menu_box = _box(menu)
    _assert_close(menu_box["y"] + menu_box["height"], taskbar_box["y"], "the menu's foot over a one-row field")

    page.locator("[data-launcher-field] textarea").fill("one\ntwo\nthree\nfour")
    wait_for(
        lambda: _box(field)["y"] < taskbar_box["y"],
        timeout=10.0,
        poll_interval=0.1,
        error_message="the field never grew above the taskbar",
    )
    grown_box = _box(field)
    rise = grown_box["height"] - one_row_box["height"]
    assert rise > 0, (grown_box, one_row_box)
    # Grown upward out of its footprint: the foot holds, the top rises over the backdrop, and the taskbar and its
    # entries stay where they were.
    _assert_close(
        grown_box["y"] + grown_box["height"], one_row_box["y"] + one_row_box["height"], "the grown field's foot"
    )
    _assert_same_box(_box(page.locator("[data-taskbar]")), taskbar_box, "the taskbar under a grown field")
    _assert_same_box(_box(page.locator("[data-taskbar-entries]")), entries_box, "the entries beside a grown field")
    menu_box = _box(menu)
    _assert_close(menu_box["y"] + menu_box["height"], taskbar_box["y"] - rise, "the menu's foot over a grown field")

    # Escape clears the text: one row again, and the menu comes back down.
    page.keyboard.press("Escape")
    expect(page.locator("[data-launcher-field] textarea")).to_have_value("")
    wait_for(
        lambda: abs(_box(field)["height"] - one_row_box["height"]) <= _GEOMETRY_TOLERANCE_PX,
        timeout=10.0,
        poll_interval=0.1,
        error_message="the field never shrank back to one row",
    )
    _assert_same_box(_box(field), one_row_box, "the field cleared")
    menu_box = _box(menu)
    _assert_close(menu_box["y"] + menu_box["height"], taskbar_box["y"], "the menu's foot over a cleared field")


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
    # A reload would drop this; the window has to arrive in the page as it stands.
    page.evaluate("() => { window.__e2eSamePage = true; }")
    answer = _broadcast_op(
        e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/?doc=7", "client": client_id}
    )
    window_id = answer["window_id"]
    assert answer["desktop_id"] == _HOME_DESKTOP_ID
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    expect(_window(page, window_id)).to_have_attribute("data-focused", "true")
    assert page.evaluate("() => window.__e2eSamePage === true"), "the shell reloaded to show the window"
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
    # The page is back too, laid over the restored window, whatever order the loads landed in.
    expect(page.locator(f'iframe[data-live-page="{window_id}"]')).to_be_visible(timeout=15000)
    assert _page_frame(page, window_id).url == f"{e2e_server.stub_url}{_STUB_LAUNCH_PATH}"


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
    page.locator('[data-menu-row="open-new"]').click()
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
def test_shortcut_menu_changes_mode_and_removes(e2e_server: E2EServer, page: Page) -> None:
    """The shortcut's menu flips its mode and removes it."""
    _land(page, e2e_server)
    shortcut = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
    shortcut.click(button="right")
    page.locator('[data-menu-row="change-mode"]').click()
    wait_for(
        lambda: _desktop(e2e_server.base_url)["shortcuts"][0]["mode"] == "new",
        timeout=10.0,
        poll_interval=0.1,
        error_message="the mode never flipped",
    )
    expect(shortcut.locator(".shortcut-label")).to_have_text(_STUB_APP_DISPLAY_NAME)

    shortcut.click(button="right")
    page.locator('[data-menu-row="remove"]').click()
    expect(shortcut).to_have_count(0, timeout=10000)
    wait_for(
        lambda: _desktop(e2e_server.base_url)["shortcuts"] == [],
        timeout=10.0,
        poll_interval=0.1,
        error_message="the shortcut stayed in the desktop record",
    )


@pytest.mark.timeout(90, func_only=False)
def test_desktop_create_settings_switch_and_delete_through_the_tray(e2e_server: E2EServer, page: Page) -> None:
    """The Desktops widget's menu creates a desktop (the client switches to it, with the seeded shortcut), its
    settings dialog renames it, the widget switches back and forth (each desktop keeps its own windows), and the
    delete confirmation removes it, moving the client home."""
    _land(page, e2e_server)
    home_window = _open_via_shortcut(page, e2e_server)

    _open_desktops_menu(page)
    page.locator('[data-menu-row="new-desktop"]').click()
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
    page.locator('[data-menu-row="settings"]').click()
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
    page.locator('[data-menu-row="delete"]').click()
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


# Pinned windows (pinned-taskbar-entries plan sections 3.2, 4.1, 4.3, 4.4)


def _pinned_window(base_url: str, desktop_id: str = _HOME_DESKTOP_ID) -> dict[str, Any]:
    """The pinned stub's window on the desktop, off the API; the desktop holds exactly one."""
    (pinned,) = [window for window in _windows(base_url, desktop_id) if window["app"] == _PINNED_APP_NAME]
    assert pinned["is_pinned"] is True
    return pinned


def _pinned_entry(page: Page) -> Locator:
    return page.locator(f'[data-pinned-entry="{_PINNED_APP_NAME}"]')


@pytest.mark.timeout(90, func_only=False)
def test_a_pinned_app_has_one_window_on_every_desktop_whose_entry_restores_minimizes_and_never_closes(
    tmp_path: Path, page: Page
) -> None:
    """A pinned app's window is on the home desktop from the first read and on a desktop created later; its
    taskbar entry is there with nothing open, a click restores the window at the pinned frame, another
    minimizes it; the window's close control and both menus' Close minimize it rather than closing it, as does the
    close chord, and an agent's close is refused."""
    with _running_e2e_server(tmp_path, pin=("plain", "linked", "bar")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        assert pinned["path"] == _PINNED_HOME_PATH
        assert pinned["scope"] == "linked"
        entry = _taskbar_entry(page, pinned["id"])
        expect(entry).to_be_visible(timeout=15000)
        expect(entry).to_have_attribute("data-pinned", "true")
        expect(entry).to_have_attribute("data-minimized", "true")
        expect(_shown_windows(page)).to_have_count(0)
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]

        entry.click()
        window = _window(page, pinned["id"])
        expect(window).to_be_visible(timeout=15000)
        expect(window).to_have_attribute("data-pinned", "true")
        # The first restore lands at the pinned frame: the right half of the backdrop, a margin in.
        backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))
        shown = _box(window)
        _assert_close(shown["x"], backdrop["x"] + 0.46 * backdrop["width"], "pinned frame x")
        _assert_close(shown["width"], 0.5 * backdrop["width"], "pinned frame width")
        _assert_close(shown["height"], 0.9 * backdrop["height"], "pinned frame height")
        expect(window.locator('[data-window-control="minimize"]')).to_be_visible()
        assert _page_frame(page, pinned["id"]).url == f"{server.pinned_url}{_PINNED_HOME_PATH}"
        # The close control stays, out of habit's way: on a pinned window it minimizes.
        window.locator('[data-window-control="close"]').click()
        expect(_shown_windows(page)).to_have_count(0)
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]
        entry.click()
        expect(window).to_be_visible(timeout=15000)
        window.locator('[data-window-control="menu"]').click()
        expect(page.locator(".window-menu")).to_be_visible(timeout=5000)
        page.locator('.window-menu [data-menu-row="close"]').click()
        expect(_shown_windows(page)).to_have_count(0)
        entry.click()
        expect(window).to_be_visible(timeout=15000)
        _open_entry_menu(page, entry).locator('[data-menu-row="close"]').click()
        expect(_shown_windows(page)).to_have_count(0)
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]
        entry.click()
        expect(window).to_be_visible(timeout=15000)

        # The close chord minimizes the pinned window rather than closing it; the entry click brings it back.
        page.evaluate("() => window.postMessage({ type: 'minds:close-active-tab' }, '*')")
        expect(entry).to_have_attribute("data-minimized", "true", timeout=15000)
        _assert_no_further_window(page, server, [pinned["id"]])
        entry.click()
        expect(window).to_be_visible(timeout=15000)

        # An agent's close is refused with the fix, and the window stays.
        client_id = _client_id(page)
        payload = json.dumps(
            {"op": "close", "args": {"window": pinned["id"], "client": client_id}, "requester": None}
        ).encode()
        request = urllib.request.Request(
            f"{server.base_url}/api/layout/broadcast",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 400
        assert "minimize" in refused.value.read().decode()
        assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]

        # A new desktop is born with its own pinned window, and an open at the home path finds it.
        _open_desktops_menu(page)
        page.locator('[data-menu-row="new-desktop"]').click()
        wait_for(
            lambda: len(_desktops(server.base_url)) == 2,
            timeout=10.0,
            poll_interval=0.1,
            error_message="no second desktop was created",
        )
        (created,) = [desktop for desktop in _desktops(server.base_url) if desktop["id"] != _HOME_DESKTOP_ID]
        born = _pinned_window(server.base_url, created["id"])
        assert born["id"] != pinned["id"]
        expect(_taskbar_entry(page, born["id"])).to_have_attribute("data-pinned", "true", timeout=15000)
        answer = _broadcast_op(
            server.base_url,
            "open",
            {"app": _PINNED_APP_NAME, "path": _PINNED_HOME_PATH, "client": client_id, "desktop": created["id"]},
        )
        assert answer["window_id"] == born["id"]
        _assert_no_further_window(page, server, [pinned["id"]])
        assert [window["id"] for window in _windows(server.base_url, created["id"])] == [born["id"]]


@pytest.mark.timeout(90, func_only=False)
def test_an_independent_pinned_window_keeps_a_path_per_client_and_an_agent_navigates_one_client(
    tmp_path: Path, page: Page
) -> None:
    """Two clients restore the same independent pinned window: each page moves on its own and reports its own
    path, the shared record keeps the home path, neither client follows the other, and an agent's ``navigate``
    for one client moves that client's page alone."""
    with _running_e2e_server(tmp_path, pin=("plain", "independent", "bar")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        assert pinned["scope"] == "independent"
        with _second_client(page, server) as other_page:
            for client_page in (page, other_page):
                _taskbar_entry(client_page, pinned["id"]).click()
                expect(_window(client_page, pinned["id"])).to_be_visible(timeout=15000)
            frame = _page_frame(page, pinned["id"])
            other_frame = _page_frame(other_page, pinned["id"])
            client_id = _client_id(page)
            other_client_id = _client_id(other_page)

            frame.evaluate("() => window.__navigateTo('/?doc=1')")
            other_frame.evaluate("() => window.__navigateTo('/?doc=2')")
            wait_for(
                lambda: _own_window_path(server.base_url, client_id, pinned["id"]) == "/?doc=1"
                and _own_window_path(server.base_url, other_client_id, pinned["id"]) == "/?doc=2",
                timeout=15.0,
                poll_interval=0.1,
                error_message="the two clients' own paths never reached their window path files",
            )
            page.wait_for_timeout(_NEGATIVE_SETTLE_MS)
            assert _pinned_window(server.base_url)["path"] == _PINNED_HOME_PATH
            assert _pinned_window(server.base_url)["title"] == ""
            expect(frame.locator("#where")).to_have_text("/?doc=1")
            expect(other_frame.locator("#where")).to_have_text("/?doc=2")
            assert frame.evaluate("() => window.__navigations") == []
            assert other_frame.evaluate("() => window.__navigations") == []
            expect(_window(page, pinned["id"]).locator(".window-title")).to_have_text("Stub /?doc=1", timeout=15000)
            expect(_window(other_page, pinned["id"]).locator(".window-title")).to_have_text("Stub /?doc=2")
            expect(_taskbar_entry(page, pinned["id"])).to_contain_text("Stub /?doc=1")

            answer = _broadcast_op(
                server.base_url, "navigate", {"window": pinned["id"], "path": "/?doc=3", "client": client_id}
            )
            assert answer["layout"]["window_paths"][pinned["id"]]["path"] == "/?doc=3"
            expect(frame.locator("#where")).to_have_text("/?doc=3", timeout=15000)
            assert frame.evaluate("() => window.__navigations") == ["/?doc=3"]
            page.wait_for_timeout(_NEGATIVE_SETTLE_MS)
            expect(other_frame.locator("#where")).to_have_text("/?doc=2")
            assert other_frame.evaluate("() => window.__navigations") == []
            assert _pinned_window(server.base_url)["path"] == _PINNED_HOME_PATH

            # A reload of the first client lands its page at its own path again.
            page.reload()
            expect(_window(page, pinned["id"])).to_be_visible(timeout=15000)
            assert _page_frame(page, pinned["id"]).url == f"{server.pinned_url}/?doc=3"


def _client_entries(base_url: str, client_id: str) -> dict[str, Any]:
    (record,) = [client for client in _get_json(f"{base_url}/api/clients")["clients"] if client["id"] == client_id]
    return record["entries"]


def _wait_for_client_entry(
    base_url: str, client_id: str, is_expected: Callable[[dict[str, Any]], bool], what: str
) -> dict[str, Any]:
    """Poll the client's presentation of the pinned entry until ``is_expected`` holds of it, answering it."""
    wait_for(
        lambda: is_expected(_client_entries(base_url, client_id).get(_PINNED_APP_NAME, {})),
        timeout=10.0,
        poll_interval=0.1,
        error_message=what,
    )
    return _client_entries(base_url, client_id)[_PINNED_APP_NAME]


@pytest.mark.timeout(90, func_only=False)
def test_a_floating_entry_toggles_its_window_drags_to_a_position_that_survives_a_reload_and_returns_to_the_bar(
    tmp_path: Path, page: Page
) -> None:
    """A pin whose default mode is floating draws its entry above the windows: a click restores the window and
    another minimizes it, a drag moves the entry and writes the position once to the client record so a reload
    puts it back, and its menu moves it into the taskbar (and out again)."""
    with _running_e2e_server(tmp_path, pin=("plain", "linked", "floating")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        client_id = _client_id(page)
        entry = _pinned_entry(page)
        expect(entry).to_be_visible(timeout=15000)
        expect(entry).to_have_attribute("data-entry-mode", "floating")
        expect(entry).to_have_attribute("data-entry-style", "plain")
        expect(_taskbar_entry(page, pinned["id"])).to_have_count(0)
        expect(page.locator("[data-floating-entries]")).to_have_count(1)

        entry.click()
        expect(_window(page, pinned["id"])).to_be_visible(timeout=15000)
        expect(entry).to_have_attribute("aria-pressed", "true")
        entry.click()
        expect(_shown_windows(page)).to_have_count(0)
        expect(entry).to_have_attribute("data-minimized", "true")

        backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))
        before = _box(entry)
        # The default corner: bottom right of the backdrop, inset by the theme's tokens.
        _assert_close(before["x"] + before["width"], backdrop["x"] + backdrop["width"] - 16, "default x")
        _assert_close(before["y"] + before["height"], backdrop["y"] + backdrop["height"] - 12, "default y")
        _drag(page, _center(before), (_center(before)[0] - 300, _center(before)[1] - 200))
        moved = _box(entry)
        _assert_close(moved["x"], before["x"] - 300, "dragged x")
        _assert_close(moved["y"], before["y"] - 200, "dragged y")
        stored = _wait_for_client_entry(
            server.base_url,
            client_id,
            lambda entry: entry.get("position") is not None,
            "the drag never wrote the position",
        )
        assert stored["mode"] == "floating"
        assert stored["style"] == "plain"
        _assert_close(stored["position"]["x"] * backdrop["width"], moved["x"] - backdrop["x"], "stored x")
        _assert_close(stored["position"]["y"] * backdrop["height"], moved["y"] - backdrop["y"], "stored y")
        # The drag fired no click: the window stayed minimized.
        expect(_shown_windows(page)).to_have_count(0)

        page.reload()
        expect(_pinned_entry(page)).to_be_visible(timeout=15000)
        _assert_same_box(_box(_pinned_entry(page)), moved, "after reload")

        menu = _open_entry_menu(page, _pinned_entry(page))
        expect(menu.locator('[data-menu-row="close"]')).to_have_count(1)
        menu.locator('[data-menu-row="move-to-taskbar"]').click()
        expect(_taskbar_entry(page, pinned["id"])).to_be_visible(timeout=10000)
        expect(_taskbar_entry(page, pinned["id"])).to_have_attribute("data-entry-mode", "bar")
        expect(page.locator("[data-floating-entries] [data-pinned-entry]")).to_have_count(0)
        _wait_for_client_entry(
            server.base_url,
            client_id,
            lambda entry: entry.get("mode") == "bar",
            "the mode never reached the client record",
        )
        _open_entry_menu(page, _taskbar_entry(page, pinned["id"])).locator('[data-menu-row="float"]').click()
        expect(page.locator("[data-floating-entries] [data-pinned-entry]")).to_have_count(1, timeout=10000)
        # The position it was dragged to is kept across the trip through the bar.
        _assert_same_box(_box(_pinned_entry(page)), moved, "back afloat")


def _avatar_image_source(entry: Locator) -> str:
    return str(entry.locator("img").get_attribute("src"))


def _write_agent_events(path: Path, state: str) -> None:
    """One ``AGENT_STATE`` line stamped now, for an agent in ``state``, beside the services agent (always running)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
    lines = [
        {
            "timestamp": now,
            "type": "AGENT_STATE",
            "agent": {"id": "services", "state": "RUNNING", "labels": {"is_primary": "true"}},
        },
        {"timestamp": now, "type": "AGENT_STATE", "agent": {"id": "worker", "state": state, "labels": {}}},
    ]
    with path.open("a") as stream:
        stream.writelines(json.dumps(line) + "\n" for line in lines)


@pytest.mark.timeout(90, func_only=False)
def test_the_avatar_wears_the_mood_of_the_agents_file_and_the_chooser_changes_every_window(
    tmp_path: Path, page: Page
) -> None:
    """An ``avatar`` pin draws the workspace's design wearing the mood the agents event file folds to (stale and
    idle until the file exists, working once an agent runs), its menu's style rows swap the image for the app's
    icon and back, "Change avatar..." opens the chooser, choosing a design changes every open window, and
    "Design your own..." drafts the design prompt into this client's pinned window, which declares a launch path that
    takes a draft, rather than opening a chat."""
    with _running_e2e_server(tmp_path, pin=("avatar", "independent", "floating")) as server:
        _land(page, server)
        entry = _pinned_entry(page)
        expect(entry).to_be_visible(timeout=15000)
        expect(entry).to_have_attribute("data-entry-style", "avatar")
        expect(entry).to_have_attribute("data-mood", "idle")
        expect(entry).to_have_attribute("data-stale", "true")
        assert _avatar_image_source(entry).endswith("/api/avatars/gummy-seal/image.svg?mood=idle")

        _write_agent_events(server.agent_events_path, "RUNNING")
        expect(entry).to_have_attribute("data-mood", "working", timeout=15000)
        expect(entry).to_have_attribute("data-stale", "false")
        assert _avatar_image_source(entry).endswith("/api/avatars/gummy-seal/image.svg?mood=working")
        _write_agent_events(server.agent_events_path, "STOPPED")
        expect(entry).to_have_attribute("data-mood", "idle", timeout=15000)

        # The plain style shows the app's icon in place of the avatar; the pin's style brings the image back.
        _open_entry_menu(page, entry).locator('[data-menu-row="style-plain"]').click()
        expect(entry).to_have_attribute("data-entry-style", "plain", timeout=10000)
        expect(entry.locator("svg")).to_have_count(1)
        expect(entry.locator("img")).to_have_count(0)
        _wait_for_client_entry(
            server.base_url,
            _client_id(page),
            lambda entry: entry.get("style") == "plain",
            "the style never reached the client record",
        )
        _open_entry_menu(page, entry).locator('[data-menu-row="style-avatar"]').click()
        expect(entry).to_have_attribute("data-entry-style", "avatar", timeout=10000)
        expect(entry.locator("img")).to_have_count(1)

        with _second_client(page, server) as other:
            other_entry = _pinned_entry(other)
            expect(other_entry).to_be_visible(timeout=15000)
            # The pinned window is shown first, so the draft below has a live page to move rather than a page to
            # create at the answered path: the case a user with the chat open is in.
            entry.click()
            expect(_window(page, _pinned_window(server.base_url)["id"])).to_be_visible(timeout=15000)
            _open_entry_menu(page, entry).locator('[data-menu-row="change-avatar"]').click()
            chooser = page.locator("[data-avatar-chooser]")
            expect(chooser).to_be_visible(timeout=5000)
            expect(chooser.locator('[data-avatar-design="gummy-seal"]')).to_have_attribute("aria-pressed", "true")
            chooser.locator('[data-avatar-design="jelly-cat"]').click()
            expect(chooser.locator('[data-avatar-design="jelly-cat"]')).to_have_attribute(
                "aria-pressed", "true", timeout=10000
            )
            assert _get_json(f"{server.base_url}/api/avatars")["selected"] == "jelly-cat"
            wait_for(
                lambda: _avatar_image_source(entry).endswith("/api/avatars/jelly-cat/image.svg?mood=idle")
                and _avatar_image_source(other_entry).endswith("/api/avatars/jelly-cat/image.svg?mood=idle"),
                timeout=10.0,
                poll_interval=0.1,
                error_message="the windows never drew the chosen design",
            )
            # "Design your own..." drafts into this client's pinned window rather than opening a chat: the app's
            # draft launch path is posted the prompt with this client's view of the window as the envelope's
            # ``window_path``, the window is pointed at the page it answers and shown, and no window is opened.
            chooser.locator(".avatar-design-own").click()
            expect(chooser).to_have_count(0)
            pinned_id = _pinned_window(server.base_url)["id"]
            client_id = _client_id(page)
            drafted_prefix = _launched_page_path(_PINNED_DRAFT_LAUNCH_ID, {}) + f"&{_PINNED_TEXT_PARAM}="

            def _drafted_path() -> str:
                return _own_window_path(server.base_url, client_id, pinned_id) or ""

            wait_for(
                lambda: _drafted_path().startswith(drafted_prefix),
                timeout=10.0,
                poll_interval=0.1,
                error_message="this client's view of the pinned window was never pointed at the draft's page",
            )
            drafted = _drafted_path()
            (draft,) = urllib.parse.parse_qs(urllib.parse.urlsplit(drafted).query)[_PINNED_TEXT_PARAM]
            assert "design my own desktop avatar" in draft
            (posted,) = _posted_launches(server.pinned_url)
            assert posted["path"] == f"/{_PINNED_DRAFT_LAUNCH_ID}"
            assert posted["body"] == {
                _PINNED_TEXT_PARAM: draft,
                "client_id": client_id,
                "desktop_id": _HOME_DESKTOP_ID,
                "window_path": _PINNED_HOME_PATH,
            }
            expect(_window(page, pinned_id)).to_be_visible(timeout=15000)
            # The page itself is moved there (the following step, as for an agent's navigate), and only here: the
            # shared record keeps the home path, the other client's view is untouched, and no window is opened.
            wait_for(
                lambda: drafted in _page_frame(page, pinned_id).evaluate("() => window.__navigations"),
                timeout=10.0,
                poll_interval=0.1,
                error_message="the pinned window's page was never navigated to the draft's page",
            )
            assert _pinned_window(server.base_url)["path"] == _PINNED_HOME_PATH
            assert [window["app"] for window in _windows(server.base_url)] == [_PINNED_APP_NAME]
            # The page takes the draft and reports its selection alone, as the chat root does; a second identical
            # draft must reach it again rather than being mistaken for a stale snapshot of the path it left.
            _page_frame(page, pinned_id).evaluate("() => window.__navigateTo('/?doc=1')")
            wait_for(
                lambda: _drafted_path() == "/?doc=1",
                timeout=10.0,
                poll_interval=0.1,
                error_message="the page's own report never replaced the draft's page path",
            )
            _open_entry_menu(page, entry).locator('[data-menu-row="change-avatar"]').click()
            expect(chooser).to_be_visible(timeout=5000)
            chooser.locator(".avatar-design-own").click()
            wait_for(
                lambda: _page_frame(page, pinned_id).evaluate("() => window.__navigations").count(drafted) == 2,
                timeout=10.0,
                poll_interval=0.1,
                error_message="the second draft never reached the page",
            )


@pytest.mark.timeout(90, func_only=False)
def test_a_phone_shows_a_floating_entry_in_the_bar_without_rewriting_its_mode(tmp_path: Path, page: Page) -> None:
    """Compact mode renders every pinned entry in the bar whatever its mode says, and offers no float verb; the
    client record's mode is untouched, so a laptop still draws it floating."""
    with _running_e2e_server(tmp_path, pin=("plain", "linked", "floating")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        expect(_pinned_entry(page)).to_have_attribute("data-entry-mode", "floating", timeout=15000)
        with _second_client(page, server, **_MOBILE_CONTEXT_ARGS) as phone_page:
            phone_entry = _taskbar_entry(phone_page, pinned["id"])
            expect(phone_entry).to_be_visible(timeout=15000)
            expect(phone_entry).to_have_attribute("data-entry-mode", "bar")
            expect(phone_page.locator("[data-floating-entries] [data-pinned-entry]")).to_have_count(0)
            phone_entry.tap()
            expect(_window(phone_page, pinned["id"])).to_have_attribute(
                "data-window-state", "MAXIMIZED", timeout=15000
            )
            phone_entry.tap()
            expect(_shown_windows(phone_page)).to_have_count(0)
            # A long press (a touch press held still) opens the entry's menu, which offers no Float (its Close minimizes)
            # on a phone.
            phone_entry.dispatch_event(
                "pointerdown", {"pointerType": "touch", "button": 0, "buttons": 1, "pointerId": 3, "bubbles": True}
            )
            expect(phone_page.locator(".entry-menu")).to_be_visible(timeout=5000)
            expect(phone_page.locator('.entry-menu [data-menu-row="float"]')).to_have_count(0)
            expect(phone_page.locator('.entry-menu [data-menu-row="close"]')).to_have_count(1)
            phone_page.keyboard.press("Escape")
            assert _client_entries(server.base_url, _client_id(phone_page)) == {}
        expect(_pinned_entry(page)).to_have_attribute("data-entry-mode", "floating")


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
        # The menu spans the taskbar on a phone; a window row appears once its title is typed.
        menu_box = _box(phone_page.locator("[data-launcher-overlay]"))
        taskbar_box = _box(phone_page.locator("[data-taskbar]"))
        assert abs(menu_box["width"] - taskbar_box["width"]) <= _GRID_INSET, (menu_box, taskbar_box)
        # The expanded field keeps the collapsed button's place, centred in the taskbar, over the entries.
        field_box = _box(phone_page.locator("[data-launcher-field]"))
        assert abs(_center(field_box)[1] - _center(taskbar_box)[1]) <= 2, (field_box, taskbar_box)
        phone_page.locator("[data-launcher-field] textarea").fill("stub")
        expect(phone_page.locator(f'[data-launcher-overlay] [data-launcher-window="{window_id}"]')).to_be_visible()
        phone_page.keyboard.press("Escape")
        expect(phone_page.locator("[data-launcher-field] textarea")).to_have_value("")
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
    launcher row is a window on the laptop too, minimized there in turn."""
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
        overlay.locator(f'[data-launch="{_STUB_SHORTCUT_KEY}"]').tap()
        windows = _wait_for_window_count(e2e_server.base_url, 2)
        (phone_window,) = [window["id"] for window in windows if window["id"] != laptop_window]
        expect(_window(phone_page, phone_window)).to_have_attribute("data-window-state", "MAXIMIZED", timeout=15000)
        expect(_taskbar_entry(page, phone_window)).to_have_attribute("data-minimized", "true", timeout=15000)
        expect(_window(page, laptop_window)).to_have_attribute("data-focused", "true")


def _visiting_client(
    page: Page, e2e_server: E2EServer, user_id: str, email_local_part: str, desktop_id: str
) -> contextlib.AbstractContextManager[Page]:
    """A second client whose every request carries a visitor's identity, landed on the visitor's own desktop
    rather than on Home (the shell makes one for a first-time visitor, named after their email here: the e2e
    shell can reach no connector for a profile)."""
    visitor = RequestIdentity(owner=False, user_id=user_id, email=f"{email_local_part}@example.com")
    return _second_client(page, e2e_server, desktop_id, extra_http_headers=identity_headers(visitor))


@pytest.mark.timeout(90, func_only=False)
def test_a_visiting_user_lands_on_a_desktop_of_their_own_seeded_from_home(e2e_server: E2EServer, page: Page) -> None:
    """A signed-in visitor's first page load gets a desktop named after them, holding Home's shortcut and a window at
    each of Home's windows, and leaves Home as it was; a second client of theirs lands there too; and when that
    desktop is deleted, their next load seeds another and says so."""
    _land(page, e2e_server)
    home_window = _open_via_shortcut(page, e2e_server)
    expect(_window(page, home_window)).to_be_visible(timeout=15000)

    with _visiting_client(page, e2e_server, "user-alice", "alice", "alice") as visitor:
        desktops = _get_json(f"{e2e_server.base_url}/api/desktops")["desktops"]
        (alice,) = [desktop for desktop in desktops if desktop["id"] == "alice"]
        assert alice["name"] == "alice"
        (copied,) = alice["windows"]
        assert copied["id"] != home_window and copied["app"] == _STUB_APP_NAME
        # The copy is hers to arrange: it starts minimized in her taskbar, and Home's window is untouched.
        expect(_taskbar_entry(visitor, copied["id"])).to_be_visible(timeout=15000)
        assert [window["id"] for window in _windows(e2e_server.base_url)] == [home_window]
        expect(page.locator('[data-desktop-switch="alice"]')).to_be_visible(timeout=15000)
        expect(visitor.locator("[data-replaced-desktop-notice]")).to_have_count(0)

        with _visiting_client(page, e2e_server, "user-alice", "alice", "alice"):
            pass
        assert [desktop["id"] for desktop in _get_json(f"{e2e_server.base_url}/api/desktops")["desktops"]] == [
            _HOME_DESKTOP_ID,
            "alice",
        ]

        _post_json(f"{e2e_server.base_url}/api/desktops/alice/delete", {})
        visitor.reload()
        expect(visitor.locator('[data-replaced-desktop-notice="alice"]')).to_be_visible(timeout=15000)
        expect(visitor.locator('[data-desktop-id="alice"]')).to_be_visible()
        visitor.locator(".replaced-desktop-dismiss").click()
        expect(visitor.locator("[data-replaced-desktop-notice]")).to_have_count(0)


@pytest.mark.timeout(120, func_only=False)
def test_a_kept_rollback_point_raises_one_banner_naming_its_apps_and_everything_seems_good_clears_it(
    e2e_server: E2EServer, page: Page
) -> None:
    """The record an apply kept names the apps it touched; the shell carries one banner naming them all (a
    rollback takes them back together) and no window carries a notice of its own, "Everything seems good" runs
    the script's confirm, and the cleared record reaches every window through the watch, so the banner goes
    without a reload."""
    write_stub_update_self_script(e2e_server.repo_root)
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    frame = _page_frame(page, window_id)
    expect(page.locator(".update-notice-banner")).to_have_count(0)

    write_rollback_point(e2e_server.repo_root, apps=[_STUB_APP_NAME, "system_interface"])

    banner = page.locator(".update-notice-banner")
    expect(banner).to_be_visible(timeout=15000)
    expect(banner).to_contain_text(f"{_STUB_APP_DISPLAY_NAME} and the workspace interface were updated a moment ago")
    expect(page.locator(".update-notice-rollback")).to_have_count(1)
    # The window of a touched app is still the same page: the notice's arrival reloaded nothing.
    assert frame.evaluate("() => window.__handshake") is not None
    expect(_window(page, window_id).locator(".update-notice-banner")).to_have_count(0)

    banner.locator(".update-notice-confirm").click()

    expect(page.locator(".update-notice-banner")).to_have_count(0, timeout=15000)
    assert _get_json(f"{e2e_server.base_url}/api/updates/pending") is None
