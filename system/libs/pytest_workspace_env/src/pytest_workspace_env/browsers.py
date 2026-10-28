"""The browsers a workspace installs for its test suites, and which one a browser test launches.

env-converge installs both, in the background after boot: Playwright's headless shell, for the
tests alone (system/scripts/env.d/1010-playwright-test-headless-shell.sh), and Fortress, the
workspace's stealth-patched Chromium fork (system/scripts/env.d/1000-playwright-fortress.sh).
Playwright's own browser-cache lookup finds neither, so a launch names the binary explicitly.
"""

import os
import sys
from pathlib import Path
from typing import Final

# A browser suite runs about a third faster in about a third less memory on the shell than on Fortress.
TEST_HEADLESS_SHELL_PATH: Final[Path] = Path(
    "/opt/playwright-test-browsers/headless-shell"
)
FORTRESS_CHROMIUM_PATH: Final[Path] = Path("/opt/fortress/tilion-fortress/tilion")


def pick_e2e_browser_executable(is_headed: bool) -> Path | None:
    """The browser binary a test launches: the test headless shell, else Fortress.

    A headed run (``--headed``) skips the shell, which cannot show a window. None when
    neither is installed (CI hosts that ran ``playwright install``, a developer laptop),
    leaving the launch to Playwright's own managed-browser lookup.
    """
    candidates = (
        (FORTRESS_CHROMIUM_PATH,)
        if is_headed
        else (TEST_HEADLESS_SHELL_PATH, FORTRESS_CHROMIUM_PATH)
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def is_e2e_browser_installed() -> bool:
    """True when a Chromium a browser test can launch is present on this host.

    The test headless shell, Fortress, or a browser in Playwright's own download cache
    satisfies the check; with none present a browser test skips instead of erroring at
    launch (and fails where ``DWT_REQUIRE_WORKSPACE_ENV=1`` says the environment is complete).
    """
    if pick_e2e_browser_executable(is_headed=False) is not None:
        return True
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        cache_dir = Path(env_path)
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    return cache_dir.exists() and any(cache_dir.iterdir())
