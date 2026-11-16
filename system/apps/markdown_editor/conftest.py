"""A home folder of the editor's own for each test, and the real server over it for the browser tests.

Each test gets a throwaway home with a ``workspace`` folder in it and a history folder beside it, so nothing it opens,
saves or lists is outside its own temporary directory.
"""

import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from werkzeug.serving import make_server

from markdown_editor_app.files import MarkdownFinder
from markdown_editor_app.files import WorkspaceFiles
from markdown_editor_app.server import STATIC_DIR
from markdown_editor_app.server import create_app


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """The workspace folder inside the throwaway home, where relative paths and the file list start."""
    folder = tmp_path / "home" / "workspace"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def history_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "history"


@pytest.fixture
def files(workspace: Path, history_dir: Path) -> WorkspaceFiles:
    return WorkspaceFiles(root=workspace.parent, search_root=workspace, history_dir=history_dir)


@pytest.fixture
def static_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text('<!doctype html><title>Markdown Editor</title><div id="editor"></div>')
    (directory / "assets" / "index-AbCdEf12.js").write_text("console.log('bundle');")
    (directory / "assets" / "logo.svg").write_text("<svg/>")
    return directory


@pytest.fixture
def client(files: WorkspaceFiles, static_directory: Path) -> FlaskClient:
    return create_app(files, MarkdownFinder(files), static_directory).test_client()


@pytest.fixture
def editor_origin(files: WorkspaceFiles) -> Iterator[str]:
    """The origin of the real server over the throwaway home, serving the built page."""
    if not (STATIC_DIR / "index.html").is_file():
        pytest.fail("The Markdown Editor is not built: run `npm run build` at system/ first")
    server = make_server("127.0.0.1", 0, create_app(files, MarkdownFinder(files), STATIC_DIR), threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="markdown-editor-test-server", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


OpenEditorPage = Callable[..., Page]


@pytest.fixture
def open_editor_page(module_browser: Browser, editor_origin: str) -> Iterator[OpenEditorPage]:
    """Open a path of the server in a fresh page, desktop-sized unless a size is given."""
    contexts: list[BrowserContext] = []

    def open_page(path_and_query: str, width: int = 1200, height: int = 900) -> Page:
        context = module_browser.new_context(viewport={"width": width, "height": height})
        contexts.append(context)
        page = context.new_page()
        page.goto(editor_origin + path_and_query)
        return page

    yield open_page
    for context in contexts:
        context.close()
