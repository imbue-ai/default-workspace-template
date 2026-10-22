import threading
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page

from app_manifest.registry import SHELL_MODULE_PATHS
from skills_app.mock_skill_index_searcher_test import RecordingSkillIndexSearcher
from skills_app.server import STATIC_DIR
from skills_app.server import create_app
from skills_app.testing import LocalSkillIndex
from skills_app.testing import serve_in_background
from skills_app.testing import shell_stand_in_app
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import init_workspace_repo


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A checkout whose template commit holds two skills, assist and notify-user."""
    return init_workspace_repo(tmp_path / "workspace", ("assist", "notify-user"))


@pytest.fixture
def searcher() -> RecordingSkillIndexSearcher:
    """An online index that answers nothing until a test gives it matches."""
    return RecordingSkillIndexSearcher()


@pytest.fixture
def client(workspace_root: Path, searcher: RecordingSkillIndexSearcher) -> FlaskClient:
    return create_app(build_catalog(workspace_root), STATIC_DIR, searcher).test_client()


@pytest.fixture
def local_skill_index() -> Iterator[LocalSkillIndex]:
    index = LocalSkillIndex()
    thread = threading.Thread(target=index.server.serve_forever, daemon=True)
    thread.start()
    yield index
    index.server.shutdown()
    index.server.server_close()


@pytest.fixture
def skills_origin(workspace_root: Path, searcher: RecordingSkillIndexSearcher) -> Iterator[str]:
    """The Skills app over the test workspace, served on a free loopback port for a browser to load."""
    # The page imports the shell's built modules, which the app serves from the repo root; a fresh worktree lacks them.
    missing = [path for path in SHELL_MODULE_PATHS if not path.is_file()]
    if missing:
        pytest.fail(
            f"The shell's modules are not built ({missing[0]}): run `npm ci && npm run build` in system/ first"
        )
    with serve_in_background(create_app(build_catalog(workspace_root), STATIC_DIR, searcher)) as origin:
        yield origin


@pytest.fixture
def shell_origin(skills_origin: str) -> Iterator[str]:
    """A stand-in for the workspace shell on its own port, framing the Skills page at ``/?path=<path>``."""
    with serve_in_background(shell_stand_in_app(skills_origin)) as origin:
        yield origin


@pytest.fixture
def new_browser_page(module_browser: Browser) -> Iterator[Callable[[int, int], Page]]:
    """Opens a blank page in a browser context of its own at a window size; every context is closed after the test."""
    contexts: list[BrowserContext] = []

    def new_page(width: int, height: int) -> Page:
        context = module_browser.new_context(viewport={"width": width, "height": height})
        contexts.append(context)
        return context.new_page()

    yield new_page
    for context in contexts:
        context.close()


@pytest.fixture
def open_skills_page(
    new_browser_page: Callable[[int, int], Page], skills_origin: str
) -> Callable[[str, int, int], Page]:
    """Opens the Skills page at a path, alone in a browser tab, at a window size."""

    def open_page(path: str, width: int, height: int) -> Page:
        page = new_browser_page(width, height)
        page.goto(skills_origin + path)
        return page

    return open_page


@pytest.fixture
def open_framed_skills_page(new_browser_page: Callable[[int, int], Page], shell_origin: str) -> Callable[[str], Page]:
    """Opens the Skills page at a path framed by the shell stand-in; the shell's page is returned."""

    def open_page(path: str) -> Page:
        page = new_browser_page(1200, 800)
        page.goto(f"{shell_origin}/?path={path}")
        return page

    return open_page
