import os
import subprocess
import tempfile
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


def _fallback_temp_roots() -> tuple[Path, ...]:
    """Where to put pytest's temp tree when the default root cannot execute, in order tried.

    `/var/tmp` is the alternate the FHS already guarantees; the cache dir is the
    workspace's own writable space, for an image that hardens `/var/tmp` the same way.
    Read at call time rather than at import, since `Path.home()` depends on the
    environment the run was launched with.
    """
    return (Path("/var/tmp"), Path.home() / ".cache" / "pytest-temp")


def _can_execute_a_file_in(directory: Path) -> bool:
    """Whether a file written into `directory` can then be run.

    Answered by running one, not by reading the mount's flags: `noexec` is the usual
    reason a directory refuses, but a `noexec`-free mount can still refuse (a `fs.suid`
    style LSM, a filesystem with no execute bit at all), and what a test planting a stub
    binary on PATH needs to know is the outcome.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=directory, suffix=".sh")
    except OSError:
        return False
    probe = Path(name)
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        probe.chmod(0o700)
        return subprocess.run([str(probe)], capture_output=True).returncode == 0
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


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
    if _can_execute_a_file_in(Path(tempfile.gettempdir())):
        return
    for candidate in _fallback_temp_roots():
        if _can_execute_a_file_in(candidate):
            # `tempfile.tempdir` is what pytest's own `tmp_path` root resolves through,
            # and it caches, so setting the variable alone would be too late for an
            # already-resolved default. TMPDIR is for the subprocesses the tests spawn.
            tempfile.tempdir = str(candidate)
            os.environ["TMPDIR"] = str(candidate)
            return
    tried = ", ".join(
        str(root) for root in (Path(tempfile.gettempdir()), *_fallback_temp_roots())
    )
    raise pytest.UsageError(
        f"no temp directory here can execute a file it holds (tried {tried}). Suites in "
        "this repo run stub binaries they write to one, so they cannot pass as-is; pass "
        "--basetemp naming a directory on a filesystem mounted without noexec."
    )
