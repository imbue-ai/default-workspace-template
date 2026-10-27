from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NoReturn

import pytest
from playwright.sync_api import Browser, BrowserContext, ViewportSize, sync_playwright

# The browser the suites launch, both provisioned by env-converge before any agent
# starts (system/scripts/env.d/1000-playwright-fortress.sh): Playwright's headless
# shell, installed for the tests alone because a browser suite runs about a third
# faster in about a third less memory on it, else Fortress (the workspace's
# stealth-patched Chromium fork). A launch names the binary explicitly, so every
# suite collected under the repo root inherits this override and `module_browser`
# needs no per-app setup. The `chat` and `system_interface` apps are NOT under it:
# the root pytest config ignores them and each runs from its own directory.
TEST_HEADLESS_SHELL_PATH = Path("/opt/playwright-test-browsers/headless-shell")
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")

# The phone the plan's e2e sizes name (an iPhone 15 in portrait), with touch.
_PHONE_VIEWPORT: Final[ViewportSize] = {"width": 393, "height": 852}


@pytest.fixture(scope="session")
def browser_type_launch_args(
    browser_type_launch_args: dict[str, Any],
) -> dict[str, Any]:
    # With neither (CI, a developer laptop) leave the launch args untouched so
    # Playwright falls through to its own managed browser. Never skip on browser
    # absence: a browser that cannot launch must fail the run loudly.
    # A headed run (`--headed`) skips the shell, which cannot show a window.
    is_headed = browser_type_launch_args.get("headless") is False
    candidates = (
        (FORTRESS_CHROMIUM_PATH,)
        if is_headed
        else (TEST_HEADLESS_SHELL_PATH, FORTRESS_CHROMIUM_PATH)
    )
    for executable in candidates:
        if executable.exists():
            return {**browser_type_launch_args, "executable_path": str(executable)}
    return browser_type_launch_args


@pytest.fixture(scope="session")
def playwright() -> NoReturn:
    """Refuses pytest-playwright's session Playwright, which its `browser`, `context`
    and `page` all stand on: its asyncio loop stays running in the worker until the
    session ends, so every later browser test there fails. Fails only the test that
    asks for it."""
    pytest.fail(
        "pytest-playwright's page, context and browser fixtures break every later "
        "browser test in this worker; use `module_browser` from the root conftest.py",
        pytrace=False,
    )


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


@pytest.fixture
def phone_context(module_browser: Browser) -> Iterator[BrowserContext]:
    """A touch-enabled browser context the size of a phone."""
    context = module_browser.new_context(viewport=_PHONE_VIEWPORT, has_touch=True)
    try:
        yield context
    finally:
        context.close()
