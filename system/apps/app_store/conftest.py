"""The browser the App Store's browser tests drive. This suite runs under its own pytest config, so the repo root's
conftest (and its ``module_browser``) is not loaded; this is the same fixture, launching Fortress, the workspace's
browser, when it is installed and Playwright's own Chromium otherwise. A browser that cannot launch fails the run."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import sync_playwright

FORTRESS_CHROMIUM_PATH: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")


@pytest.fixture(scope="module")
def module_browser() -> Iterator[Browser]:
    """A Chromium for one test module, closed with its Playwright when the module ends."""
    launch_args: dict[str, Any] = {}
    if FORTRESS_CHROMIUM_PATH.exists():
        launch_args["executable_path"] = str(FORTRESS_CHROMIUM_PATH)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)
        try:
            yield browser
        finally:
            browser.close()
