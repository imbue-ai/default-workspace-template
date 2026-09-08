from pathlib import Path
from typing import Any

import pytest

# The workspace's browser engine is Fortress (a stealth-patched Chromium fork)
# provisioned by env-converge before any agent starts. Playwright's browser-cache
# lookup only auto-discovers builds Playwright downloaded itself, so a launch has
# to name this binary explicitly. Every suite collected under the repo root
# (each app's tests included) inherits this override, so pytest-playwright's
# `page` fixture drives Fortress with no per-app setup.
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
