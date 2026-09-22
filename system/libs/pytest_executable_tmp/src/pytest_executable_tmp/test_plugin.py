import os
import subprocess
import sys
from pathlib import Path

import pytest

_STUB_NAME = "stub-tool-73914"

# A test that stands a stub in for a tool the way the workspace's suites do:
# written under tmp_path and put first on PATH.
_INNER_TEST = f"""
import os
import subprocess
from pathlib import Path


def test_the_stub_runs(tmp_path: Path) -> None:
    Path(os.environ["MARKER_PATH"]).write_text(str(tmp_path))
    stub = tmp_path / "{_STUB_NAME}"
    stub.write_text("#!/bin/sh\\necho stub ran\\n")
    stub.chmod(0o755)
    env = {{**os.environ, "PATH": f"{{tmp_path}}:{{os.environ['PATH']}}"}}
    assert subprocess.run(["{_STUB_NAME}"], env=env, capture_output=True, text=True).stdout == "stub ran\\n"
"""


def _run_inner_session(
    project: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    project.mkdir()
    (project / "test_inner.py").write_text(_INNER_TEST)
    # An empty ini keeps the workspace's own pytest config (addopts, ignores) out of the inner run.
    (project / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, **env_overrides}
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(project)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_session_whose_temp_root_can_run_files_uses_it_unchanged(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    marker = tmp_path / "marker"

    result = _run_inner_session(
        tmp_path / "project",
        {
            "TMPDIR": str(temp_root),
            "HOME": str(tmp_path / "home"),
            "MARKER_PATH": str(marker),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(marker.read_text()).is_relative_to(temp_root.resolve())
    assert "temporary files:" not in result.stdout


def test_an_explicit_temp_root_that_cannot_run_files_stops_the_session_before_any_test(
    tmp_path: Path,
) -> None:
    # A regular file in place of the root: nothing can be written under it, let alone run.
    unusable_root = tmp_path / "a-regular-file"
    unusable_root.write_text("")
    marker = tmp_path / "marker"

    result = _run_inner_session(
        tmp_path / "project",
        {
            "PYTEST_DEBUG_TEMPROOT": str(unusable_root),
            "HOME": str(tmp_path / "home"),
            "MARKER_PATH": str(marker),
        },
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        result.stdout + result.stderr
    )
    assert "cannot be run" in result.stdout + result.stderr
    assert not marker.exists()
