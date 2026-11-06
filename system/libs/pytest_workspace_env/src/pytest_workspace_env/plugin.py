import os
from collections.abc import Generator, Mapping
from typing import Final

import pytest

REQUIRE_WORKSPACE_ENV_VAR: Final[str] = "DWT_REQUIRE_WORKSPACE_ENV"

_IS_REQUIRED_KEY: Final[pytest.StashKey[bool]] = pytest.StashKey[bool]()

_CLEARED_AGENT_ENV_KEY: Final[pytest.StashKey[tuple[str, ...]]] = pytest.StashKey[
    tuple[str, ...]
]()

# What an mngr agent's environment holds and CI's does not: mngr's and Imbue Studio's own variables, the
# code-guardian stop hook's settings, Claude Code's own session variables, the tmux session the agent runs in,
# and the per-agent directories the agent types point their harness at.
_AGENT_ENV_PREFIXES: Final[tuple[str, ...]] = (
    "MNGR_",
    "MINDS_",
    "CODE_GUARDIAN_",
    "REVIEWER_",
    "CLAUDE_",
)
_AGENT_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "CLAUDECODE",
        "MAIN_CLAUDE_SESSION_ID",
        "TMUX",
        "TMUX_PANE",
        "LLM_USER_PATH",
        "ORIGINAL_CLAUDE_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
    }
)


def is_workspace_env_required(environ: Mapping[str, str]) -> bool:
    return environ.get(REQUIRE_WORKSPACE_ENV_VAR) == "1"


def is_agent_env_var(name: str) -> bool:
    return name.startswith(_AGENT_ENV_PREFIXES) or name in _AGENT_ENV_NAMES


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


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    # Runs before pytest imports any conftest, so clearing here also covers a value read at import time, and every
    # subprocess a test starts inherits the cleared environment.
    cleared = tuple(sorted(name for name in os.environ if is_agent_env_var(name)))
    for name in cleared:
        del os.environ[name]
    early_config.stash[_CLEARED_AGENT_ENV_KEY] = cleared


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_IS_REQUIRED_KEY] = is_workspace_env_required(os.environ)


def pytest_report_header(config: pytest.Config) -> list[str]:
    lines: list[str] = []
    if config.stash[_IS_REQUIRED_KEY]:
        lines.append(f"a skipped test fails: {REQUIRE_WORKSPACE_ENV_VAR}=1")
    cleared = config.stash.get(_CLEARED_AGENT_ENV_KEY, ())
    if cleared:
        lines.append(f"cleared the agent environment: {', '.join(cleared)}")
    return lines


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
