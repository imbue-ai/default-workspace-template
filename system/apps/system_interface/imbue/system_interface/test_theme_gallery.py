"""Browser: every built-in theme, worn by the theme gallery and by the desktop (docs/system/blueprint/workspace-themes/).

The shell runs over a copy of the template's real theme folders. For each built-in theme the gallery loads with the
theme's bundle, draws the title bar from the theme's chrome, and leaves a window's frame and content box clear, so
the app page laid under a live window shows through; and the desktop wears the workspace's default theme.
"""

import shutil
import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import Page
from playwright.sync_api import Route
from workspace_themes.contract import BUILTIN_THEMES_DIRECTORY

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import find_free_port
from imbue.system_interface.testing import is_e2e_browser_installed
from imbue.system_interface.testing import is_server_answering
from imbue.system_interface.wsgi import make_threaded_server

_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "static"
# imbue/system_interface/test_theme_gallery.py -> the repo root
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[5]
_TRANSPARENT: Final[str] = "rgba(0, 0, 0, 0)"
_BUILTIN_THEMES: Final[tuple[str, ...]] = ("standard", "mac-classic", "windows-2000")

pytestmark = [
    pytest.mark.browser,
    pytest.mark.timeout(60),
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not (_STATIC_DIRECTORY / "theme-gallery.html").is_file(),
        reason="System interface frontend not built (run `cd system && npm run build`); skipping.",
    ),
]


@pytest.fixture
def themed_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The shell on a free port over a copy of the template's theme folders, answering its base URL."""
    repo_root = tmp_path / "repo"
    shutil.copytree(_REPO_ROOT / BUILTIN_THEMES_DIRECTORY, repo_root / BUILTIN_THEMES_DIRECTORY)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    registry = tmp_path / "apps.toml"
    write_registry(registry, registry_row_toml("files", f"http://127.0.0.1:{find_free_port()}", display_name="Files"))
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    state = build_test_state(
        config=Config(system_interface_host="127.0.0.1", system_interface_port=port),
        shell_state_directory=tmp_path / "shell-state",
        repo_root=repo_root,
        # Not beside the registry: on macOS a second watch of one folder in a process is refused and stays dead.
        agent_events_path=tmp_path / "mngr-events" / "events.jsonl",
    )
    server = make_threaded_server("127.0.0.1", port, create_application(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for(lambda: is_server_answering(base_url), timeout=10.0, poll_interval=0.1)
        state.shell.start()
        try:
            yield base_url
        finally:
            state.shell.stop()
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()


def _computed(page: Page, selector: str, property_name: str) -> str:
    return page.eval_on_selector(selector, f"(element) => getComputedStyle(element).{property_name}")


@pytest.mark.parametrize("theme_id", _BUILTIN_THEMES)
def test_the_gallery_wears_each_built_in_theme_and_leaves_the_page_under_a_window_visible(
    themed_shell: str, page: Page, theme_id: str
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.goto(f"{themed_shell}/theme-gallery?theme={theme_id}")
    page.wait_for_selector(f'[data-gallery-ready="{theme_id}"]')

    assert page.eval_on_selector("html", "(root) => root.getAttribute('data-ui-theme')") == theme_id
    bundles = page.eval_on_selector_all(
        "link[data-workspace-theme]", "(links) => links.map((link) => link.sheet !== null)"
    )
    assert bundles == ([] if theme_id == "standard" else [True])
    for part in ("window-frame", "window-content"):
        selector = f'[data-focused="true"] [data-part="{part}"]'
        assert _computed(page, selector, "backgroundColor") == _TRANSPARENT, (theme_id, part)
        assert _computed(page, selector, "backgroundImage") == "none", (theme_id, part)
    assert page.locator("[data-gallery-problems]").count() == 0
    assert errors == []


def test_classic_mac_puts_the_close_box_first_and_centres_the_title(themed_shell: str, page: Page) -> None:
    page.goto(f"{themed_shell}/theme-gallery?theme=mac-classic")
    page.wait_for_selector('[data-gallery-ready="mac-classic"]')

    controls = page.eval_on_selector_all(
        '[data-focused="true"] [data-part="window-control"]',
        "(buttons) => buttons.map((button) => button.getAttribute('data-control'))",
    )
    assert controls == ["close", "refresh", "menu", "minimize", "maximize"]
    assert _computed(page, '[data-focused="true"] [data-part="window-title"]', "position") == "absolute"
    assert page.locator('[data-focused="true"] [data-part="window-icon"]').count() == 0


def test_the_desktop_wears_the_workspace_default_theme(themed_shell: str, page: Page) -> None:
    chosen = page.request.post(f"{themed_shell}/api/themes/default", data={"theme": "windows-2000"})
    assert chosen.ok, (chosen.status, chosen.text())

    page.goto(themed_shell)

    page.wait_for_function("() => document.documentElement.getAttribute('data-ui-theme') === 'windows-2000'")
    # The bundle's own sheet parses before the files it imports load, so wait for the token itself to apply: the
    # Windows 2000 desktop blue.
    page.wait_for_function(
        """() => {
            const desktop = document.querySelector('[data-part="desktop"]');
            return desktop !== null && getComputedStyle(desktop).backgroundColor === "rgb(58, 110, 165)";
        }"""
    )


_PROBE_CONTROLS: Final[str] = (
    '<button id="bare">Go</button><button id="marked" data-part="button">Go</button>'
    '<input id="bare-field"><input id="marked-field" data-part="field">'
)
_KIT_PROBE_PAGE: Final[str] = (
    '<!doctype html><html><head><link rel="stylesheet" href="/_static/workspace_theme.css">'
    '<script src="/_static/workspace_theme.js"></script></head>'
    f"<body>{_PROBE_CONTROLS}</body></html>"
)
_NO_KIT_PROBE_PAGE: Final[str] = (
    '<!doctype html><html><head><link rel="stylesheet" href="/_static/themes/windows-2000/theme.css"></head>'
    f"<body>{_PROBE_CONTROLS}</body></html>"
)
_DRESSED_PROPERTIES: Final[tuple[str, ...]] = (
    "borderTopColor",
    "borderTopWidth",
    "backgroundColor",
    "boxShadow",
    "fontFamily",
    "paddingLeft",
)


def _serve_built_kit_file(kit_file: str) -> Callable[[Route], None]:
    return lambda route: route.fulfill(path=_STATIC_DIRECTORY / "_static" / kit_file)


def _dress_of(page: Page, element_id: str) -> dict[str, str]:
    return page.evaluate(
        "([id, properties]) => Object.fromEntries("
        "properties.map((name) => [name, getComputedStyle(document.getElementById(id))[name]]))",
        [element_id, list(_DRESSED_PROPERTIES)],
    )


def test_a_page_kit_page_dresses_its_bare_controls_as_their_parts_and_no_other_page_does(
    themed_shell: str, page: Page
) -> None:
    """A plain page that loads the page kit wears the theme on its bare <button> and <input> as on marked ones; the
    same theme stylesheet on a page without the kit (as on a built-in app) leaves bare controls alone."""
    page.add_init_script(
        "window.localStorage.setItem('workspace-theme', JSON.stringify({id: 'windows-2000', revision: ''}))"
    )
    page.route(
        f"{themed_shell}/kit-probe",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_KIT_PROBE_PAGE),
    )
    # The kit is the shell's build, which every plain app serves at /_static/ from its own origin; the shell does not.
    for kit_file in ("workspace_theme.css", "workspace_theme.js"):
        page.route(f"{themed_shell}/_static/{kit_file}", _serve_built_kit_file(kit_file))
    page.route(
        f"{themed_shell}/no-kit-probe",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_NO_KIT_PROBE_PAGE),
    )
    is_dressed = "() => getComputedStyle(document.getElementById('marked')).fontFamily.includes('Tahoma')"

    page.goto(f"{themed_shell}/kit-probe")
    page.wait_for_function(is_dressed)
    assert _dress_of(page, "bare") == _dress_of(page, "marked")
    assert _dress_of(page, "bare-field") == _dress_of(page, "marked-field")

    page.goto(f"{themed_shell}/no-kit-probe")
    page.wait_for_function(is_dressed)
    assert _dress_of(page, "bare") != _dress_of(page, "marked")
