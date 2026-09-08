"""Tests for the content-addressed provisioning skip guard."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_GUARD = Path(__file__).with_name("_provision_guard.sh")

# A guarded step, as setup_system.sh is laid out: skip check, the work, then
# the marker.
_GUARDED_STEP = (
    f'. "{_GUARD}"\n'
    "provision_skip_if_done setup_system\n"
    "echo ran-the-step\n"
    "provision_mark_done setup_system\n"
)


def _provisioned_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "base"],
        cwd=repo,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return repo


def _run_guarded_step(repo: Path, marker_dir: Path, **extra_env: str) -> str:
    result = subprocess.run(
        ["bash", "-c", _GUARDED_STEP],
        env={
            **os.environ,
            "PROVISION_REPO_ROOT": str(repo),
            "PROVISION_MARKER_DIR": str(marker_dir),
            **extra_env,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_a_forced_run_goes_past_the_marker_the_rollback_tree_already_has(
    tmp_path: Path,
) -> None:
    # After a rollback the tree hash is the originally provisioned one, so the
    # marker from that first run matches -- and the re-provision that puts the
    # global toolchain back would be skipped without the override.
    repo = _provisioned_repo(tmp_path)
    marker_dir = tmp_path / "markers"

    first = _run_guarded_step(repo, marker_dir)
    second = _run_guarded_step(repo, marker_dir)
    forced = _run_guarded_step(repo, marker_dir, PROVISION_FORCE="1")

    assert "ran-the-step" in first
    assert "ran-the-step" not in second
    assert "skipping" in second
    assert "ran-the-step" in forced
    assert "PROVISION_FORCE=1" in forced
