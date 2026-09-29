import os
from collections.abc import Generator
from typing import Final

import pytest

REQUIRE_WORKSPACE_ENV_VAR: Final[str] = "DWT_REQUIRE_WORKSPACE_ENV"
# Markers whose tests only run with something a provisioned workspace has (Fortress and Xvfb,
# the pinned claude binary); each pytest root registers them.
WORKSPACE_ONLY_MARKERS: Final[tuple[str, ...]] = ("browser", "real_claude")

_IS_REQUIRED_KEY: Final[pytest.StashKey[bool]] = pytest.StashKey[bool]()


def is_workspace_env_required(environ: dict[str, str]) -> bool:
    return environ.get(REQUIRE_WORKSPACE_ENV_VAR) == "1"


def workspace_only_marker_of(item: pytest.Item) -> str | None:
    for marker_name in WORKSPACE_ONLY_MARKERS:
        if item.get_closest_marker(marker_name) is not None:
            return marker_name
    return None


def skipped_workspace_only_test_message(marker_name: str, skip_reason: str) -> str:
    return (
        f"pytest-workspace-env: a `{marker_name}` test skipped, but {REQUIRE_WORKSPACE_ENV_VAR}=1 says this "
        f"environment must be able to run it. The skip's reason: {skip_reason}"
    )


def _skip_reason_of(longrepr: object) -> str:
    # A skipped report carries (path, line number, reason).
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_IS_REQUIRED_KEY] = is_workspace_env_required(dict(os.environ))


def pytest_report_header(config: pytest.Config) -> str | None:
    if not config.stash[_IS_REQUIRED_KEY]:
        return None
    markers = ", ".join(WORKSPACE_ONLY_MARKERS)
    return f"workspace-only tests ({markers}) fail instead of skipping: {REQUIRE_WORKSPACE_ENV_VAR}=1"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    # An xfail that is not run also reports as skipped; that is the test's own decision, not the environment's.
    if (
        not report.skipped
        or hasattr(report, "wasxfail")
        or not item.config.stash[_IS_REQUIRED_KEY]
    ):
        return report
    marker_name = workspace_only_marker_of(item)
    if marker_name is None:
        return report
    # A skip at setup (a skipif condition) becomes a setup error, one from the test body a failure.
    report.outcome = "failed"
    report.longrepr = skipped_workspace_only_test_message(
        marker_name, _skip_reason_of(report.longrepr)
    )
    return report
