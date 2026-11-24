"""Shared fixtures for the Disk Space app's tests.

Nothing here reads or writes the app's real data. Importing any of the app's modules builds ``store.DATA_DIR`` from
``DISK_SPACE_DATA_DIR``, so this file points that at a scratch directory before the first test module imports the
app, and the ``data_dir`` fixture gives each test an empty one. The browser fixture launches the workspace's browser
(Fortress, installed before any agent starts); this app's suite is its own pytest root, outside the repo-root
conftest that offers one.
"""

import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright
from werkzeug.serving import make_server

_SCRATCH_ROOT = Path(tempfile.mkdtemp(prefix="disk-space-tests-"))
os.environ["DISK_SPACE_DATA_DIR"] = str(_SCRATCH_ROOT / "data")

from disk_space import (  # noqa: E402  (the data directory above must be set first)
    runner,
    store,
)

FORTRESS_CHROMIUM = Path("/opt/fortress/tilion-fortress/tilion")


@pytest.fixture(autouse=True)
def data_dir() -> Iterator[Path]:
    """An empty data directory for each test, so no test sees another's settings, history or measurements."""
    shutil.rmtree(store.DATA_DIR, ignore_errors=True)
    store.DATA_DIR.mkdir(parents=True)
    try:
        yield store.DATA_DIR
    finally:
        shutil.rmtree(store.DATA_DIR, ignore_errors=True)


@pytest.fixture
def served_origin() -> Iterator[str]:
    """The app on a free loopback port over this test's state, for the browser to drive."""
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def module_browser() -> Iterator[Browser]:
    """One browser for a test module: Fortress, the workspace's Chromium, by its own path."""
    launch_args = (
        {"executable_path": str(FORTRESS_CHROMIUM)}
        if FORTRESS_CHROMIUM.exists()
        else {}
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)
        try:
            yield browser
        finally:
            browser.close()
