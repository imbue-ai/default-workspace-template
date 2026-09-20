import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# The workspace's browser engine is Fortress (a stealth-patched Chromium fork)
# provisioned by env-converge before any agent starts. Playwright's browser-cache
# lookup only auto-discovers builds Playwright downloaded itself, so a launch has
# to name this binary explicitly. Every suite collected under the repo root
# inherits this override, so pytest-playwright's `page` fixture drives Fortress
# with no per-app setup. The `chat` and `system_interface` apps are NOT under it:
# the root pytest config ignores them and each runs from its own directory.
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")

# Stdlib-only and loaded by path: this file is a conftest, not a package, and the
# module is shared with the two app suites that root elsewhere.
_EXEC_TEMP_ROOT_PATH = (
    Path(__file__).resolve().parent / "system/scripts/exec_temp_root.py"
)


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


def _load_exec_temp_root() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_exec_temp_root", _EXEC_TEMP_ROOT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest_configure(config: pytest.Config) -> None:
    """Move pytest's temp tree off a filesystem that cannot execute what tests write there.

    A workspace container mounts `/tmp` `noexec`, and several suites here test a script
    the way it is really used: plant a stub binary in a temp dir, put that dir on PATH,
    and run the real thing against it. Under the default root every one of those stubs
    dies with "Permission denied", which surfaces as dozens of unrelated-looking
    assertion failures about empty listings -- so these suites are red inside the very
    workspace this template builds, and green in CI, which is the combination that
    teaches an agent to ignore a red suite.

    An explicit `--basetemp` is the caller's own choice and is left alone.
    """
    if config.getoption("basetemp") is not None:
        return
    exec_temp_root = _load_exec_temp_root()
    try:
        exec_temp_root.relocate_temp_root_if_needed()
    except exec_temp_root.NoExecutableTempRootError as e:
        raise pytest.UsageError(str(e)) from e
