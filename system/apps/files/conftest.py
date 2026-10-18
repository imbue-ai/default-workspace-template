"""The Files server over a temporary folder, serving the Files build, for Files' browser tests.

It is the real server, with the temporary folder as its root, so a test reads the outcome of every change off the
disk. ``/__files/api/connections`` alone is a stand-in: it answers with what a test puts in the ``connections``
fixture, since what is shared from the user's computers comes from outside the workspace.
"""

import base64
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from flask import Response
from flask import jsonify
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page

from files_app.server import create_app
from files_app.serving import serve_in_background

STATIC_DIRECTORY = Path(__file__).parent / "static"


class Connections:
    """What the stand-in says is shared from the user's computers."""

    def __init__(self) -> None:
        self.listed: dict[str, Any] = {"devices": [], "shares": []}


@pytest.fixture
def files_root(tmp_path: Path) -> Path:
    """The folder the server serves as the filesystem root: a small tree of folders, text, an image, and a dotfile."""
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
    """The origin of the Files server over `files_root`."""
    if not (STATIC_DIRECTORY / "index.html").is_file():
        pytest.fail("Files is not built: run `npm run build` at system/ first")
    app = create_app(gateway=None, static_directory=STATIC_DIRECTORY, root=files_root)

    def listed_connections() -> Response:
        return jsonify(connections.listed)

    app.view_functions["connections"] = listed_connections
    with serve_in_background("127.0.0.1", 0, app) as server:
        yield f"http://127.0.0.1:{server.port}"


OpenFilesPage = Callable[..., Page]


@pytest.fixture
def open_files_page(module_browser: Browser, files_origin: str) -> Iterator[OpenFilesPage]:
    """Open a path of the server in a fresh page, desktop-sized unless a size is given. Times render in UTC."""
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
