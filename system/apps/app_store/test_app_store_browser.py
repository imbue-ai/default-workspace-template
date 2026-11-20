"""The built page in a real browser, over a throwaway server whose installs are faked: the catalog renders as tiles,
Install reaches the engine and the page follows the install to its end, and an app's detail page shows its icon on
its own."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from flask import Flask
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app_store.command_finder import Screenshot
from app_store.command_finder import take_screenshot
from app_store.engine import InstallEngine
from app_store.pages import build_pages_blueprint
from app_store.records import InstallStatus
from app_store.template_sizes import TemplateSizeStore
from app_store.testing import FakeRepositorySizeFetcher
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import build_test_finder
from getting_started.template_catalog import TemplateCatalogStore
from getting_started.testing import FakeTemplateCatalogFetcher

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]

_STATIC_DIRECTORY = Path(__file__).parent / "src" / "app_store" / "static"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def page(module_browser: Browser) -> Iterator[Page]:
    """A fresh desktop-sized page."""
    context = module_browser.new_context(viewport={"width": 1200, "height": 800})
    try:
        yield context.new_page()
    finally:
        context.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[str, InstallEngine]]:
    """The page and its API on a free loopback port, with the system faked."""
    assert (_STATIC_DIRECTORY / "index.html").is_file(), "build the frontend first: cd system && npm run build"
    engine = build_test_engine(tmp_path)
    adder = build_test_adder(engine, tmp_path)
    app = Flask("app-store-browser-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            _STATIC_DIRECTORY,
            engine,
            adder,
            build_test_finder(engine, tmp_path),
            TemplateCatalogStore(
                catalog_url="", cache_path=tmp_path / "catalog.json", fetcher=FakeTemplateCatalogFetcher()
            ),
            TemplateSizeStore(cache_path=tmp_path / "sizes.json", fetcher=FakeRepositorySizeFetcher()),
            tmp_path / "app_contract.js",
        )
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", engine
    finally:
        server.shutdown()


def test_the_catalog_renders_and_an_install_runs_from_the_page_to_the_end(
    page: Page, store: tuple[str, InstallEngine]
) -> None:
    url, engine = store
    page.goto(url)
    tiles = page.locator("[data-app]")
    expect(tiles).to_have_count(len(engine.catalog.apps))
    datasette = page.locator('[data-app="datasette"]')
    expect(datasette).to_contain_text("Datasette")

    installed = page.locator('[data-stat="installed"] > span').first
    expect(installed).to_have_text("0")

    datasette.locator(".app-store-install").click()
    dialog = page.locator('[data-dialog="install"]')
    expect(dialog).to_contain_text("Install Datasette?")
    dialog.locator(".app-store-confirm-install").click()
    expect(dialog).to_have_count(0)
    # The tile follows the queued job instead of offering Install.
    expect(datasette.locator(".app-store-install")).to_have_count(0)
    record = engine.records.read().find("datasette")
    assert record is not None and record.status is InstallStatus.QUEUED

    engine.run_next_job()
    expect(installed).to_have_text("1", timeout=15_000)


def test_an_apps_detail_page_shows_its_icon_on_its_own(page: Page, store: tuple[str, InstallEngine]) -> None:
    url, _engine = store
    page.goto(url)
    page.locator('[data-app="marimo"]').click()
    detail = page.locator('[data-app-detail="marimo"]')
    expect(detail).to_be_visible()
    holder = detail.locator("[data-detail-icon]")
    icon = holder.locator("img")
    expect(icon).to_have_attribute("src", "/api/apps/marimo/icon.svg")
    expect(icon).to_have_js_property("complete", True)
    assert icon.evaluate("image => image.naturalWidth") > 0
    assert holder.evaluate("element => getComputedStyle(element).borderTopWidth") == "0px"


class _RedPage(BaseHTTPRequestHandler):
    """A red page, sent with the server's ``framing_headers``."""

    framing_headers: tuple[tuple[str, str], ...] = ()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        for name, value in self.framing_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(b"<!doctype html><body style='margin:0;background:#c00'><h1>Hello</h1></body>")

    def log_message(self, format: str, *args: object) -> None:
        return


def _shoot(framing_headers: tuple[tuple[str, str], ...]) -> Screenshot:
    handler = type("Handler", (_RedPage,), {"framing_headers": framing_headers})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return take_screenshot(_REPO_ROOT, server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("framing_headers", "is_blocked"),
    [
        ((), False),
        # What Jupyter sends once told to allow any site: a frame page on a blank or public address would be refused.
        ((("Content-Security-Policy", "frame-ancestors *"),), False),
        ((("X-Frame-Options", "DENY"),), True),
        ((("Content-Security-Policy", "frame-ancestors 'self'"),), True),
    ],
)
def test_a_found_apps_page_is_screenshot_inside_a_frame_as_the_workspace_shows_it(
    framing_headers: tuple[tuple[str, str], ...], is_blocked: bool
) -> None:
    shot = _shoot(framing_headers)
    assert shot.image is not None and shot.image.startswith(b"\x89PNG")
    assert (shot.blocked_reason is not None) is is_blocked
    assert not list((_REPO_ROOT / "data" / ".state" / "app-store").glob("screenshot-*.png"))
