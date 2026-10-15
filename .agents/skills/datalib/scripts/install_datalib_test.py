"""Tests for the datalib skill's install_datalib.sh: how it adds the Datalib app's program to supervisord.

The script runs for real against a release that is already installed (stub binaries and docs, so nothing
is downloaded) and a fake supervisorctl that records its calls.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "install_datalib.sh"
# .agents/skills/datalib/scripts/install_datalib_test.py -> the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROGRAM_SOURCE = _REPO_ROOT / "system/apps/datalib/datalib.conf"
_DOCS = (
    "agent_user.md",
    "user/getting_your_data.md",
    "user/config_examples/all_sources.toml",
)

_FAKE_SUPERVISORCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_SUPERVISORCTL_LOG"
case "$1" in
    reread) exit "${FAKE_REREAD_EXIT:-0}" ;;
    status)
        printf 'datalib                          %s\\n' "$FAKE_DATALIB_STATE"
        [ "$FAKE_DATALIB_STATE" = RUNNING ] || exit 3
        ;;
esac
"""


def _pinned_version() -> str:
    match = re.search(
        r'^readonly PINNED_VERSION="(v[0-9.]+)"$', _SCRIPT.read_text(), re.MULTILINE
    )
    assert match is not None
    return match.group(1)


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    release = tmp_path / "install" / _pinned_version()
    _executable(release / "datalib-http", "#!/bin/sh\n")
    _executable(release / "datalib-step", "#!/bin/sh\nexit 0\n")
    for doc in _DOCS:
        (release / "docs" / doc).parent.mkdir(parents=True, exist_ok=True)
        (release / "docs" / doc).write_text("stub\n")
    (tmp_path / "conf.d").mkdir()
    _executable(tmp_path / "supervisorctl", _FAKE_SUPERVISORCTL)
    return tmp_path


def _run(
    workspace: Path, state: str = "RUNNING", reread_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env={
            **os.environ,
            "DATALIB_INSTALL_ROOT": str(workspace / "install"),
            "DATALIB_BIN_DIR": str(workspace / "bin"),
            "DATALIB_PROGRAM_DIR": str(workspace / "conf.d"),
            "DATALIB_SUPERVISORCTL": str(workspace / "supervisorctl"),
            "FAKE_SUPERVISORCTL_LOG": str(workspace / "supervisorctl.log"),
            "FAKE_DATALIB_STATE": state,
            "FAKE_REREAD_EXIT": str(reread_exit),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _calls(workspace: Path) -> list[str]:
    return (workspace / "supervisorctl.log").read_text().splitlines()


def test_the_program_is_copied_in_and_loaded(workspace: Path) -> None:
    """Also guards the script's repository-root path: a wrong one finds no datalib.conf to copy."""
    result = _run(workspace)

    assert result.returncode == 0, result.stderr
    assert (
        workspace / "conf.d" / "datalib.conf"
    ).read_text() == _PROGRAM_SOURCE.read_text()
    assert _calls(workspace) == ["reread", "update datalib", "status datalib"]


def test_a_stopped_program_is_started(workspace: Path) -> None:
    """`update` leaves a program it already has alone, so one stopped since must be started explicitly."""
    result = _run(workspace, state="STOPPED")

    assert result.returncode == 0, result.stderr
    assert _calls(workspace)[-1] == "start datalib"


def test_a_second_run_keeps_the_installed_copy(workspace: Path) -> None:
    assert _run(workspace).returncode == 0
    installed = workspace / "conf.d" / "datalib.conf"
    first_mtime = installed.stat().st_mtime_ns

    result = _run(workspace)

    assert result.returncode == 0, result.stderr
    assert "added the Datalib app's program" not in result.stdout
    assert installed.stat().st_mtime_ns == first_mtime


def test_a_supervisord_that_cannot_reread_fails_the_install(workspace: Path) -> None:
    result = _run(workspace, reread_exit=2)

    assert result.returncode != 0
    assert _calls(workspace) == ["reread"]
