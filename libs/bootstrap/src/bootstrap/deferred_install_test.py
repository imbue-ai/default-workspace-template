"""Unit tests for scripts/deferred_install.sh.

The script is bash, so each test sources it in a fresh bash process (it only
runs `main` when executed directly) and exercises one function with a
test-controlled marker dir (DEFERRED_INSTALL_MARKER_DIR -> tmp_path). Only the
network/apt-free paths are covered here: the marker-present skip returns before
any dpkg/apt/uv call, so these tests need no privileged tooling and stay
deterministic on both macOS and Linux.
"""

from __future__ import annotations

from pathlib import Path

from bootstrap.testing import REPO_ROOT
from bootstrap.testing import run_sourced

_SCRIPT = REPO_ROOT / "scripts" / "deferred_install.sh"


def test_marker_for_uses_env_overridable_marker_dir(tmp_path: Path) -> None:
    marker_dir = tmp_path / "markers"
    result = run_sourced(
        _SCRIPT,
        "_marker_for playwright",
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(marker_dir)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{marker_dir}/done.playwright"


def test_install_playwright_skips_without_apt_when_marker_present(tmp_path: Path) -> None:
    # The marker-present branch returns before _recover_interrupted_dpkg / the
    # real install, so no dpkg/apt/uv is invoked.
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / "done.playwright").touch()

    result = run_sourced(
        _SCRIPT,
        "_install_playwright",
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(marker_dir)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "marker present" in result.stdout
    assert "skipping" in result.stdout


def test_main_succeeds_when_marker_present(tmp_path: Path) -> None:
    # Sourcing must NOT run main (the BASH_SOURCE guard); calling it explicitly
    # with every install already marked done reports success and exits 0.
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / "done.playwright").touch()

    result = run_sourced(
        _SCRIPT,
        "main",
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(marker_dir)},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "all deferred installs complete" in result.stdout
