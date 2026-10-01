from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from playwright.sync_api import Browser, BrowserContext, ViewportSize
from terminal_app.testing import TerminalEnvironment, prepare_terminal_environment

# system/apps/terminal/conftest.py -> the repository root, the cwd the app resolves
# system/scripts/forward_port.py against.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

# The phone the plan's e2e sizes name (an iPhone 15 in portrait), with touch.
_PHONE_VIEWPORT: Final[ViewportSize] = {"width": 393, "height": 852}


@pytest.fixture
def terminal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TerminalEnvironment:
    """The cwd and registry the terminal app under test runs against."""
    return prepare_terminal_environment(tmp_path, monkeypatch, REPO_ROOT)


@pytest.fixture
def phone_context(module_browser: Browser) -> Iterator[BrowserContext]:
    """A touch-enabled browser context the size of a phone."""
    context = module_browser.new_context(viewport=_PHONE_VIEWPORT, has_touch=True)
    try:
        yield context
    finally:
        context.close()
