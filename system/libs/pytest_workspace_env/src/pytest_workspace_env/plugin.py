import os
from collections.abc import Generator, Mapping
from typing import Final

import pytest

REQUIRE_WORKSPACE_ENV_VAR: Final[str] = "DWT_REQUIRE_WORKSPACE_ENV"

_IS_REQUIRED_KEY: Final[pytest.StashKey[bool]] = pytest.StashKey[bool]()


def is_workspace_env_required(environ: Mapping[str, str]) -> bool:
    return environ.get(REQUIRE_WORKSPACE_ENV_VAR) == "1"


def skipped_test_message(skip_reason: str) -> str:
    return (
        f"pytest-workspace-env: this test skipped, but {REQUIRE_WORKSPACE_ENV_VAR}=1 says this environment must "
        f"be able to run every test. The skip's reason: {skip_reason}"
    )


def _skip_reason_of(longrepr: object) -> str:
    # A skipped report carries (path, line number, reason).
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_IS_REQUIRED_KEY] = is_workspace_env_required(os.environ)


def pytest_report_header(config: pytest.Config) -> str | None:
    if not config.stash[_IS_REQUIRED_KEY]:
        return None
    return f"a skipped test fails: {REQUIRE_WORKSPACE_ENV_VAR}=1"


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
    # A skip at setup (a skipif condition) becomes a setup error, one from the test body a failure.
    report.outcome = "failed"
    report.longrepr = skipped_test_message(_skip_reason_of(report.longrepr))
    return report
