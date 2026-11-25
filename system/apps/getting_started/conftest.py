"""The browser for Getting Started's browser tests. They run under the repo root's pytest configuration (the root suite)
and under this app's own (a run naming a path inside the app), and the root conftest.py, whose ``module_browser`` the
root suite requires in place of pytest-playwright's session fixtures, is loaded only under the root's; these give both
runs the same browser."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import sync_playwright

# The workspace's browser (AGENTS.md): Playwright finds only the builds it downloaded itself, so a launch names it.
FORTRESS_CHROMIUM_PATH: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    if not FORTRESS_CHROMIUM_PATH.exists():
        return browser_type_launch_args
    return {**browser_type_launch_args, "executable_path": str(FORTRESS_CHROMIUM_PATH)}


@pytest.fixture(scope="module")
def module_browser(browser_type_launch_args: dict[str, Any]) -> Iterator[Browser]:
    """A Chromium for one test module, closed with its Playwright when the module ends."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**browser_type_launch_args)
        try:
            yield browser
        finally:
            browser.close()
