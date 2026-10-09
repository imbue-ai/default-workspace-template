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
import re
import shutil
import subprocess
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
from playwright.sync_api import WebSocket
from playwright.sync_api import WebSocketRoute
from playwright.sync_api import expect
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import message_handling_app
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
    pytest.mark.browser,
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
    # Further registry rows, for apps a test serves itself.
    extra_rows: tuple[str, ...] = (),
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
        write_registry(registry_path, *rows, *extra_rows)
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
# its frame to move it. Its ``#held`` input is state no reload survives. It installs the served element context
# menu as a scaffolded app's page does, so a right-click in the frame drafts through the shell.
_STUB_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Stub</title></head><body>
<div id="where"></div><input id="held" value="" />
<script type="module">
import { connectToShell } from "__BASE_URL__/_static/app_contract.js";
import { installElementContextMenu } from "__BASE_URL__/_static/context_menu.js";
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
  handlers.capabilities = { navigation: true, closeChord: false };
  handlers.onNavigate = (path) => {
    history.replaceState(null, "", path);
    show(path);
    window.__navigations.push(path);
  };
}
const connection = connectToShell(handlers);
installElementContextMenu({ connection, handshake: () => window.__handshake ?? null });
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

    def _serve_posted_launches() -> Response:
        return jsonify(launches)

    app.add_url_rule("/", view_func=_page, endpoint="stub_page_root", methods=["GET"])
    app.add_url_rule("/__launches", view_func=_serve_posted_launches, endpoint="stub_launches", methods=["GET"])
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
    """POST an op to ``/api/layout/broadcast`` the way ``workspace-layout`` does, retrying while the shell
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


def _open_via_shortcut(page: Page, server: E2EServer, key: str = _STUB_SHORTCUT_KEY) -> str:
    """Run a shortcut by double click and wait for the one new window it opens; answers the window id."""
    before = {window["id"] for window in _windows(server.base_url)}

    def _opened() -> set[str]:
        return {window["id"] for window in _windows(server.base_url)} - before

    page.locator(f'[data-shortcut="{key}"]').dblclick()
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


def _travel_duration(window: Locator) -> str:
    """How long the window's chrome would take to travel to a new rectangle, as its computed style has it."""
    return window.evaluate("(element) => getComputedStyle(element).transitionDuration.split(',')[0].trim()")


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


def _open_a_second_window_over_the_first(page: Page, server: E2EServer) -> tuple[str, str]:
    """Open a window clear of the shortcuts, then a second from the shortcut's menu; answers (first, second) once
    the second is focused."""
    first = _open_via_shortcut(page, server)
    _move_window_off_the_shortcuts(page, first)
    page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]').click(button="right")
    page.locator('[data-menu-row="open-new"]').click()
    windows = _wait_for_window_count(server.base_url, 2)
    (second,) = [window["id"] for window in windows if window["id"] != first]
    expect(_window(page, second)).to_have_attribute("data-focused", "true", timeout=15000)
    return first, second


def _press_shield(page: Page, window_id: str) -> None:
    """A press on a lower window's content, which its shield takes, near the shield's bottom-left corner."""
    shield_box = _box(_window(page, window_id).locator("[data-window-shield]"))
    page.mouse.click(shield_box["x"] + 20, shield_box["y"] + shield_box["height"] - 20)


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
    # Nothing of ``browser_context_args`` reaches here, so the suite's reduced motion is asked for again.
    context_args.setdefault("reduced_motion", "reduce")
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
    expect(page).to_have_title(_get_json(f"{e2e_server.base_url}/api/inventory")["workspace_name"])
    shortcut = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
    expect(shortcut).to_have_attribute("data-cell", "0,0")
    expect(shortcut.locator(".shortcut-label")).to_have_text(_STUB_APP_DISPLAY_NAME)
    expect(_shown_windows(page)).to_have_count(0)
    expect(page.locator("[data-taskbar] [data-launcher-field]")).to_be_visible()
    expect(page.locator('[data-tray-widget="desktops"] [data-desktop-switch]')).to_have_count(1)
    expect(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]')).to_be_visible()
    # The wallpaper is the app layout's, so it spans the taskbar too; the backdrop is clear over it.
    assert (
        page.locator(".app-layout")
        .evaluate("(el) => getComputedStyle(el).backgroundImage")
        .endswith('/wallpapers/bundled/arcs")')
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
def test_a_pages_element_menu_drafts_the_reference_into_the_pinned_window(tmp_path: Path, page: Page) -> None:
    """A right-click in a framed page opens the page's own element menu (the served module, as a scaffolded app's
    page installs it); "Explain..." hands the shell a prompt over the element's reference through
    ``shell:draft-text``, and the shell posts the pinned app's draft launch path with it (the element-reference-menu
    plan sections 4.1 and 6), pointing this client's view of the pinned window at the page it answers. The reference
    carries the page's scope from the handshake and the element as the page has it. A right-click on the desktop's
    own backdrop opens the shell's element menu, whose rows are the reference rows."""
    with _running_e2e_server(tmp_path, pin=("plain", "independent", "bar")) as server:
        _land(page, server)
        pinned = _pinned_window(server.base_url)
        client_id = _client_id(page)
        window_id = _open_via_shortcut(page, server)
        frame = _page_frame(page, window_id)

        frame.locator("#where").click(button="right")
        card = frame.locator("[data-context-menu]")
        expect(card).to_be_visible()
        expect(card.locator("[data-context-menu-row]")).to_have_count(3)
        card.locator('[data-context-menu-row="explain-element"]').click()
        expect(card).to_be_hidden()

        assert poll_until(lambda: len(_posted_launches(server.pinned_url)) == 1, timeout=15.0, poll_interval=0.1)
        (posted,) = _posted_launches(server.pinned_url)
        assert posted["path"] == f"/{_PINNED_DRAFT_LAUNCH_ID}"
        assert posted["body"]["client_id"] == client_id
        assert posted["body"]["desktop_id"] == _HOME_DESKTOP_ID
        assert posted["body"]["window_path"] == _PINNED_HOME_PATH
        text = posted["body"][_PINNED_TEXT_PARAM]
        prompt, blank, block_open, block_json, block_close = text.split("\n")
        reference = json.loads(block_json)["element_reference"]
        assert re.fullmatch(r"REF-[0-9a-z]{11}", reference["reference_id"])
        assert (prompt, blank) == (f"Explain what I attached in {reference['reference_id']}", "")
        assert (block_open, block_close) == ("```json", "```")
        assert reference["app"] == _STUB_APP_NAME
        assert reference["window_id"] == window_id
        assert reference["client_id"] == client_id
        assert reference["desktop_id"] == _HOME_DESKTOP_ID
        assert reference["tag"] == "div"
        assert reference["id"] == "where"
        assert reference["selector"] == "#where"
        assert reference["page_path"] == _STUB_LAUNCH_PATH
        assert "text" not in reference
        assert "ancestors" not in reference
        assert "outer_html" not in reference
        assert reference["viewport"]["width"] > 0

        draft_page_path = _launched_page_path(_PINNED_DRAFT_LAUNCH_ID, {_PINNED_TEXT_PARAM: text})
        _wait_for_own_window_path(server.base_url, client_id, pinned["id"], draft_page_path)
        expect(_window(page, pinned["id"])).to_be_visible(timeout=15000)

        # The desktop's own chrome: a right-click on the empty backdrop opens the shell's element menu.
        page.locator("[data-backdrop-area]").click(button="right", position={"x": 700, "y": 20})
        shell_menu = page.locator(".element-menu")
        expect(shell_menu).to_be_visible()
        expect(shell_menu.locator('[data-menu-row="explain-element"]')).to_be_visible()
        page.keyboard.press("Escape")
        expect(shell_menu).to_be_hidden()


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


@pytest.mark.timeout(60, func_only=False)
def test_a_window_travels_only_where_the_platform_welcomes_motion(e2e_server: E2EServer, page: Page) -> None:
    """The travel is asked for, not taken away: a context saying motion is welcome transitions a window's
    rectangle, and one asking for less motion -- which every context of this suite does, so a box can be read
    the moment a state lands -- puts the window at its new rectangle outright."""
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    assert _travel_duration(_window(page, window_id)) == "0s"

    with _second_client(page, e2e_server, reduced_motion="no-preference") as other_page:
        # The window is shared but its placement is not, so it reaches a fresh client minimized.
        _taskbar_entry(other_page, window_id).click()
        expect(_window(other_page, window_id)).to_be_visible(timeout=15000)
        assert _travel_duration(_window(other_page, window_id)) != "0s"


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
    first, second = _open_a_second_window_over_the_first(page, e2e_server)
    expect(_window(page, first)).to_have_attribute("data-focused", "false")
    expect(_window(page, first).locator("[data-window-shield]")).to_have_count(1)
    expect(_window(page, second).locator("[data-window-shield]")).to_have_count(0)

    _press_shield(page, first)
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


@pytest.mark.timeout(60, func_only=False)
def test_raising_a_window_by_its_content_takes_the_focus_off_the_window_now_under_it(
    e2e_server: E2EServer, page: Page
) -> None:
    """A press on a lower window's content raises it without the press reaching any page; the page that held the
    keyboard (the one typed in last) must lose it, or keys go to a window the user can no longer see, and the
    browser hands that page the focus back when the user returns from another application, which raises it again."""
    _land(page, e2e_server)
    first, second = _open_a_second_window_over_the_first(page, e2e_server)
    second_frame = _page_frame(page, second)
    second_frame.click("#held")
    page.keyboard.type("typed")
    assert second_frame.input_value("#held") == "typed"

    _press_shield(page, first)
    expect(_window(page, first)).to_have_attribute("data-focused", "true")
    page.keyboard.type(" stray")

    assert second_frame.input_value("#held") == "typed"
    assert page.evaluate(f"() => document.activeElement?.getAttribute('data-live-page') !== {json.dumps(second)}")


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
def test_shortcut_drag_lifts_the_icon_and_sends_the_shortcut_in_its_way_aside(tmp_path: Path, page: Page) -> None:
    """Dragging a shortcut to an empty cell moves it there for everyone and moves nothing else; held over an
    occupied cell the occupant steps aside under the hand, before the drop, and the drop keeps it there."""
    with _running_e2e_server(tmp_path, is_second_app_offered=True) as server:
        _land(page, server)
        assert _shortcut_cells(server.base_url) == {_STUB_SHORTCUT_KEY: (0, 0), _SECOND_SHORTCUT_KEY: (1, 0)}
        backdrop = _box(page.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]'))

        docs = page.locator(f'[data-shortcut="{_STUB_SHORTCUT_KEY}"]')
        notes = page.locator(f'[data-shortcut="{_SECOND_SHORTCUT_KEY}"]')
        _drag(page, _center(_box(docs)), _cell_center(backdrop, 2, 2))
        wait_for(
            lambda: _shortcut_cells(server.base_url)[_STUB_SHORTCUT_KEY] == (2, 2),
            timeout=15.0,
            poll_interval=0.1,
            error_message="the shortcut never moved to (2, 2)",
        )
        expect(docs).to_have_attribute("data-cell", "2,2")
        # An empty cell needs nothing stepping aside: the other shortcut stayed where it was.
        assert _shortcut_cells(server.base_url)[_SECOND_SHORTCUT_KEY] == (1, 0)

        # Held over the cell docs is in, without releasing: docs has already stepped aside to (1, 2), the
        # nearest free cell to the one it is being displaced from, and notes is the icon in the hand.
        _drag(page, _center(_box(notes)), _cell_center(backdrop, 2, 2), is_released=False)
        expect(notes).to_have_attribute("data-lifted", "true")
        expect(notes).to_have_attribute("data-cell", "2,2")
        expect(docs).to_have_attribute("data-cell", "1,2")
        # Nothing is written until the drop: the step aside is the desktop showing where the icon would land.
        assert _shortcut_cells(server.base_url) == {_STUB_SHORTCUT_KEY: (2, 2), _SECOND_SHORTCUT_KEY: (1, 0)}

        page.mouse.up()
        wait_for(
            lambda: _shortcut_cells(server.base_url) == {_STUB_SHORTCUT_KEY: (1, 2), _SECOND_SHORTCUT_KEY: (2, 2)},
            timeout=15.0,
            poll_interval=0.1,
            error_message="the drop did not keep the room that was made for it",
        )
        expect(notes).to_have_attribute("data-cell", "2,2")
        expect(notes).not_to_have_attribute("data-lifted", "true")
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
    page.locator('[data-menu-row="remove-from-desktop"]').click()
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


# Rapid desktop switches (desktop contracts.md section 6)

# How long a window that has settled after rapid switches is watched: the shell reads a socket's reports once a
# second at most, so a client whose windows and shell were still answering each other's desktop news would send
# reports and redraw its windows several times in this.
_SETTLED_WATCH_MS = 3000
# How many tenths of a second the pages get to hear the move of every switch.
_HEARD_EVERY_SWITCH_POLLS = 150

# Records, in the page, every change of whether a window is drawn and how its taskbar entry reads.
_WINDOW_STATE_RECORDER = """(id) => {
  window.__windowStates = [];
  const record = () => {
    const drawn = document.querySelector(`[data-window-id="${id}"]`) !== null;
    const entry = document.querySelector(`[data-taskbar-entry="${id}"]`);
    const state = `${drawn ? "drawn" : "gone"}/${entry === null ? "no-entry" : entry.getAttribute("data-minimized")}`;
    const last = window.__windowStates[window.__windowStates.length - 1];
    if (last !== state) window.__windowStates.push(state);
  };
  new MutationObserver(record).observe(document.body, { subtree: true, childList: true, attributes: true });
  record();
}"""


class _DesktopTraffic(MutableModel):
    """What one page said and heard about its client's desktop over the socket, appended as it happens."""

    reports: list[dict[str, Any]] = Field(description="Every ``client_state`` the page sent")
    moves: list[dict[str, Any]] = Field(description="Every ``active_desktop_changed`` the page received")


def _take_sent(traffic: _DesktopTraffic, payload: str | bytes) -> None:
    message = json.loads(payload)
    if message.get("type") == "client_state":
        traffic.reports.append(message)


def _take_received(traffic: _DesktopTraffic, payload: str | bytes) -> None:
    message = json.loads(payload)
    if message.get("type") == "active_desktop_changed":
        traffic.moves.append(message)


def _record_desktop_traffic(page: Page) -> _DesktopTraffic:
    """Record the page's desktop traffic from here on; call before the page loads."""
    traffic = _DesktopTraffic(reports=[], moves=[])

    def _watch(websocket: WebSocket) -> None:
        websocket.on("framesent", lambda payload: _take_sent(traffic, payload))
        websocket.on("framereceived", lambda payload: _take_received(traffic, payload))

    page.on("websocket", _watch)
    return traffic


def _create_desktop_shown_on(server: E2EServer, pages: list[Page]) -> str:
    """Create a second desktop and wait until every page offers a switch to it; answers its id."""
    desktop_id = _post_json(f"{server.base_url}/api/desktops", {"name": "Other", "color": "#4477aa", "glyph": 0})["id"]
    for page in pages:
        expect(page.locator(f'[data-desktop-switch="{desktop_id}"]')).to_be_visible(timeout=15000)
    return desktop_id


def _pump_until(page: Page, condition: Callable[[], bool], what: str) -> None:
    """Wait through the sync API, which hands the pages' socket events over only while it is called."""
    for _ in range(_HEARD_EVERY_SWITCH_POLLS):
        if condition():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"never saw {what}")


def _switch_rapidly(page: Page, desktop_ids: list[str], traffics: list[_DesktopTraffic]) -> None:
    """Click the desktops in turn as fast as the pointer goes, waiting on nothing between clicks, then wait until
    every page has heard the move of each click."""
    heard_before = [len(traffic.moves) for traffic in traffics]
    for desktop_id in desktop_ids:
        page.locator(f'[data-desktop-switch="{desktop_id}"]').click()
    _pump_until(
        page,
        lambda: all(
            len(traffic.moves) >= before + len(desktop_ids)
            for traffic, before in zip(traffics, heard_before, strict=True)
        ),
        "every page hear every switch",
    )


def _expect_settled_on(page: Page, desktop_id: str, window_id: str) -> None:
    expect(page.locator(f'[data-desktop-switch="{desktop_id}"]')).to_have_attribute("data-active", "true")
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    expect(_taskbar_entry(page, window_id)).to_have_attribute("data-minimized", "false")


def _watch_settled(pages: list[Page], window_id: str, traffics: list[_DesktopTraffic]) -> None:
    """Watch settled pages for ``_SETTLED_WATCH_MS``: none of them sends a report or hears another move, and the
    window stays drawn and shown on each the whole time."""
    for page in pages:
        page.evaluate(_WINDOW_STATE_RECORDER, window_id)
    before = [(len(traffic.reports), len(traffic.moves)) for traffic in traffics]
    pages[0].wait_for_timeout(_SETTLED_WATCH_MS)
    assert [(len(traffic.reports), len(traffic.moves)) for traffic in traffics] == before, (
        "the client's windows and the shell kept answering each other's desktop moves"
    )
    for page in pages:
        assert page.evaluate("() => window.__windowStates") == ["drawn/false"]


@pytest.mark.timeout(60, func_only=False)
def test_rapid_desktop_switches_settle_on_the_last_one_chosen(e2e_server: E2EServer, page: Page) -> None:
    """Switching back and forth faster than the shell answers puts several moves in flight at once, whose echoes
    reach the window after it has moved on. The window ends on the desktop chosen last and stays there: it neither
    follows those echoes back nor sends anything more, and its windows are never taken down."""
    traffic = _record_desktop_traffic(page)
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    other = _create_desktop_shown_on(e2e_server, [page])

    _switch_rapidly(page, [other, _HOME_DESKTOP_ID, other, _HOME_DESKTOP_ID], [traffic])
    _expect_settled_on(page, _HOME_DESKTOP_ID, window_id)
    _watch_settled([page], window_id, [traffic])


@pytest.mark.timeout(60, func_only=False)
def test_two_windows_of_one_client_settle_together_after_rapid_switches(e2e_server: E2EServer, page: Page) -> None:
    """Two windows of one browser are one client: the other window follows every move of the one switching. A
    window that only follows moves nothing, so the two never answer each other's news back and forth, and both
    end on the desktop chosen last."""
    second = page.context.new_page()
    traffics = [_record_desktop_traffic(page), _record_desktop_traffic(second)]
    _land(page, e2e_server)
    window_id = _open_via_shortcut(page, e2e_server)
    _land(second, e2e_server)
    other = _create_desktop_shown_on(e2e_server, [page, second])

    _switch_rapidly(page, [other, _HOME_DESKTOP_ID, other, _HOME_DESKTOP_ID], traffics)
    for shown in (page, second):
        _expect_settled_on(shown, _HOME_DESKTOP_ID, window_id)
    _watch_settled([page, second], window_id, traffics)
    clients = _get_json(f"{e2e_server.base_url}/api/clients")["clients"]
    assert [client["active_desktop"] for client in clients] == [_HOME_DESKTOP_ID]


class _HeldSocket(_DesktopTraffic):
    """A page's shell socket with what the page sends held back from the shell until released."""

    is_holding: bool = Field(description="Whether what the page sends is held rather than passed on")
    held: list[str] = Field(description="What the page sent while held, oldest first")


def _hold_page_socket(page: Page) -> tuple[_HeldSocket, Callable[[], None]]:
    """Route the page's shell socket through the test, holding what the page sends; call before the page loads.
    Answers the socket's record and the release, which passes the held messages on in order and stops holding."""
    socket = _HeldSocket(is_holding=True, held=[], reports=[], moves=[])
    shell_sides: list[WebSocketRoute] = []

    def _route(page_side: WebSocketRoute) -> None:
        shell_side = page_side.connect_to_server()
        shell_sides.append(shell_side)

        def _from_page(payload: str | bytes) -> None:
            _take_sent(socket, payload)
            if socket.is_holding:
                socket.held.append(str(payload))
            else:
                shell_side.send(payload)

        def _from_shell(payload: str | bytes) -> None:
            _take_received(socket, payload)
            page_side.send(payload)

        page_side.on_message(_from_page)
        shell_side.on_message(_from_shell)

    def _release() -> None:
        socket.is_holding = False
        for payload in socket.held:
            shell_sides[-1].send(payload)
        socket.held.clear()

    page.route_web_socket("**/api/ws", _route)
    return socket, _release


@pytest.mark.timeout(60, func_only=False)
def test_an_op_move_wins_over_a_report_the_page_made_before_hearing_it(e2e_server: E2EServer, page: Page) -> None:
    """An agent's op moves the client while the page's own report of where it landed is still on its way to the
    shell (held here; a socket read late or a slow network in real use). The report was made before the page heard
    of the op's move, so the shell does not record it: the client and the page end on the op's desktop, and no
    further move is announced."""
    socket, release = _hold_page_socket(page)
    _land(page, e2e_server)
    _pump_until(page, lambda: any(not report["is_following"] for report in socket.reports), "the landing's report")
    client_id = _client_id(page)
    other = _create_desktop_shown_on(e2e_server, [page])

    _broadcast_op(e2e_server.base_url, "load", {"desktop": other, "client": client_id})
    other_switch = page.locator(f'[data-desktop-switch="{other}"]')
    expect(other_switch).to_have_attribute("data-active", "true", timeout=15000)
    _pump_until(page, lambda: any(report["is_following"] for report in socket.reports), "the page follow the op")
    assert [move["desktop_id"] for move in socket.moves] == [other]

    release()

    def _is_registered() -> bool:
        clients = _get_json(f"{e2e_server.base_url}/api/clients")["clients"]
        return any(client["id"] == client_id and client["is_connected"] for client in clients)

    _pump_until(page, _is_registered, "the shell register the page's reports")
    page.wait_for_timeout(_SETTLED_WATCH_MS)
    assert [move["desktop_id"] for move in socket.moves] == [other]
    expect(other_switch).to_have_attribute("data-active", "true")
    clients = _get_json(f"{e2e_server.base_url}/api/clients")["clients"]
    assert [client["active_desktop"] for client in clients if client["id"] == client_id] == [other]


# Pinned windows (pinned-taskbar-entries plan sections 3.2, 4.1, 4.3, 4.4)


def _pinned_window(base_url: str, desktop_id: str = _HOME_DESKTOP_ID) -> dict[str, Any]:
    """The pinned stub's window on the desktop, off the API; the desktop holds exactly one."""
    (pinned,) = [window for window in _windows(base_url, desktop_id) if window["app"] == _PINNED_APP_NAME]
    assert pinned["is_pinned"] is True
    return pinned


def _pinned_entry(page: Page) -> Locator:
    return page.locator(f'[data-pinned-entry="{_PINNED_APP_NAME}"]')


def _resting_box(page: Page, entry: Locator) -> FloatRect:
    """A floating entry's box with the pointer off it and its tile at rest. Under the pointer the tile grows by a
    tenth about its centre, through a transition reduced motion leaves on, so a box read there is anywhere between
    its place and its place grown."""
    page.mouse.move(0, 0)
    page.wait_for_function("(element) => element.getAnimations().length === 0", arg=entry.element_handle())
    return _box(entry)


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


# Flaky: failed once in CI on a navigate the page never followed (the window showed no "Stub /?doc=1"), and
# passes locally; likely the same shell navigation race as the chat app's send-picker test (livePages.ts follow()).
@pytest.mark.flaky
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
        before = _resting_box(page, entry)
        # The default corner: bottom right of the backdrop, inset by the theme's tokens.
        _assert_close(before["x"] + before["width"], backdrop["x"] + backdrop["width"] - 16, "default x")
        _assert_close(before["y"] + before["height"], backdrop["y"] + backdrop["height"] - 12, "default y")
        _drag(page, _center(before), (_center(before)[0] - 300, _center(before)[1] - 200))
        moved = _resting_box(page, entry)
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
        _assert_same_box(_resting_box(page, _pinned_entry(page)), moved, "after reload")

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
        _assert_same_box(_resting_box(page, _pinned_entry(page)), moved, "back afloat")


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
        # The default design is the character, which the page draws itself rather than loading as an image.
        expect(entry.locator("[data-character-body]")).to_have_count(1)
        expect(entry.locator("img")).to_have_count(0)

        _write_agent_events(server.agent_events_path, "RUNNING")
        expect(entry).to_have_attribute("data-mood", "working", timeout=15000)
        expect(entry).to_have_attribute("data-stale", "false")
        _write_agent_events(server.agent_events_path, "STOPPED")
        expect(entry).to_have_attribute("data-mood", "idle", timeout=15000)

        # The plain style shows the app's icon in place of the avatar; the pin's style brings the image back.
        _open_entry_menu(page, entry).locator('[data-menu-row="style-plain"]').click()
        expect(entry).to_have_attribute("data-entry-style", "plain", timeout=10000)
        expect(entry.locator("svg")).to_have_count(1)
        expect(entry.locator("[data-character-body]")).to_have_count(0)
        _wait_for_client_entry(
            server.base_url,
            _client_id(page),
            lambda entry: entry.get("style") == "plain",
            "the style never reached the client record",
        )
        _open_entry_menu(page, entry).locator('[data-menu-row="style-avatar"]').click()
        expect(entry).to_have_attribute("data-entry-style", "avatar", timeout=10000)
        expect(entry.locator("[data-character-body]")).to_have_count(1)

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
            expect(chooser.locator('[data-avatar-design="imbue-character"]')).to_have_attribute("aria-pressed", "true")
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


# A phone-shaped browser context, inlined so the emulated UA is pinned rather than drifting with the Playwright
# version. The shell reads the phone layout off the viewport's size and touch off the coarse pointer.
_MOBILE_CONTEXT_ARGS: dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "viewport": {"width": 393, "height": 852},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
}


@contextlib.contextmanager
def _phone_client(
    page: Page, server: E2EServer, viewport: dict[str, int] | None = None
) -> Generator[Page, None, None]:
    """A phone of its own (a second browser context, so its own client id), landed in the phone layout."""
    context = _second_context(
        page, **{**_MOBILE_CONTEXT_ARGS, "viewport": viewport or _MOBILE_CONTEXT_ARGS["viewport"]}
    )
    try:
        phone = context.new_page()
        phone.goto(f"{server.base_url}/")
        expect(phone.locator("[data-phone-bar]")).to_be_visible(timeout=15000)
        yield phone
    finally:
        context.close()


def _phone_pill(phone: Page) -> Locator:
    return phone.locator("[data-phone-pill]")


def _phone_shows(phone: Page, window_id: str) -> None:
    """Wait until the phone shows the window: the pill names it and its page is laid over the page host."""
    expect(_phone_pill(phone)).to_have_attribute("data-phone-pill", window_id, timeout=15000)
    expect(phone.locator(f'iframe[data-live-page="{window_id}"]')).to_be_visible(timeout=15000)


def _phone_sheet_rows(phone: Page) -> list[str]:
    """The windows sheet's rows, in order, by window id."""
    return [row.get_attribute("data-phone-window-row") or "" for row in phone.locator("[data-phone-window-row]").all()]


def _long_press(target: Locator) -> None:
    """A touch press held still past the long-press time."""
    target.dispatch_event(
        "pointerdown", {"pointerType": "touch", "button": 0, "buttons": 1, "pointerId": 7, "bubbles": True}
    )


def _shown_history(base_url: str, client_id: str) -> list[str]:
    (client,) = [client for client in _get_json(f"{base_url}/api/clients")["clients"] if client["id"] == client_id]
    return list(client["shown_history"])


@pytest.mark.timeout(120, func_only=False)
def test_only_a_phone_sized_viewport_gets_the_phone_layout(e2e_server: E2EServer, page: Page) -> None:
    """Phones either way round get the phone layout; a tablet, the Studio's smallest window, and a desktop window
    that is short but wide keep the desktop. The layout follows the viewport live, without a reload."""
    _land(page, e2e_server)
    # Each size flips the layout, so every assertion waits for the media query's change rather than passing on the
    # state the previous size left.
    sizes_and_phone = [
        ((393, 852), True),
        ((800, 562), False),
        ((852, 393), True),
        ((1255, 561), False),
        ((440, 956), True),
        ((1200, 480), False),
        ((956, 440), True),
        ((744, 1133), False),
    ]
    for (width, height), is_phone in sizes_and_phone:
        page.set_viewport_size({"width": width, "height": height})
        if is_phone:
            expect(page.locator("html"), f"{width}x{height}").to_have_attribute("data-phone", "")
        else:
            expect(page.locator("html"), f"{width}x{height}").not_to_have_attribute("data-phone", "")


@pytest.mark.timeout(120, func_only=False)
@pytest.mark.parametrize("viewport", [{"width": 393, "height": 852}, {"width": 852, "height": 393}])
def test_a_phone_in_either_orientation_lands_on_the_pinned_window(
    tmp_path: Path, page: Page, viewport: dict[str, int]
) -> None:
    """Upright or on its side, a phone gets the phone layout (the bar, no taskbar, no window chrome) and lands on the
    pinned window, its page filling the space above the bar and the pill wearing the avatar; a reload lands on what
    it showed last, home included."""
    with _running_e2e_server(tmp_path, pin=("avatar", "linked", "bar")) as server:
        pinned = _pinned_window(server.base_url)
        with _phone_client(page, server, viewport) as phone:
            expect(phone.locator("html")).to_have_attribute("data-phone", "")
            expect(phone.locator("[data-taskbar]")).to_have_count(0)
            expect(phone.locator("[data-window-id] .title-bar")).to_have_count(0)
            _phone_shows(phone, pinned["id"])
            expect(_phone_pill(phone).locator("[data-character-body]")).to_be_visible()
            host = _box(phone.locator("[data-phone-page-host]"))
            _assert_same_box(_box(phone.locator(f'iframe[data-live-page="{pinned["id"]}"]')), host, "phone page")
            bar = _box(phone.locator("[data-phone-bar]"))
            _assert_close(host["y"] + host["height"], bar["y"], "the page's foot against the bar")
            _assert_close(bar["y"] + bar["height"], viewport["height"], "the bar's foot against the screen's")

            phone.locator("[data-phone-home]").tap()
            expect(phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]')).to_be_visible()
            expect(phone.locator("[data-phone-home]")).to_be_disabled()
            wait_for(
                lambda: _shown_history(server.base_url, _client_id(phone))[-1:] == ["home"],
                timeout=10.0,
                poll_interval=0.1,
                error_message="the phone never recorded going home",
            )
            phone.reload()
            expect(phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]')).to_be_visible(timeout=15000)
            expect(_phone_pill(phone)).to_have_attribute("data-phone-pill", "home")


@pytest.mark.timeout(120, func_only=False)
def test_a_phone_and_a_laptop_share_the_windows_but_the_phone_moves_nothing(e2e_server: E2EServer, page: Page) -> None:
    """A window the laptop opens is in the phone's windows sheet, and showing it on the phone writes no placement
    anywhere; a window the phone opens from the start sheet lands on the first desktop, shown on the phone and
    minimized on the laptop; and when the laptop closes the window the phone shows, the phone goes home."""
    _land(page, e2e_server)
    laptop_window = _open_via_shortcut(page, e2e_server)
    laptop_client = _client_id(page)
    laptop_placements = _placements(e2e_server.base_url, laptop_client)
    with _phone_client(page, e2e_server) as phone:
        phone_client = _client_id(phone)
        _phone_pill(phone).tap()
        expect(phone.locator(f'[data-phone-window-row="{laptop_window}"]')).to_be_visible(timeout=15000)
        phone.locator(f'[data-phone-window-row="{laptop_window}"]').tap()
        _phone_shows(phone, laptop_window)
        expect(phone.locator('[data-phone-sheet="windows"]')).to_have_count(0)
        phone.wait_for_timeout(_NEGATIVE_SETTLE_MS)
        assert _placements(e2e_server.base_url, laptop_client) == laptop_placements
        assert laptop_window not in _placements(e2e_server.base_url, phone_client)

        phone.locator("[data-phone-new]").tap()
        field = phone.locator("[data-phone-start-field]")
        expect(field).to_have_attribute("placeholder", "Open an app or send a message")
        expect(field).not_to_be_focused()
        field.fill(_STUB_LAUNCH_LABEL)
        field.press("Enter")
        (opened,) = [
            window["id"] for window in _wait_for_window_count(e2e_server.base_url, 2) if window["id"] != laptop_window
        ]
        _phone_shows(phone, opened)
        assert _placements(e2e_server.base_url, phone_client)[opened]["is_minimized"] is True
        expect(_taskbar_entry(page, opened)).to_have_attribute("data-minimized", "true", timeout=15000)
        assert opened not in _placements(e2e_server.base_url, laptop_client)

        _taskbar_entry(page, opened).click()
        expect(_window(page, opened)).to_be_visible(timeout=15000)
        _window(page, opened).locator('[data-window-control="close"]').click()
        expect(phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]')).to_be_visible(timeout=15000)
        expect(_phone_pill(phone)).to_have_attribute("data-phone-pill", "home")


@pytest.mark.timeout(120, func_only=False)
def test_the_phone_home_grid_and_windows_sheet(tmp_path: Path, page: Page) -> None:
    """A home tile shows the app's window when it has one and launches it when it has none; the windows sheet puts
    what the phone showed first and has no X on the pinned window; an X closes a window, and Close all, once
    confirmed, closes every window but the pinned one."""
    with _running_e2e_server(tmp_path, is_second_app_offered=True, pin=("avatar", "linked", "bar")) as server:
        pinned = _pinned_window(server.base_url)
        with _phone_client(page, server) as phone:
            phone.locator("[data-phone-home]").tap()
            phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]').tap()
            (docs,) = [
                window["id"] for window in _wait_for_window_count(server.base_url, 2) if not window["is_pinned"]
            ]
            _phone_shows(phone, docs)
            phone.locator("[data-phone-home]").tap()
            phone.locator(f'[data-phone-app="{_SECOND_APP_NAME}"]').tap()
            (notes,) = [
                window["id"]
                for window in _wait_for_window_count(server.base_url, 3)
                if not window["is_pinned"] and window["id"] != docs
            ]
            _phone_shows(phone, notes)
            # Docs has a window: its tile shows that window rather than opening another.
            phone.locator("[data-phone-home]").tap()
            phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]').tap()
            _phone_shows(phone, docs)
            assert len(_windows(server.base_url)) == 3
            expect(phone.locator("[data-phone-count]")).to_have_text("2")

            _phone_pill(phone).tap()
            expect(phone.locator('[data-phone-sheet="windows"]')).to_be_visible()
            assert _phone_sheet_rows(phone) == [docs, notes, pinned["id"]]
            expect(phone.locator(f'[data-phone-window-close="{pinned["id"]}"]')).to_have_count(0)
            phone.locator(f'[data-phone-window-close="{notes}"]').tap()
            _wait_for_window_count(server.base_url, 2)
            expect(phone.locator(f'[data-phone-window-row="{notes}"]')).to_have_count(0)
            # The sheet covers the bar; a tap on the scrim above it takes it down.
            phone.locator('[data-phone-sheet-scrim="windows"]').click(position={"x": 200, "y": 20})
            expect(phone.locator('[data-phone-sheet="windows"]')).to_have_count(0)

            phone.locator("[data-phone-home]").tap()
            phone.locator(f'[data-phone-app="{_SECOND_APP_NAME}"]').tap()
            _wait_for_window_count(server.base_url, 3)
            _phone_pill(phone).tap()
            prompts: list[str] = []

            def _accept(dialog: Any) -> None:
                prompts.append(dialog.message)
                dialog.accept()

            phone.on("dialog", _accept)
            phone.locator("[data-phone-close-all]").tap()
            _wait_for_window_count(server.base_url, 1)
            assert prompts == ["Close 2 windows?"]
            assert [window["id"] for window in _windows(server.base_url)] == [pinned["id"]]


@pytest.mark.timeout(120, func_only=False)
def test_the_phone_pill_menu_start_sheet_message_and_a_refused_open(tmp_path: Path, page: Page) -> None:
    """A long press on the pill offers the shown window's menu without the desktop's placement verbs; a message
    typed into the start sheet goes to the pinned window's app and the phone shows that window; and an open the
    shell refuses says so in a toast rather than an alert."""
    with _running_e2e_server(tmp_path, pin=("avatar", "linked", "bar")) as server:
        pinned = _pinned_window(server.base_url)
        with _phone_client(page, server) as phone:
            _phone_shows(phone, pinned["id"])
            _long_press(_phone_pill(phone))
            menu = phone.locator(".phone-window-menu")
            expect(menu).to_be_visible(timeout=5000)
            expect(menu.locator('[data-menu-row="refresh"]')).to_have_count(1)
            for absent in ("minimize", "close", "size", "pop-out"):
                expect(menu.locator(f'[data-menu-row="{absent}"]')).to_have_count(0)
            phone.keyboard.press("Escape")

            phone.locator("[data-phone-home]").tap()
            phone.locator("[data-phone-new]").tap()
            field = phone.locator("[data-phone-start-field]")
            field.fill("plan the trip")
            field.press("Enter")
            wait_for(
                lambda: any(
                    launch["body"].get(_PINNED_TEXT_PARAM) == "plan the trip"
                    for launch in _posted_launches(server.pinned_url)
                ),
                timeout=15.0,
                poll_interval=0.1,
                error_message="the message never reached the pinned app",
            )
            _phone_shows(phone, pinned["id"])

            phone.route(
                re.compile(r".*/api/desktops/[^/]+/launch$"),
                lambda route: route.fulfill(status=503, json={"detail": "the shell is restarting"}),
            )
            phone.locator("[data-phone-home]").tap()
            phone.locator(f'[data-phone-app="{_STUB_APP_NAME}"]').tap()
            expect(phone.locator("[data-toast]")).to_contain_text("the shell is restarting", timeout=10000)


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


# Past the grace a freshly torn-out pop-out gives the desktop's detach save before writing the detach itself
# (``SOLO_HEAL_GRACE_MS`` in DesktopStore.ts): long enough for a pop-out that was going to write it to have.
_PAST_SOLO_HEAL_GRACE_MS = 2000

# Records, in the page it is added to, every ``minds:detached-windows`` report and ``minds:pop-out-window`` ask the
# page's shell sends its embedder: a top-level page is its own parent, so it hears its own messages.
_RECORD_DETACHED_REPORTS_SCRIPT = """
window.__detachedReports = [];
window.__popOutAsks = [];
window.addEventListener("message", (event) => {
  if (event.data?.type === "minds:detached-windows") window.__detachedReports.push(event.data.windows);
  if (event.data?.type === "minds:pop-out-window") window.__popOutAsks.push(event.data.windowId);
});
"""


def _pop_out(page: Page, server: E2EServer, window_id: str, is_reopened: bool = False) -> Page:
    """A pulled-out window's own page as the chrome's desktop window loads it: a second page of ``page``'s browser
    context (so the same client) at ``/?solo=<window_id>``, marked reopened when asked, showing the window's page."""
    pop_out = page.context.new_page()
    pop_out.add_init_script(_RECORD_DETACHED_REPORTS_SCRIPT)
    query = f"?solo={window_id}" + ("&reopened=1" if is_reopened else "")
    pop_out.goto(f"{server.base_url}/{query}")
    _page_frame(pop_out, window_id)
    return pop_out


@pytest.mark.timeout(90, func_only=False)
def test_a_pop_out_is_reached_by_a_refresh_of_its_window_and_stays_a_pop_out_over_the_interface_reload(
    e2e_server: E2EServer, page: Page
) -> None:
    """A pulled-out window's own page registers with the shell under its client, so ``refresh <window>`` reloads
    it (and the main window's hidden copy of it), and it keeps ``?solo=`` in its URL, so the interface reload
    brings it back as the pop-out rather than as a whole desktop."""
    _land(page, e2e_server)
    client_id = _client_id(page)
    window_id = _broadcast_op(e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/", "client": client_id})[
        "window_id"
    ]
    main_copy = _page_frame(page, window_id)
    pop_out = _pop_out(page, e2e_server, window_id)
    # No drag wrote the detach, so the fresh pop-out writes it itself once its grace is over.
    _wait_for_stored_placement(e2e_server, client_id, window_id, lambda placement: placement["is_detached"], "detach")
    expect(_window(page, window_id)).to_have_count(0, timeout=15000)

    pop_out_page = _page_frame(pop_out, window_id)
    for frame in (pop_out_page, main_copy):
        frame.evaluate("() => { window.__beforeRefresh = true; }")
    _broadcast_op(e2e_server.base_url, "refresh", {"window": window_id, "client": client_id})
    for frame in (pop_out_page, main_copy):
        frame.wait_for_function(
            "() => window.__beforeRefresh === undefined && window.__handshake !== undefined", timeout=15000
        )

    with pop_out.expect_navigation(timeout=15000):
        _broadcast_op(e2e_server.base_url, "reload_system_interface", {})
    assert urllib.parse.parse_qs(urllib.parse.urlparse(pop_out.url).query)["solo"] == [window_id]
    _page_frame(pop_out, window_id)
    expect(pop_out.locator("[data-taskbar]")).to_have_count(0)
    assert _stored_placements(e2e_server.state_dir, client_id)[window_id]["is_detached"] is True


@pytest.mark.timeout(60, func_only=False)
def test_a_pop_out_reopened_at_launch_over_a_window_brought_back_closes_rather_than_pulling_it_out_again(
    e2e_server: E2EServer, page: Page
) -> None:
    """A pop-out the chrome reopened (a relaunch) whose window was brought back while it was away reports the
    window back at once, which is what closes it, and never writes the window out again after the grace."""
    _land(page, e2e_server)
    client_id = _client_id(page)
    window_id = _broadcast_op(e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/", "client": client_id})[
        "window_id"
    ]
    _wait_for_stored_placement(
        e2e_server, client_id, window_id, lambda placement: not placement["is_detached"], "the open"
    )
    pop_out = _pop_out(page, e2e_server, window_id, is_reopened=True)

    pop_out.wait_for_function("() => window.__detachedReports.length > 0", timeout=15000)
    pop_out.wait_for_timeout(_PAST_SOLO_HEAL_GRACE_MS)
    assert pop_out.evaluate("() => window.__detachedReports") == [[]]
    assert _stored_placements(e2e_server.state_dir, client_id)[window_id]["is_detached"] is False
    expect(_window(page, window_id)).to_be_visible()


@pytest.mark.timeout(90, func_only=False)
def test_agent_ops_raise_a_pop_out_in_its_own_window_refuse_to_move_it_and_bring_it_back_when_forced(
    e2e_server: E2EServer, page: Page
) -> None:
    """``focus``, and an ``open`` that finds the window, ask the pop-out's own page to raise its window and leave it
    popped out; ``restore`` is refused without ``force``; with it the window is back on the desktop, and the
    pop-out's page reports it back, which is what closes its window."""
    _land(page, e2e_server)
    client_id = _client_id(page)
    window_id = _broadcast_op(e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/", "client": client_id})[
        "window_id"
    ]
    pop_out = _pop_out(page, e2e_server, window_id)
    _wait_for_stored_placement(e2e_server, client_id, window_id, lambda placement: placement["is_detached"], "detach")

    focused = _broadcast_op(e2e_server.base_url, "focus", {"window": window_id, "client": client_id})
    assert focused["is_raised_in_own_window"] is True
    pop_out.wait_for_function(f"() => window.__popOutAsks.includes({json.dumps(window_id)})", timeout=15000)
    found = _broadcast_op(e2e_server.base_url, "open", {"app": _STUB_APP_NAME, "path": "/", "client": client_id})
    assert (found["window_id"], found["is_raised_in_own_window"]) == (window_id, True)
    pop_out.wait_for_function("() => window.__popOutAsks.length >= 2", timeout=15000)
    assert _stored_placements(e2e_server.state_dir, client_id)[window_id]["is_detached"] is True
    expect(_window(page, window_id)).to_have_count(0)

    with pytest.raises(urllib.error.HTTPError) as refused:
        _post_json(
            f"{e2e_server.base_url}/api/layout/broadcast",
            {"op": "restore", "args": {"window": window_id, "client": client_id}, "requester": None},
        )
    assert refused.value.code == 423
    assert _stored_placements(e2e_server.state_dir, client_id)[window_id]["is_detached"] is True

    restored = _broadcast_op(e2e_server.base_url, "restore", {"window": window_id, "client": client_id, "force": True})
    assert restored["is_brought_back"] is True
    expect(_window(page, window_id)).to_be_visible(timeout=15000)
    pop_out.wait_for_function(
        "() => { const last = window.__detachedReports.at(-1); return last !== undefined && last.length === 0; }",
        timeout=15000,
    )


# The Imbue Studio chrome, played by a page on its own origin: it frames the shell, waits for the shell's
# ``minds:workspace-ready``, and then posts the chat notification's ask down to it, as the Imbue Studio app does.
_CHROME_PAGE_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Chrome</title></head><body>
<iframe id="workspace" src="__SHELL_URL__/" style="width: 1200px; height: 800px"></iframe>
<script>
window.__readyCount = 0;
window.addEventListener("message", (event) => {
  const frame = document.getElementById("workspace");
  if (event.source !== frame.contentWindow || event.data?.type !== "minds:workspace-ready") return;
  window.__readyCount += 1;
  frame.contentWindow.postMessage({ type: "minds:focus-chat", chatId: "__CHAT_ID__" }, "*");
});
</script></body></html>"""

_FOCUS_CHAT_TYPE = "minds:focus-chat"
_FOCUS_CHAT_HANDLER_PATH = "/api/focus-chat"
_FOCUSED_CHAT_ID = "agent-5f0c2e7a"


def _chrome_app(shell_url: str) -> Flask:
    app = Flask("chrome")
    page = _CHROME_PAGE_TEMPLATE.replace("__SHELL_URL__", shell_url).replace("__CHAT_ID__", _FOCUSED_CHAT_ID)
    app.add_url_rule("/", view_func=lambda: Response(page, mimetype="text/html"), endpoint="chrome")
    return app


@pytest.mark.timeout(90, func_only=False)
def test_a_message_from_the_minds_chrome_reaches_the_app_that_registered_its_type_once_with_the_client(
    tmp_path: Path, page: Page
) -> None:
    """The shell framed by the Imbue Studio chrome relays ``minds:focus-chat`` to the app whose registry row registers the
    type: the app's handler route is posted the message once, with the client id of the shell page that received
    it, and the shell reads nothing of it itself."""
    received: list[dict[str, Any]] = []
    with serve_app(message_handling_app(received, _FOCUS_CHAT_HANDLER_PATH, 200)) as handler_app:
        handler_row = registry_row_toml(
            "helper",
            handler_app.http_url,
            display_name="Helper",
            message_handlers=[(_FOCUS_CHAT_TYPE, _FOCUS_CHAT_HANDLER_PATH)],
        )
        with _running_e2e_server(tmp_path, extra_rows=(handler_row,)) as server:
            port = find_free_port()
            chrome_server = make_threaded_server("127.0.0.1", port, _chrome_app(server.base_url))
            chrome_thread = threading.Thread(target=chrome_server.serve_forever, daemon=True)
            chrome_thread.start()
            try:
                page.goto(f"http://127.0.0.1:{port}/")
                shell_frame = page.frame_locator("#workspace")
                expect(shell_frame.locator(f'[data-desktop-id="{_HOME_DESKTOP_ID}"]')).to_be_visible(timeout=15000)
                page.wait_for_function("() => window.__readyCount === 1", timeout=15000)
                wait_for(
                    lambda: len(received) >= 1,
                    timeout=15.0,
                    poll_interval=0.1,
                    error_message="the app registered for minds:focus-chat was never posted the message",
                )
                page.wait_for_timeout(_NEGATIVE_SETTLE_MS)
                shell = next(frame for frame in page.frames if frame.url.startswith(f"{server.base_url}/"))
                client_id = shell.evaluate("() => localStorage.getItem('si-client-id')")
            finally:
                chrome_server.shutdown()
                chrome_thread.join(timeout=5.0)
                chrome_server.server_close()
    assert isinstance(client_id, str) and client_id
    assert received == [{"type": _FOCUS_CHAT_TYPE, "client_id": client_id, "chatId": _FOCUSED_CHAT_ID}]


# The File Viewer (``system/apps/files``): dufs over a folder of the test's own, with the workspace's vendored and
# patched frontend, registered as the ``files`` app. The workspace image installs dufs; elsewhere these tests skip.
_FILES_APP_NAME = "files"
_FILES_ASSETS_DIRECTORY = Path(__file__).resolve().parents[3] / "files" / "assets"
_DUFS_BINARY = shutil.which("dufs")
# dufs answers a client it takes for a script (curl and the like) with a bare "Not Found" instead of the assets'
# ``404.html``, so a direct request says it is a browser.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)


@contextlib.contextmanager
def _running_file_viewer(root: Path) -> Generator[str, None, None]:
    """Run dufs over ``root`` as the File Viewer's program line runs it over ``/``; yields its URL."""
    assert _DUFS_BINARY is not None
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    command = [_DUFS_BINARY, "--allow-all", "--bind", "127.0.0.1", "--port", str(port)]
    command += ["--assets", str(_FILES_ASSETS_DIRECTORY), str(root)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(
            lambda: is_server_answering(url),
            timeout=10.0,
            poll_interval=0.1,
            error_message=f"dufs did not come up at {url}",
        )
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5.0)


@contextlib.contextmanager
def _running_e2e_server_with_file_viewer(tmp_path: Path, root: Path) -> Generator[tuple[E2EServer, str], None, None]:
    """The shell over the stub app and a File Viewer serving ``root``; yields the shell and the viewer's URL."""
    with _running_file_viewer(root) as viewer_url:
        row = registry_row_toml(
            _FILES_APP_NAME,
            viewer_url,
            display_name="File Viewer",
            launch_paths=(("new", "File Viewer", "/"),),
        )
        with _running_e2e_server(tmp_path, extra_rows=(row,)) as server:
            yield server, viewer_url


def _file_viewer_frame(page: Page, window_id: str) -> Frame:
    """The frame of a File Viewer window; it speaks only its location beacon, so there is no handshake to await."""
    handle = page.locator(f'iframe[data-live-page="{window_id}"]').element_handle(timeout=15000)
    frame = handle.content_frame()
    assert frame is not None
    return frame


def _raise_window(page: Page, server: E2EServer, client_id: str, window_id: str) -> None:
    """Bring a window covered by a newer one back on top, as an agent's ``focus`` op does."""
    _broadcast_op(server.base_url, "focus", {"window": window_id, "client": client_id})
    expect(_window(page, window_id)).to_have_attribute("data-focused", "true", timeout=15000)


def _window_record(base_url: str, window_id: str) -> dict[str, Any]:
    return next(window for window in _windows(base_url) if window["id"] == window_id)


def _wait_for_window_at(base_url: str, window_id: str, path: str) -> dict[str, Any]:
    wait_for(
        lambda: _window_record(base_url, window_id)["path"] == path,
        timeout=15.0,
        poll_interval=0.1,
        error_message=f"window {window_id} never reached {path!r}",
    )
    return _window_record(base_url, window_id)


@pytest.mark.skipif(_DUFS_BINARY is None, reason="dufs is not installed (the workspace image installs it)")
@pytest.mark.timeout(120, func_only=False)
def test_the_file_viewer_opens_files_in_workspace_windows_and_raises_one_already_on_the_page(
    tmp_path: Path, page: Page
) -> None:
    """In a File Viewer window, a folder opens in place; a file's name opens its view page in a new window of the
    File Viewer, titled after the file, and a second click raises that window; the view page's Edit takes the same
    window to the edit page; the listing's Edit then raises that window, and a modified click on the name opens a
    view page again, as no window is on it any more. No click opens a browser window of its own."""
    root = tmp_path / "viewer-root"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan 1.txt").write_text("the plan\n")
    view_path = "/notes/plan%201.txt?view"
    edit_path = "/notes/plan%201.txt?edit"
    with _running_e2e_server_with_file_viewer(tmp_path, root) as (server, _):
        _land(page, server)
        client_id = _client_id(page)
        listing_id = _broadcast_op(
            server.base_url, "open", {"app": _FILES_APP_NAME, "path": "/", "client": client_id}
        )["window_id"]
        listing = _file_viewer_frame(page, listing_id)

        listing.get_by_role("link", name="notes", exact=True).click()
        _wait_for_window_at(server.base_url, listing_id, "/notes/")
        assert [window["id"] for window in _windows(server.base_url)] == [listing_id]

        listing.get_by_role("link", name="plan 1.txt", exact=True).click()
        (viewer,) = [window for window in _wait_for_window_count(server.base_url, 2) if window["id"] != listing_id]
        assert viewer["app"] == _FILES_APP_NAME and viewer["path"] == view_path
        wait_for(
            lambda: _window_record(server.base_url, viewer["id"])["title"] == "plan 1.txt",
            timeout=15.0,
            poll_interval=0.1,
            error_message="the view page's window was never titled after the file",
        )
        expect(_window(page, viewer["id"])).to_have_attribute("data-focused", "true", timeout=15000)

        _raise_window(page, server, client_id, listing_id)
        listing.get_by_role("link", name="plan 1.txt", exact=True).click()
        expect(_window(page, viewer["id"])).to_have_attribute("data-focused", "true", timeout=15000)
        _assert_no_further_window(page, server, [listing_id, viewer["id"]])

        viewer_frame = _file_viewer_frame(page, viewer["id"])
        viewer_frame.locator(".edit-file").click()
        _wait_for_window_at(server.base_url, viewer["id"], edit_path)
        assert [window["id"] for window in _windows(server.base_url)] == [listing_id, viewer["id"]]

        _raise_window(page, server, client_id, listing_id)
        listing.locator('a[title="Edit file"]').click()
        expect(_window(page, viewer["id"])).to_have_attribute("data-focused", "true", timeout=15000)
        _assert_no_further_window(page, server, [listing_id, viewer["id"]])

        _raise_window(page, server, client_id, listing_id)
        listing.get_by_role("link", name="plan 1.txt", exact=True).click(modifiers=["ControlOrMeta"])
        (second_viewer,) = [
            window
            for window in _wait_for_window_count(server.base_url, 3)
            if window["id"] not in (listing_id, viewer["id"])
        ]
        assert second_viewer["path"] == view_path
        assert page.context.pages == [page], "a File Viewer click opened a browser window of its own"


@pytest.mark.skipif(_DUFS_BINARY is None, reason="dufs is not installed (the workspace image installs it)")
@pytest.mark.timeout(90, func_only=False)
def test_the_file_viewer_answers_a_missing_path_with_its_own_page_naming_it_and_the_nearest_folder(
    tmp_path: Path, page: Page
) -> None:
    """A path that does not exist answers 404 with the File Viewer's own page, which, in a window, names the path
    asked for, links the nearest folder above it that exists, and titles the window after the missing name."""
    root = tmp_path / "viewer-root"
    (root / "notes").mkdir(parents=True)
    missing = "/notes/gone/missing.txt"
    with _running_e2e_server_with_file_viewer(tmp_path, root) as (server, viewer_url):
        request = urllib.request.Request(f"{viewer_url}{missing}?view", headers={"User-Agent": _BROWSER_USER_AGENT})
        with pytest.raises(urllib.error.HTTPError) as answered:
            urllib.request.urlopen(request, timeout=5)
        assert answered.value.code == 404
        assert "<h1>Not found</h1>" in answered.value.read().decode()

        _land(page, server)
        client_id = _client_id(page)
        window_id = _broadcast_op(
            server.base_url, "open", {"app": _FILES_APP_NAME, "path": f"{missing}?view", "client": client_id}
        )["window_id"]
        frame = _file_viewer_frame(page, window_id)
        expect(frame.locator(".asked-path")).to_have_text(missing, timeout=15000)
        nearest = frame.locator(".nearest-folder")
        expect(nearest).to_have_text("/notes/", timeout=15000)
        assert nearest.get_attribute("href") == "/notes/"
        wait_for(
            lambda: _window_record(server.base_url, window_id)["title"] == "missing.txt",
            timeout=15.0,
            poll_interval=0.1,
            error_message="the not-found page's window was never titled after the missing name",
        )
