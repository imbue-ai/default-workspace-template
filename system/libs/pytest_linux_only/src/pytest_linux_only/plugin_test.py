import os
import subprocess
import sys
from pathlib import Path

import pytest

from pytest_linux_only.plugin import ALLOW_MACOS_ENV_VAR
from pytest_linux_only.plugin import refusal_message


def test_a_run_on_macos_is_refused() -> None:
    assert refusal_message("darwin", None) is not None


def test_a_run_on_macos_goes_ahead_when_allowed() -> None:
    assert refusal_message("darwin", "1") is None


def test_only_the_documented_value_allows_it() -> None:
    assert refusal_message("darwin", "yes") is not None


def test_a_run_on_linux_goes_ahead() -> None:
    assert refusal_message("linux", None) is None


def _run_inner_session(project: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    project.mkdir()
    (project / "test_inner.py").write_text("def test_nothing() -> None:\n    pass\n")
    # An empty ini keeps the workspace's own pytest config (addopts, ignores) out of the inner run.
    (project / "pytest.ini").write_text("[pytest]\n")
    inherited = {name: value for name, value in os.environ.items() if name != ALLOW_MACOS_ENV_VAR}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(project)],
        cwd=project,
        env={**inherited, **env_overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="the refusal only fires on macOS")
def test_the_installed_plugin_stops_a_macos_session_before_any_test(tmp_path: Path) -> None:
    refused = _run_inner_session(tmp_path / "refused", {})
    allowed = _run_inner_session(tmp_path / "allowed", {ALLOW_MACOS_ENV_VAR: "1"})

    assert refused.returncode == pytest.ExitCode.USAGE_ERROR, refused.stdout + refused.stderr
    assert "1 passed" not in refused.stdout
    assert allowed.returncode == pytest.ExitCode.OK, allowed.stdout + allowed.stderr
