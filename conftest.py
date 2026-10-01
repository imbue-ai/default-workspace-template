from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, sync_playwright

# The workspace's browser engine is Fortress (a stealth-patched Chromium fork)
# provisioned by env-converge before any agent starts. Playwright's browser-cache
# lookup only auto-discovers builds Playwright downloaded itself, so a launch has
# to name this binary explicitly. Every suite collected under the repo root
# inherits this override, so `module_browser` drives Fortress with no per-app
# setup. The `chat` and `system_interface` apps are NOT under it: the root pytest
# config ignores them and each runs from its own directory.
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")


@pytest.fixture(scope="session")
def browser_type_launch_args(
    browser_type_launch_args: dict[str, Any],
) -> dict[str, Any]:
    # Without Fortress (CI, a developer laptop) leave the launch args untouched
    # so Playwright falls through to its own managed browser. Never skip on
    # browser absence: a browser that cannot launch must fail the run loudly.
    if not FORTRESS_CHROMIUM_PATH.exists():
        return browser_type_launch_args
    return {**browser_type_launch_args, "executable_path": str(FORTRESS_CHROMIUM_PATH)}


@pytest.fixture(scope="module")
def module_browser(browser_type_launch_args: dict[str, Any]) -> Iterator[Browser]:
    """A Chromium for one test module, closed with its Playwright when the module ends.

    Use it rather than pytest-playwright's `browser` or `page`: those live for the
    whole session, and their sync Playwright keeps an asyncio loop running in the
    worker's main thread until then, so every later `asyncio.run` in the worker fails.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**browser_type_launch_args)
        try:
            yield browser
        finally:
            browser.close()
