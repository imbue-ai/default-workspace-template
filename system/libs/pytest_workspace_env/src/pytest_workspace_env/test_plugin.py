import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from pytest_workspace_env.plugin import REQUIRE_WORKSPACE_ENV_VAR

# One test per way a test can skip, and one test that runs.
_INNER_TEST = """
import pytest


def test_a_test_that_skips_itself() -> None:
    pytest.skip("no Fortress on this host 40213")


@pytest.mark.skipif(True, reason="no claude binary on this host 40213")
def test_a_test_skipped_by_a_condition() -> None:
    pass


def test_a_test_that_runs() -> None:
    pass
"""

_INNER_INI = "[pytest]\n"

_AGENT_ENV = {
    "MNGR_AGENT_ID": "agent-0000000000000000000000000000leak",
    "MINDS_CHAT_ID": "agent-0000000000000000000000000000chat",
    "CLAUDE_CONFIG_DIR": "/agent/claude-config",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "MAIN_CLAUDE_SESSION_ID": "agent-session-0000",
    "TMUX": "/tmp/tmux-0/default,7,0",
    "TMUX_PANE": "%3",
}

_UNRELATED_VAR = "PYTEST_WORKSPACE_ENV_UNRELATED"

# Fails the whole session if the agent environment is still there when a conftest is imported.
_INNER_CONFTEST = f"""
import os

leaked = [name for name in {sorted(_AGENT_ENV)!r} if name in os.environ]
assert not leaked, leaked
"""

_INNER_AGENT_ENV_TEST = f"""
import os
import subprocess
import sys


def test_a_test_does_not_see_the_agent_environment() -> None:
    assert not [name for name in {sorted(_AGENT_ENV)!r} if name in os.environ]


def test_a_subprocess_does_not_see_the_agent_environment() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import os; print(sorted(name for name in os.environ if name in {sorted(_AGENT_ENV)!r}))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "[]"


def test_other_variables_are_kept() -> None:
    assert os.environ[{_UNRELATED_VAR!r}] == "kept 40213"
"""


def _run_inner_session(
    project: Path, env_overrides: dict[str, str], files: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    project.mkdir()
    for name, content in files.items():
        (project / name).write_text(content)
    # A minimal ini keeps the workspace's own pytest config (addopts, ignores) out of the inner run.
    (project / "pytest.ini").write_text(_INNER_INI)
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name
        not in ("PYTEST_ADDOPTS", "PYTEST_DEBUG_TEMPROOT", REQUIRE_WORKSPACE_ENV_VAR)
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rfs",
        ],
        cwd=project,
        env={**inherited, **env_overrides},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_skips_fail_when_the_workspace_env_is_required(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(
        tmp_path / "project",
        {REQUIRE_WORKSPACE_ENV_VAR: "1"},
        {"test_inner.py": _INNER_TEST},
    )

    assert completed.returncode != 0, completed.stdout
    # A skip from the test body becomes a failure; a skipif condition skips at setup, so it becomes an error.
    assert "1 failed, 1 passed, 1 error" in completed.stdout, completed.stdout
    assert "FAILED test_inner.py::test_a_test_that_skips_itself" in completed.stdout
    assert "ERROR at setup of test_a_test_skipped_by_a_condition" in completed.stdout
    # The failure keeps the skip's own reason.
    assert "no Fortress on this host 40213" in completed.stdout
    assert "no claude binary on this host 40213" in completed.stdout


def test_every_skip_stays_a_skip_when_the_workspace_env_is_not_required(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(
        tmp_path / "project", {}, {"test_inner.py": _INNER_TEST}
    )

    assert completed.returncode == 0, completed.stdout
    assert "1 passed, 2 skipped" in completed.stdout, completed.stdout
    assert "pytest-workspace-env" not in completed.stdout


def test_the_agent_environment_is_cleared_before_any_test_code_runs(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(
        tmp_path / "project",
        {**_AGENT_ENV, _UNRELATED_VAR: "kept 40213"},
        {"conftest.py": _INNER_CONFTEST, "test_inner.py": _INNER_AGENT_ENV_TEST},
    )

    assert completed.returncode == 0, completed.stdout
    assert "3 passed" in completed.stdout, completed.stdout
