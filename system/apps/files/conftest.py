"""A stand-in for dufs serving the vendored frontend, for the file viewer's browser tests.

It answers the reads `dufs --allow-all` answers for a small fixed tree: a folder's listing
and a file's editor page are `assets/index.html` with dufs's two placeholders filled (the
listing JSON base64-encoded, as dufs 0.46 encodes it), a file's plain GET is its content, and
the assets are served under dufs's assets prefix, and the shell's built app contract at its path in the workspace,
which is where the viewer's `shell.js` imports it from. Writes never reach it: every page's `fetch`
is stubbed to record the non-GET requests dufs makes and answer them as dufs would, unless a test
set ``window.fetchRefusals[<method>]`` to ``{status, body}`` (answered so, a GET included) or to
``"no answer"`` (the request rejects, as one nothing answered does).
"""

import base64
import http.server
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import pytest
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.testing import (
    SHELL_HANDSHAKE_COUNTER_SCRIPT,
    built_app_contract_source,
    shell_stand_in_page,
)
from playwright.sync_api import Browser, BrowserContext, Frame, Page

ASSETS_DIRECTORY = Path(__file__).parent / "assets"
ASSETS_PREFIX = "/__dufs_v0.46.0__/"
WORKSPACE = "/home/user/workspace"
_APP_CONTRACT_PATH = f"{WORKSPACE}/{SHELL_APP_CONTRACT_PATH}"

_SEPT_21 = 1790000000000
_SEPT_27 = 1790500000000
_OCT_3 = 1791000000000

# A folder deep enough that a file's view page in it is longer than a window's path can be.
LONG_FOLDER = f"{WORKSPACE}/{'deep-folder-' * 180}"

# Each folder's listing in the order dufs sends it (it sorts server-side).
LISTINGS: dict[str, list[dict[str, Any]]] = {
    LONG_FOLDER: [
        {"path_type": "File", "name": "far.txt", "mtime": _OCT_3, "size": 12},
    ],
    WORKSPACE: [
        {"path_type": "Dir", "name": ".git", "mtime": _SEPT_21, "size": 12},
        {"path_type": "Dir", "name": "data", "mtime": _SEPT_27, "size": 3},
        {"path_type": "File", "name": ".env", "mtime": _SEPT_21, "size": 96},
        {"path_type": "File", "name": "README.md", "mtime": _OCT_3, "size": 1289},
        {"path_type": "File", "name": "notes.txt", "mtime": _SEPT_27, "size": 5120},
    ],
}

# What a search of the workspace folder finds: names are paths relative to the folder searched.
SEARCH_RESULTS: list[dict[str, Any]] = [
    {"path_type": "File", "name": ".git/notes-ref", "mtime": _SEPT_21, "size": 41},
    {
        "path_type": "File",
        "name": "data/notes/ideas.md",
        "mtime": _SEPT_27,
        "size": 611,
    },
    {"path_type": "File", "name": "notes.txt", "mtime": _SEPT_27, "size": 5120},
]

FILES: dict[str, str] = {
    f"{WORKSPACE}/README.md": "# workspace\n",
    f"{WORKSPACE}/notes.txt": "notes\n",
}

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "application/javascript; charset=utf-8",
}

_FETCH_STUB = """
(() => {
  const pageFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const refusal = (window.fetchRefusals || {})[method];
    if (refusal === "no answer") throw new TypeError("Failed to fetch");
    if (refusal !== undefined) return new Response(refusal.body, { status: refusal.status });
    if (method === "GET") return pageFetch(input, init);
    const headers = init.headers || {};
    await window.recordFetch({
      method,
      url: new URL(String(input), location.href).href,
      destination: headers.Destination ?? null,
      body: typeof init.body === "string" ? init.body : null,
    });
    // HEAD is dufs's does-the-destination-exist probe before a move: nothing is there.
    return new Response(null, { status: method === "HEAD" ? 404 : 201 });
  };
})();
"""


def _index_data(path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    href = "/" + path.strip("/")
    common = {
        "href": href,
        "uri_prefix": "/",
        "allow_upload": True,
        "allow_delete": True,
        "auth": False,
        "user": None,
    }
    if href in FILES:
        kind = "View" if "view" in query else "Edit"
        return {**common, "kind": kind, "editable": True}
    if "q" in query:
        paths = SEARCH_RESULTS if href == WORKSPACE else []
    else:
        paths = LISTINGS.get(href, [])
    return {
        **common,
        "kind": "Index",
        "allow_search": True,
        "allow_archive": True,
        "dir_exists": True,
        "paths": paths,
    }


class _DufsStandInHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query, keep_blank_values=True)
        if path.startswith(ASSETS_PREFIX):
            asset = ASSETS_DIRECTORY / path.removeprefix(ASSETS_PREFIX)
            if not asset.is_file():
                self.send_error(404)
                return
            self._send(
                asset.read_bytes(),
                _CONTENT_TYPES.get(asset.suffix, "application/octet-stream"),
            )
            return
        if path == _APP_CONTRACT_PATH and SHELL_APP_CONTRACT_PATH.is_file():
            self._send(SHELL_APP_CONTRACT_PATH.read_bytes(), _CONTENT_TYPES[".js"])
            return
        if path in FILES and "edit" not in query and "view" not in query:
            self._send(FILES[path].encode(), "text/plain; charset=utf-8")
            return
        data = json.dumps(_index_data(path, query)).encode()
        page = (
            (ASSETS_DIRECTORY / "index.html")
            .read_text()
            .replace("__ASSETS_PREFIX__", ASSETS_PREFIX)
            .replace("__INDEX_DATA__", base64.b64encode(data).decode())
        )
        self._send(page.encode(), _CONTENT_TYPES[".html"])

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _ShellStandInHandler(http.server.BaseHTTPRequestHandler):
    """Serves ``shell_stand_in_page`` for the ``frame`` and ``can_report_bug`` its query names."""

    def do_GET(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        page = shell_stand_in_page(query["frame"][0], query["can_report_bug"][0] == "true")
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES[".html"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _loopback_server(
    handler: type[http.server.BaseHTTPRequestHandler],
) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def dufs_origin() -> Iterator[str]:
    """The origin of a running dufs stand-in serving the vendored frontend."""
    with _loopback_server(_DufsStandInHandler) as origin:
        yield origin


@pytest.fixture
def shell_stand_in_origin() -> Iterator[str]:
    """The origin of a server of shell stand-in pages; a real one, on loopback like the dufs stand-in, since a
    browser refuses a loopback frame inside a page it cannot place on the local network."""
    with _loopback_server(_ShellStandInHandler) as origin:
        yield origin


@pytest.fixture
def long_folder() -> str:
    """A folder of the stand-in's whose files' view pages are longer than a window's path can be."""
    return LONG_FOLDER


@pytest.fixture
def fetch_calls() -> list[dict[str, Any]]:
    """The non-GET requests the pages' dufs frontend made, in order."""
    return []


@pytest.fixture
def new_files_context(
    module_browser: Browser, fetch_calls: list[dict[str, Any]]
) -> Iterator[Callable[[int, int], BrowserContext]]:
    """A fresh browser context of the given viewport size whose pages' writes are stubbed into ``fetch_calls``.

    Times render in UTC, so a listing's timestamps read the same on every machine.
    """
    contexts: list[BrowserContext] = []

    def record_fetch(call: dict[str, Any]) -> None:
        fetch_calls.append(call)

    def new_context(width: int, height: int) -> BrowserContext:
        context = module_browser.new_context(
            viewport={"width": width, "height": height}, timezone_id="UTC"
        )
        contexts.append(context)
        context.expose_function("recordFetch", record_fetch)
        context.add_init_script(_FETCH_STUB)
        return context

    yield new_context
    for context in contexts:
        context.close()


@pytest.fixture
def open_files_page(
    new_files_context: Callable[[int, int], BrowserContext], dufs_origin: str
) -> Callable[[str, int, int], Page]:
    """Open a path of the dufs stand-in in a fresh page of the given viewport size."""

    def open_page(path: str, width: int, height: int) -> Page:
        page = new_files_context(width, height).new_page()
        page.goto(dufs_origin + path)
        return page

    return open_page


@pytest.fixture
def open_framed_files_page(
    new_files_context: Callable[[int, int], BrowserContext],
    dufs_origin: str,
    shell_stand_in_origin: str,
) -> Callable[[str, bool], tuple[Page, Frame]]:
    """Open a path of the dufs stand-in in a stand-in for the shell, which greets it saying whether Imbue Studio opens
    a bug report; answers the stand-in's page and the viewer's frame once the viewer has had its handshake."""

    def open_page(path: str, can_report_bug: bool) -> tuple[Page, Frame]:
        built_app_contract_source()
        frame_url = dufs_origin + path
        query = urlencode({"frame": frame_url, "can_report_bug": json.dumps(can_report_bug)})
        context = new_files_context(1280, 800)
        context.add_init_script(SHELL_HANDSHAKE_COUNTER_SCRIPT)
        page = context.new_page()
        page.goto(f"{shell_stand_in_origin}/?{query}")
        frame = page.frame(url=frame_url)
        assert frame is not None
        frame.wait_for_function("window.shellHandshakes > 0")
        return page, frame

    return open_page
