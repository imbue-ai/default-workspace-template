import os
import subprocess
import sys
from pathlib import Path

from pytest_workspace_env.plugin import REQUIRE_WORKSPACE_ENV_VAR

# One test per way a workspace-only test can skip, one skip the plugin must leave alone, and one
# marked test that runs.
_INNER_TEST = """
import pytest


@pytest.mark.browser
def test_marked_test_that_skips_itself() -> None:
    pytest.skip("no Fortress on this host 40213")


@pytest.mark.real_claude
@pytest.mark.skipif(True, reason="no claude binary on this host 40213")
def test_marked_test_skipped_by_a_condition() -> None:
    pass


def test_unmarked_test_that_skips_itself() -> None:
    pytest.skip("skips for a reason of its own 40213")


@pytest.mark.browser
def test_marked_test_that_runs() -> None:
    pass
"""

_INNER_INI = """[pytest]
markers =
    browser: drives a browser
    real_claude: runs the real claude binary
"""


def _run_inner_session(
    project: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    project.mkdir()
    (project / "test_inner.py").write_text(_INNER_TEST)
    # An empty-ish ini keeps the workspace's own pytest config (addopts, ignores) out of the inner run.
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
            "test_inner.py",
        ],
        cwd=project,
        env={**inherited, **env_overrides},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_marked_tests_that_skip_fail_when_the_workspace_env_is_required(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(
        tmp_path / "project", {REQUIRE_WORKSPACE_ENV_VAR: "1"}
    )

    assert completed.returncode != 0, completed.stdout
    # A skip from the test body becomes a failure; a skipif condition skips at setup, so it becomes an error.
    assert "1 failed, 1 passed, 1 skipped, 1 error" in completed.stdout, (
        completed.stdout
    )
    assert (
        "FAILED test_inner.py::test_marked_test_that_skips_itself" in completed.stdout
    )
    assert (
        "ERROR at setup of test_marked_test_skipped_by_a_condition" in completed.stdout
    )
    assert "SKIPPED [1] test_inner.py:" in completed.stdout
    # The failure names the marker and keeps the skip's own reason.
    assert "a `browser` test skipped" in completed.stdout
    assert "no Fortress on this host 40213" in completed.stdout
    assert "a `real_claude` test skipped" in completed.stdout
    assert "no claude binary on this host 40213" in completed.stdout


def test_every_skip_stays_a_skip_when_the_workspace_env_is_not_required(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(tmp_path / "project", {})

    assert completed.returncode == 0, completed.stdout
    assert "1 passed, 3 skipped" in completed.stdout, completed.stdout
    assert "pytest-workspace-env" not in completed.stdout
