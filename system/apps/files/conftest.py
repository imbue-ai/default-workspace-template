"""A stand-in for dufs serving the vendored frontend, for the file viewer's browser tests.

It answers the reads `dufs --allow-all` answers for a small fixed tree: a folder's listing
and a file's editor page are `assets/index.html` with dufs's two placeholders filled (the
listing JSON base64-encoded, as dufs 0.46 encodes it), a file's plain GET is its content, and
the assets are served under dufs's assets prefix. Writes never reach it: every page's `fetch`
is stubbed to record the non-GET requests dufs makes and answer them as dufs would.
"""

import base64
import http.server
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

ASSETS_DIRECTORY = Path(__file__).parent / "assets"
ASSETS_PREFIX = "/__dufs_v0.46.0__/"
WORKSPACE = "/home/user/workspace"

_SEPT_21 = 1790000000000
_SEPT_27 = 1790500000000
_OCT_3 = 1791000000000

# Each folder's listing in the order dufs sends it (it sorts server-side).
LISTINGS: dict[str, list[dict[str, Any]]] = {
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


@pytest.fixture
def dufs_origin() -> Iterator[str]:
    """The origin of a running dufs stand-in serving the vendored frontend."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DufsStandInHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def fetch_calls() -> list[dict[str, Any]]:
    """The non-GET requests the pages' dufs frontend made, in order."""
    return []


@pytest.fixture
def open_files_page(
    browser: Browser, dufs_origin: str, fetch_calls: list[dict[str, Any]]
) -> Iterator[Callable[[str, int, int], Page]]:
    """Open a path of the dufs stand-in in a fresh page of the given viewport size.

    Times render in UTC, so a listing's timestamps read the same on every machine.
    """
    contexts: list[BrowserContext] = []

    def record_fetch(call: dict[str, Any]) -> None:
        fetch_calls.append(call)

    def open_page(path: str, width: int, height: int) -> Page:
        context = browser.new_context(
            viewport={"width": width, "height": height}, timezone_id="UTC"
        )
        contexts.append(context)
        context.expose_function("recordFetch", record_fetch)
        context.add_init_script(_FETCH_STUB)
        page = context.new_page()
        page.goto(dufs_origin + path)
        return page

    yield open_page
    for context in contexts:
        context.close()
