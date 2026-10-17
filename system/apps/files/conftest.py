"""A stand-in for the Files server over a temporary folder, serving the Files build, for Files' browser tests.

It answers what `dufs --allow-all --assets static/ <root>` answers, as far as the frontend asks: a folder's page and a
file's `?view` / `?edit` page are `static/index.html` with dufs's two placeholders filled (the page data
base64-encoded, as dufs 0.46 encodes it), `?json` is a folder's listing, `?q=` a search under it, a file's plain GET
its bytes (a `Range` honoured), the build is served under dufs's assets prefix, and PUT, MKCOL, MOVE, and DELETE
change the folder for real, so a test reads the outcome off the disk. ``/__files/api/connections`` answers with
what a test puts in the ``connections`` fixture and records what is asked of it.
"""

import base64
import http.server
import json
import shutil
import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page

STATIC_DIRECTORY = Path(__file__).parent / "static"
ASSETS_PREFIX = "/__dufs_v0.46.0__/"

# dufs offers a file's text for editing below this size, when it decodes.
_EDITABLE_LIMIT = 4 * 1024 * 1024

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def _path_item(path: Path, name: str) -> dict[str, Any]:
    is_dir = path.is_dir()
    return {
        "path_type": "Dir" if is_dir else "File",
        "name": name,
        "mtime": int(path.stat().st_mtime * 1000),
        "size": len(list(path.iterdir())) if is_dir else path.stat().st_size,
    }


def _is_text(path: Path) -> bool:
    if path.stat().st_size > _EDITABLE_LIMIT:
        return False
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class Connections:
    """What the stand-in says is shared from the user's computers, and the requests for more it was sent."""

    def __init__(self) -> None:
        self.listed: dict[str, Any] = {"devices": [], "shares": []}
        self.requests: list[dict[str, Any]] = []


def _make_handler(root: Path, connections: Connections) -> type[http.server.BaseHTTPRequestHandler]:
    def on_disk(url_path: str) -> Path:
        return root / unquote(url_path).lstrip("/")

    def page_data(url_path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        target = on_disk(url_path)
        common = {
            "href": unquote(url_path),
            "uri_prefix": "/",
            "allow_upload": True,
            "allow_delete": True,
            "auth": False,
            "user": None,
        }
        if target.is_file():
            kind = "Edit" if "edit" in query else "View"
            return {**common, "kind": kind, "editable": _is_text(target)}
        if "q" in query:
            needle = query["q"][0].lower()
            paths = [
                _path_item(found, str(found.relative_to(target)))
                for found in sorted(target.rglob("*"))
                if needle in found.name.lower()
            ]
        else:
            paths = [_path_item(child, child.name) for child in sorted(target.iterdir())] if target.is_dir() else []
        return {
            **common,
            "kind": "Index",
            "allow_search": True,
            "allow_archive": True,
            "dir_exists": target.is_dir(),
            "paths": paths,
        }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/__files/api/connections":
                self._send(200, json.dumps(connections.listed).encode(), "application/json")
                return
            query = parse_qs(url.query, keep_blank_values=True)
            if url.path.startswith(ASSETS_PREFIX):
                asset = STATIC_DIRECTORY / url.path.removeprefix(ASSETS_PREFIX)
                if asset.is_file():
                    self._send(
                        200,
                        asset.read_bytes(),
                        _CONTENT_TYPES.get(asset.suffix, "application/octet-stream"),
                    )
                else:
                    self._send(404, b"Not Found", "text/plain")
                return
            target = on_disk(url.path)
            if "json" in query:
                self._send(
                    200,
                    json.dumps(page_data(url.path, query)).encode(),
                    "application/json",
                )
                return
            if target.is_file() and "view" not in query and "edit" not in query:
                self._send_file(target)
                return
            if not target.exists() and not url.path.endswith("/"):
                self._send(
                    404,
                    (STATIC_DIRECTORY / "404.html").read_bytes(),
                    _CONTENT_TYPES[".html"],
                )
                return
            data = base64.b64encode(json.dumps(page_data(url.path, query)).encode()).decode()
            page = (
                (STATIC_DIRECTORY / "index.html")
                .read_text()
                .replace("__ASSETS_PREFIX__", ASSETS_PREFIX)
                .replace("__INDEX_DATA__", data)
            )
            self._send(200, page.encode(), _CONTENT_TYPES[".html"])

        def do_HEAD(self) -> None:
            url = urlsplit(self.path)
            # dufs answers any path ending in a slash as a folder, there or not.
            found = url.path.endswith("/") or on_disk(url.path).exists()
            self.send_response(200 if found else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            connections.requests.append(json.loads(self.rfile.read(length)))
            self._send(200, b'{"requested": true}', "application/json")

        def do_PUT(self) -> None:
            target = on_disk(urlsplit(self.path).path)
            target.parent.mkdir(parents=True, exist_ok=True)
            length = int(self.headers.get("Content-Length") or 0)
            target.write_bytes(self.rfile.read(length))
            self._send(201, b"", "text/plain")

        def do_MKCOL(self) -> None:
            on_disk(urlsplit(self.path).path).mkdir(parents=True)
            self._send(201, b"", "text/plain")

        def do_MOVE(self) -> None:
            source = on_disk(urlsplit(self.path).path)
            destination = on_disk(urlsplit(self.headers["Destination"]).path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            self._send(201, b"", "text/plain")

        def do_DELETE(self) -> None:
            target = on_disk(urlsplit(self.path).path)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            self._send(200, b"", "text/plain")

        def _send_file(self, target: Path) -> None:
            body = target.read_bytes()
            content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            ranged = self.headers.get("Range")
            if ranged is not None and ranged.startswith("bytes="):
                start, _, end = ranged.removeprefix("bytes=").partition("-")
                self._send(206, body[int(start) : int(end) + 1], content_type)
                return
            self._send(200, body, content_type)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


@pytest.fixture
def files_root(tmp_path: Path) -> Path:
    """The folder the stand-in serves as the filesystem root: a small tree of folders, text, an image, and a dotfile."""
    root = tmp_path / "root"
    (root / "projects" / "app").mkdir(parents=True)
    (root / "projects" / "app" / "main.py").write_text('print("hi")\n')
    (root / "photos").mkdir()
    (root / "photos" / "sunset.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
    )
    (root / "notes.txt").write_text("first line\nsecond line\n")
    (root / "README.md").write_text("# hello\n")
    (root / ".env").write_text("SECRET=1\n")
    return root


@pytest.fixture
def connections() -> Connections:
    return Connections()


@pytest.fixture
def files_origin(files_root: Path, connections: Connections) -> Iterator[str]:
    """The origin of a running dufs stand-in over `files_root`."""
    if not (STATIC_DIRECTORY / "index.html").is_file():
        pytest.fail("Files is not built: run `npm run build` at system/ first")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(files_root, connections))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


OpenFilesPage = Callable[..., Page]


@pytest.fixture
def open_files_page(module_browser: Browser, files_origin: str) -> Iterator[OpenFilesPage]:
    """Open a path of the stand-in in a fresh page, desktop-sized unless a size is given. Times render in UTC."""
    contexts: list[BrowserContext] = []

    def open_page(path: str, width: int = 1200, height: int = 760) -> Page:
        context = module_browser.new_context(viewport={"width": width, "height": height}, timezone_id="UTC")
        contexts.append(context)
        page = context.new_page()
        page.goto(files_origin + quote(path, safe="/?=&"))
        return page

    yield open_page
    for context in contexts:
        context.close()
