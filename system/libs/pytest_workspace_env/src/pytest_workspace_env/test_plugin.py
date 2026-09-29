import os
import subprocess
import sys
from pathlib import Path

from pytest_workspace_env.plugin import MAY_SKIP_MARKER, REQUIRE_WORKSPACE_ENV_VAR

# One test per way a test can skip, one skip the plugin must leave alone, and one test that runs.
_INNER_TEST = f"""
import pytest


def test_a_test_that_skips_itself() -> None:
    pytest.skip("no Fortress on this host 40213")


@pytest.mark.skipif(True, reason="no claude binary on this host 40213")
def test_a_test_skipped_by_a_condition() -> None:
    pass


@pytest.mark.{MAY_SKIP_MARKER}
def test_a_test_whose_skip_is_right_in_a_workspace_too() -> None:
    pytest.skip("needs a non-root user 40213")


def test_a_test_that_runs() -> None:
    pass
"""

_INNER_INI = f"""[pytest]
markers =
    {MAY_SKIP_MARKER}: a skip that is right inside a workspace too
"""


def _run_inner_session(
    project: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    project.mkdir()
    (project / "test_inner.py").write_text(_INNER_TEST)
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
            "test_inner.py",
        ],
        cwd=project,
        env={**inherited, **env_overrides},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_skips_fail_when_the_workspace_env_is_required_unless_marked(
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
    assert "FAILED test_inner.py::test_a_test_that_skips_itself" in completed.stdout
    assert "ERROR at setup of test_a_test_skipped_by_a_condition" in completed.stdout
    assert "SKIPPED [1] test_inner.py:" in completed.stdout
    assert "needs a non-root user 40213" in completed.stdout
    # The failure says how to mark a legitimate skip and keeps the skip's own reason.
    assert f"@pytest.mark.{MAY_SKIP_MARKER}" in completed.stdout
    assert "no Fortress on this host 40213" in completed.stdout
    assert "no claude binary on this host 40213" in completed.stdout


def test_every_skip_stays_a_skip_when_the_workspace_env_is_not_required(
    tmp_path: Path,
) -> None:
    completed = _run_inner_session(tmp_path / "project", {})

    assert completed.returncode == 0, completed.stdout
    assert "1 passed, 3 skipped" in completed.stdout, completed.stdout
    assert "pytest-workspace-env" not in completed.stdout
